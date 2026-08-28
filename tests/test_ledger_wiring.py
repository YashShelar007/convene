"""The ledger hooks in the engine and in sessions.

These fake the subprocess rather than spending, so they can assert the thing
that actually matters about a budget: that **no process is spawned** once the
ceiling is reached. A budget that only reports after the fact would pass a
weaker test.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from convene import ledger as ledger_mod
from convene.errors import BudgetError
from convene.ledger import Budget, add_budget, clear_budgets, get_ledger
from convene.runtime import Call, run_sync

ENVELOPE = json.dumps(
    {
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "session_id": "sess-1234",
        "total_cost_usd": 0.0042,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 3,
            "cache_read_input_tokens": 1504,
            "cache_creation_input_tokens": 0,
        },
    }
)


@pytest.fixture(autouse=True)
def _fresh_ledger(tmp_path, monkeypatch):
    """Point the process-wide ledger at a temp file, and clear budgets."""
    monkeypatch.setenv("CONVENE_HOME", str(tmp_path))
    monkeypatch.setenv("CONVENE_LEDGER", "1")
    monkeypatch.delenv("CONVENE_BUDGET_USD", raising=False)
    ledger_mod.reset_ledger()
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "ledger.sqlite3")
    clear_budgets()
    yield
    clear_budgets()
    ledger_mod.reset_ledger()


@pytest.fixture
def spawned(monkeypatch):
    """Fake the subprocess, and count how many times it was launched."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=ENVELOPE, stderr="")

    monkeypatch.setattr("convene.runtime.subprocess.run", fake_run)
    monkeypatch.setattr("convene.runtime.preflight", lambda mode: None)
    return calls


def a_call(**kw) -> Call:
    kw.setdefault("log_tag", "t")
    return Call(user_prompt="hi", system_prompt="s", **kw)


# ---- recording -------------------------------------------------------------
def test_a_call_is_recorded(spawned):
    run_sync(a_call())
    totals = get_ledger().totals()
    assert totals.calls == 1
    assert totals.cost_usd == pytest.approx(0.0042)
    assert totals.cache_read_tokens == 1504
    assert totals.cache_hit_rate == 1.0


def test_the_tag_becomes_the_ledger_key(spawned):
    run_sync(a_call(log_tag="triage"))
    run_sync(a_call(log_tag="triage"))
    run_sync(a_call(log_tag="summarise"))
    by_tag = get_ledger().by_tag()
    assert by_tag["triage"].calls == 2
    assert by_tag["summarise"].calls == 1


def test_recording_off_writes_nothing(spawned, monkeypatch):
    monkeypatch.setenv("CONVENE_LEDGER", "0")
    ledger_mod.reset_ledger()
    run_sync(a_call())
    assert get_ledger().totals().calls == 0
    assert len(spawned) == 1, "the call itself must still happen"


def test_a_broken_ledger_does_not_lose_a_paid_result(spawned, monkeypatch):
    """The result was already paid for; an accounting failure must not eat it."""

    def boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(get_ledger(), "record", boom)
    assert run_sync(a_call()).text == "ok"


# ---- budgets ---------------------------------------------------------------
def test_budget_blocks_before_any_process_is_spawned(spawned):
    """The whole point: it must stop the spend, not report it afterwards."""
    get_ledger().record(tag="t", model="m", cost_usd=5.00)
    add_budget(Budget(1.00, "1d"))

    with pytest.raises(BudgetError):
        run_sync(a_call())

    assert spawned == [], "a blocked call must not launch the CLI"


def test_calls_proceed_under_the_ceiling(spawned):
    get_ledger().record(tag="t", model="m", cost_usd=0.10)
    add_budget(Budget(1.00, "1d"))
    assert run_sync(a_call()).text == "ok"
    assert len(spawned) == 1


def test_a_budget_engages_once_recorded_spend_crosses_it(spawned):
    add_budget(Budget(0.008, "1d"))
    # Each fake call records $0.0042. After two, $0.0084 >= $0.008, so the
    # third is refused -- and the ceiling is crossed by the second call, which
    # is the documented overshoot, not a bug.
    run_sync(a_call())
    run_sync(a_call())
    with pytest.raises(BudgetError):
        run_sync(a_call())
    assert len(spawned) == 2


def test_a_tag_scoped_budget_ignores_other_tags(spawned):
    get_ledger().record(tag="expensive", model="m", cost_usd=9.99)
    add_budget(Budget(1.00, "1d", tag="expensive"))
    assert run_sync(a_call(log_tag="cheap")).text == "ok"
    with pytest.raises(BudgetError):
        run_sync(a_call(log_tag="expensive"))


def test_no_budget_means_no_ceiling(spawned):
    get_ledger().record(tag="t", model="m", cost_usd=1000.0)
    assert run_sync(a_call()).text == "ok"


# ---- sessions --------------------------------------------------------------
def test_live_session_turns_record_incremental_cost(tmp_path):
    """A live session reports cumulative cost; the ledger must store the delta,
    or a three-turn conversation is recorded at roughly twice what it cost."""
    from convene.sessions import _record_turn, _turn_from_envelope

    def envelope(cumulative):
        return {
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s-1",
            "total_cost_usd": cumulative,
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }

    prior = 0.0
    for cumulative in (0.000987, 0.002121, 0.003417):
        turn, prior = _turn_from_envelope(envelope(cumulative), prior, 1.0, "m")
        _record_turn(turn, "claude-sonnet-5", "low")

    totals = get_ledger().totals()
    assert totals.calls == 3
    # The true conversation cost, not 0.000987 + 0.002121 + 0.003417 = 0.006525.
    assert totals.cost_usd == pytest.approx(0.003417, abs=1e-9)
