"""Tests that make real calls. Opt in with `pytest -m live`.

These cost money -- a few cents for the whole file -- and require a working
`claude` login. They exist because the claims in FINDINGS.md are the product,
and a claim nobody re-runs rots.
"""

from __future__ import annotations

import pytest

from conclave import Call, LiveSession, Session, ask, ask_json, ready, run_sync

pytestmark = pytest.mark.live

CHEAP = {"model": "claude-sonnet-5", "effort": "low"}


@pytest.fixture(scope="module", autouse=True)
def _require_auth():
    if not ready():
        pytest.skip("not logged in to Claude Code")


def test_text_round_trip():
    assert "paris" in ask("Capital of France, one word.", **CHEAP).lower()


def test_schema_is_enforced_server_side():
    data = ask_json(
        "Extract: Ada Lovelace, born 1815.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}, "born": {"type": "integer"}},
            "required": ["name", "born"],
            "additionalProperties": False,
        },
        **CHEAP,
    )
    assert data["born"] == 1815
    assert isinstance(data["born"], int)


def test_prompt_cache_engages_on_a_long_stable_prompt():
    """The core economic claim: a repeated long system prompt gets cheaper."""
    system = "You classify documents for an enterprise archive. " * 200
    cold = run_sync(
        Call(user_prompt="Classify: invoice. One word.", system_prompt=system, **CHEAP)
    )
    warm = run_sync(
        Call(user_prompt="Classify: receipt. One word.", system_prompt=system, **CHEAP)
    )

    assert cold.cache_creation_input_tokens > 0, "no cache entry was created"
    assert warm.cache_read_input_tokens > 0, "second call did not read the cache"
    assert warm.cost_usd < cold.cost_usd, (
        f"warm call ({warm.cost_usd}) was not cheaper than cold ({cold.cost_usd})"
    )


def test_short_prompts_do_not_cache():
    """The other half of the claim, and the reason the lint exists."""
    result = run_sync(Call(user_prompt="Say ok.", system_prompt="Be terse.", **CHEAP))
    assert result.cache_creation_input_tokens == 0


def test_durable_session_remembers_across_processes():
    session = Session(system_prompt="You are terse.", **CHEAP)
    session.ask("My favourite number is 41. Acknowledge in two words.")
    reattached = Session.resume(session.id, system_prompt="You are terse.", **CHEAP)
    assert "42" in (
        reattached.ask("My favourite number plus one? Digits only.").text or ""
    )


def test_live_session_remembers_within_one_process():
    with LiveSession(system_prompt="You are terse.", **CHEAP) as session:
        session.ask("My favourite number is 41. Acknowledge in two words.")
        assert "42" in (session.ask("Plus one? Digits only.").text or "")
        # Cumulative reporting must not double-count.
        assert sum(t.cost_usd for t in session.turns) == pytest.approx(
            session.total_cost_usd, abs=1e-9
        )
