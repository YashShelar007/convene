"""Session bookkeeping, especially the cost-reporting asymmetry.

The two session kinds report ``total_cost_usd`` differently -- per call for the
durable path, cumulative for the live one -- and nothing in the CLI documents
that. These tests pin the normalisation so a refactor cannot quietly
reintroduce triple-counted costs.
"""

from __future__ import annotations

import pytest

from conclave.errors import SessionError
from conclave.sessions import Session, _live_argv, _turn_from_envelope, _user_message


def envelope(cost: float, **extra: object) -> dict:
    base = {
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "total_cost_usd": cost,
        "session_id": "s-1",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    base.update(extra)
    return base


def test_live_session_costs_are_differenced_not_summed():
    """Measured live: 0.000987 -> 0.002121 -> 0.003417 is cumulative.

    Summing the raw field would report $0.0065 for a conversation that cost
    $0.0034.
    """
    prior = 0.0
    increments = []
    for cumulative in (0.000987, 0.002121, 0.003417):
        turn, prior = _turn_from_envelope(envelope(cumulative), prior, 1.0, "m")
        increments.append(turn.cost_usd)

    assert increments == pytest.approx([0.000987, 0.001134, 0.001296], abs=1e-9)
    assert sum(increments) == pytest.approx(0.003417, abs=1e-9)
    assert prior == pytest.approx(0.003417)


def test_a_decreasing_cumulative_cost_never_yields_a_negative_turn():
    turn, _ = _turn_from_envelope(envelope(0.001), 0.005, 1.0, "m")
    assert turn.cost_usd == 0.0


def test_raw_envelope_cost_stays_available():
    """The incremental figure is the useful one, but the raw one is not lost."""
    turn, _ = _turn_from_envelope(envelope(0.002121), 0.000987, 1.0, "m")
    assert turn.cost_usd == pytest.approx(0.001134)
    assert turn.result.cost_usd == pytest.approx(0.002121)


def test_failed_turn_raises_even_with_success_subtype():
    bad = envelope(0.0, is_error=True, result="Not logged in · Please run /login")
    with pytest.raises(SessionError, match="Not logged in"):
        _turn_from_envelope(bad, 0.0, 1.0, "m")


# ---- durable sessions ------------------------------------------------------
def test_first_turn_creates_and_later_turns_resume():
    from conclave.runtime import build_argv

    session = Session(system_prompt="s")
    first = build_argv(session._call("hello"))
    assert "--session-id" in first
    assert "--resume" not in first

    session.started = True
    later = build_argv(session._call("again"))
    assert "--resume" in later
    assert "--session-id" not in later
    assert later[later.index("--resume") + 1] == session.id


def test_durable_session_persists_to_disk():
    """--no-session-persistence would make the session unresumable."""
    from conclave.runtime import build_argv

    assert "--no-session-persistence" not in build_argv(Session()._call("hi"))


def test_resume_reattaches_without_recreating():
    session = Session.resume("dead-beef", system_prompt="s")
    assert session.id == "dead-beef"
    assert session.started is True
    assert "--resume" in __import__(
        "conclave.runtime", fromlist=["build_argv"]
    ).build_argv(session._call("x"))


def test_sessions_get_distinct_ids():
    assert Session().id != Session().id


# ---- live protocol ---------------------------------------------------------
def test_user_message_matches_the_stream_json_protocol():
    import json

    message = json.loads(_user_message("hello"))
    assert message["type"] == "user"
    assert message["message"]["role"] == "user"
    assert message["message"]["content"] == [{"type": "text", "text": "hello"}]


def test_live_argv_requires_verbose():
    """stream-json output is refused by the CLI without --verbose."""
    argv = _live_argv(
        system_prompt="s",
        model="m",
        effort=None,
        json_schema=None,
        max_budget_usd=None,
        tools="",
    )
    assert "--verbose" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--bare" not in argv
    assert argv[argv.index("--tools") + 1] == ""
