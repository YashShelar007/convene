"""The engine: argv construction and the envelope trap.

None of these spend money.
"""

from __future__ import annotations

import json

import pytest

from conclave.errors import CLIError
from conclave.runtime import Call, build_argv, decode_envelope


def test_lockdown_flags_present_by_default():
    argv = build_argv(Call(user_prompt="hi", system_prompt="sys"))
    for flag in (
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--no-session-persistence",
    ):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_bare_is_never_passed():
    """--bare forces API-key auth and never reads OAuth. It must never appear."""
    argv = build_argv(Call(user_prompt="hi", system_prompt="s", tools="Read"))
    assert "--bare" not in argv


def test_tools_override_does_not_duplicate_the_flag():
    argv = build_argv(Call(user_prompt="hi", system_prompt="s", tools="Read"))
    assert argv.count("--tools") == 1
    assert argv[argv.index("--tools") + 1] == "Read"


def test_session_calls_keep_persistence():
    """--no-session-persistence would make --resume impossible later."""
    argv = build_argv(Call(user_prompt="hi", system_prompt="s", session_id="abc"))
    assert "--no-session-persistence" not in argv
    assert argv[argv.index("--session-id") + 1] == "abc"

    argv = build_argv(Call(user_prompt="hi", system_prompt="s", resume="abc"))
    assert "--no-session-persistence" not in argv
    assert argv[argv.index("--resume") + 1] == "abc"


def test_schema_is_compact_json():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    argv = build_argv(Call(user_prompt="hi", system_prompt="s", json_schema=schema))
    emitted = argv[argv.index("--json-schema") + 1]
    assert " " not in emitted
    assert json.loads(emitted) == schema


def test_optional_flags_are_omitted_when_unset():
    argv = build_argv(Call(user_prompt="hi", system_prompt="s"))
    for flag in ("--json-schema", "--effort", "--max-budget-usd", "--fallback-model"):
        assert flag not in argv


# ---- the envelope trap -----------------------------------------------------
_CALL = Call(user_prompt="hi", system_prompt="s")


def test_auth_failure_is_caught_despite_success_subtype():
    """The whole point of decode_envelope.

    The CLI reports auth failure as subtype "success" with is_error true, and
    exits 0. Neither the exit code nor the subtype catches it.
    """
    envelope = json.dumps(
        {
            "subtype": "success",
            "is_error": True,
            "result": "Not logged in · Please run /login",
        }
    )
    with pytest.raises(CLIError, match="Not logged in"):
        decode_envelope(envelope, _CALL, 1.0)


def test_error_subtypes_are_caught():
    envelope = json.dumps(
        {"subtype": "error_max_structured_output_retries", "is_error": False}
    )
    with pytest.raises(CLIError, match="error_max_structured_output_retries"):
        decode_envelope(envelope, _CALL, 1.0)


def test_non_json_stdout_raises():
    with pytest.raises(CLIError, match="non-JSON"):
        decode_envelope("segfault", _CALL, 1.0)


def test_successful_envelope_maps_every_field():
    envelope = json.dumps(
        {
            "subtype": "success",
            "is_error": False,
            "result": "Paris",
            "structured_output": {"city": "Paris"},
            "session_id": "abc-123",
            "num_turns": 1,
            "total_cost_usd": 0.00123,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_input_tokens": 4191,
                "cache_creation_input_tokens": 243,
            },
        }
    )
    result = decode_envelope(envelope, _CALL, 2.5)
    assert result.text == "Paris"
    assert result.structured_output == {"city": "Paris"}
    assert result.cost_usd == pytest.approx(0.00123)
    assert result.cache_read_input_tokens == 4191
    assert result.cache_hit is True
    assert result.billable_input_tokens == 253
    assert result.elapsed_s == 2.5


def test_missing_usage_defaults_to_zero():
    envelope = json.dumps({"subtype": "success", "is_error": False, "result": "x"})
    result = decode_envelope(envelope, _CALL, 1.0)
    assert result.input_tokens == 0
    assert result.cost_usd == 0.0
    assert result.cache_hit is False


def test_null_cost_does_not_crash():
    """total_cost_usd has been observed as null; float(None) would raise."""
    envelope = json.dumps(
        {"subtype": "success", "is_error": False, "result": "x", "total_cost_usd": None}
    )
    assert decode_envelope(envelope, _CALL, 1.0).cost_usd == 0.0


def test_back_compat_result_aliases():
    """Existing callers reach for the predecessor's field names.

    The old ClaudeResult exposed `result_text` and `total_cost_usd`. Dropping
    them would break every project importing through the claude_cli shim with
    an AttributeError, which is exactly what the shim exists to prevent.
    """
    envelope = json.dumps(
        {
            "subtype": "success",
            "is_error": False,
            "result": "Paris",
            "total_cost_usd": 0.00123,
        }
    )
    result = decode_envelope(envelope, _CALL, 1.0)
    assert result.result_text == result.text == "Paris"
    assert result.total_cost_usd == result.cost_usd == pytest.approx(0.00123)
