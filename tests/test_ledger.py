"""Spend accounting and budget ceilings.

None of these spend money — they write rows directly and assert on the maths.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from convene.errors import BudgetError, ConveneError
from convene.ledger import Budget, Ledger, parse_window


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite3")


def add(ledger: Ledger, *, tag="triage", cost=0.01, cache_read=0, ago_hours=0, **kw):
    from convene.ledger import _now

    ledger.record(
        tag=tag,
        model="claude-sonnet-5",
        cost_usd=cost,
        cache_read_tokens=cache_read,
        when=_now() - timedelta(hours=ago_hours),
        **kw,
    )


# ---- windows ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("2w", timedelta(weeks=2)),
        ("all", None),
    ],
)
def test_window_parsing(text, expected):
    assert parse_window(text) == expected


@pytest.mark.parametrize("bad", ["7", "d", "7x", "seven days", "-1d", "1.5d"])
def test_bad_window_is_rejected_not_defaulted(bad):
    """A mistyped window on a budget would make the ceiling meaningless, and
    nothing else in the system would notice."""
    with pytest.raises(ConveneError, match="bad window"):
        parse_window(bad)


def test_budget_validates_its_window_at_construction():
    with pytest.raises(ConveneError, match="bad window"):
        Budget(5.0, "1 day")


def test_budget_rejects_a_nonpositive_limit():
    with pytest.raises(ConveneError, match="positive"):
        Budget(0.0)


# ---- recording -------------------------------------------------------------
def test_totals_sum_what_was_recorded(ledger):
    add(ledger, cost=0.01)
    add(ledger, cost=0.02)
    totals = ledger.totals()
    assert totals.calls == 2
    assert totals.cost_usd == pytest.approx(0.03)
    assert totals.avg_cost_usd == pytest.approx(0.015)


def test_window_excludes_older_rows(ledger):
    add(ledger, cost=1.00, ago_hours=48)
    add(ledger, cost=0.25, ago_hours=1)
    assert ledger.totals(window="1d").cost_usd == pytest.approx(0.25)
    assert ledger.totals(window="7d").cost_usd == pytest.approx(1.25)
    assert ledger.totals(window="all").cost_usd == pytest.approx(1.25)


def test_by_tag_groups_and_orders_by_spend(ledger):
    add(ledger, tag="cheap", cost=0.01)
    add(ledger, tag="pricey", cost=0.50)
    add(ledger, tag="cheap", cost=0.01)
    by_tag = ledger.by_tag()
    assert list(by_tag) == ["pricey", "cheap"]
    assert by_tag["cheap"].calls == 2
    assert by_tag["pricey"].cost_usd == pytest.approx(0.50)


def test_cache_hit_rate(ledger):
    """The number worth acting on: a low rate means money left on the table."""
    for _ in range(3):
        add(ledger, cache_read=1504)
    add(ledger, cache_read=0)
    assert ledger.totals().cache_hit_rate == pytest.approx(0.75)


def test_empty_ledger_reports_zero_not_a_crash(ledger):
    totals = ledger.totals()
    assert totals.calls == 0
    assert totals.cost_usd == 0.0
    assert totals.cache_hit_rate == 0.0
    assert totals.avg_cost_usd == 0.0


def test_prompts_are_never_stored(ledger):
    """The ledger is for accounting, not transcripts."""
    add(ledger)
    with ledger._connect() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    for forbidden in ("prompt", "system_prompt", "user_prompt", "result", "text"):
        assert forbidden not in columns


# ---- budgets ---------------------------------------------------------------
def test_budget_passes_below_the_limit(ledger):
    add(ledger, cost=0.50)
    ledger.check([Budget(1.00, "1d")])


def test_budget_raises_at_the_limit(ledger):
    add(ledger, cost=1.00)
    with pytest.raises(BudgetError, match="budget reached"):
        ledger.check([Budget(1.00, "1d")])


def test_budget_only_counts_its_own_window(ledger):
    add(ledger, cost=5.00, ago_hours=48)
    ledger.check([Budget(1.00, "1d")])  # yesterday's spend is out of scope
    with pytest.raises(BudgetError):
        ledger.check([Budget(1.00, "7d")])


def test_budget_can_be_scoped_to_one_tag(ledger):
    add(ledger, tag="triage", cost=2.00)
    add(ledger, tag="summarise", cost=0.01)
    ledger.check([Budget(1.00, "1d", tag="summarise")])
    with pytest.raises(BudgetError, match="triage"):
        ledger.check([Budget(1.00, "1d", tag="triage")])


def test_the_error_says_how_to_act_on_it(ledger):
    add(ledger, cost=3.0)
    with pytest.raises(BudgetError) as e:
        ledger.check([Budget(1.0, "1d")])
    message = str(e.value)
    assert "$1.00 per 1d" in message
    assert "3.0000 already spent" in message
    assert "convene usage" in message


def test_check_is_a_pre_spend_guard_not_a_hard_cap(ledger):
    """Documented limitation, pinned so it cannot be quietly mis-sold.

    The ceiling is enforced against spend already recorded. A call that would
    cross it is allowed through; only the *next* one is blocked. Under
    concurrency the overshoot is up to one in-flight batch's worth.
    """
    budget = Budget(1.00, "1d")
    add(ledger, cost=0.99)
    ledger.check([budget])  # 0.99 < 1.00, so this call proceeds...
    add(ledger, cost=50.00)  # ...and may cost far more than the headroom
    with pytest.raises(BudgetError):
        ledger.check([budget])  # only now is it stopped


# ---- environment ------------------------------------------------------------
def test_env_var_creates_a_budget_with_no_code(monkeypatch):
    from convene import ledger as mod

    mod.clear_budgets()
    monkeypatch.setenv("CONVENE_BUDGET_USD", "2.50")
    monkeypatch.setenv("CONVENE_BUDGET_WINDOW", "12h")
    budgets = mod.active_budgets()
    assert len(budgets) == 1
    assert budgets[0].limit_usd == 2.50
    assert budgets[0].window == "12h"


def test_env_budget_defaults_to_one_day(monkeypatch):
    from convene import ledger as mod

    mod.clear_budgets()
    monkeypatch.setenv("CONVENE_BUDGET_USD", "1")
    monkeypatch.delenv("CONVENE_BUDGET_WINDOW", raising=False)
    assert mod.active_budgets()[0].window == "1d"


def test_a_non_numeric_env_budget_is_an_error(monkeypatch):
    from convene import ledger as mod

    mod.clear_budgets()
    monkeypatch.setenv("CONVENE_BUDGET_USD", "five dollars")
    with pytest.raises(ConveneError, match="not a number"):
        mod.active_budgets()


def test_no_budgets_by_default(monkeypatch):
    from convene import ledger as mod

    mod.clear_budgets()
    monkeypatch.delenv("CONVENE_BUDGET_USD", raising=False)
    assert mod.active_budgets() == []


def test_recording_can_be_disabled(monkeypatch):
    from convene import ledger as mod

    monkeypatch.setenv("CONVENE_LEDGER", "0")
    mod.reset_ledger()
    assert mod.recording_enabled() is False
    monkeypatch.setenv("CONVENE_LEDGER", "1")
    mod.reset_ledger()
    assert mod.recording_enabled() is True


# ---- maintenance -----------------------------------------------------------
def test_purge_by_age_keeps_recent_rows(ledger):
    add(ledger, cost=1.0, ago_hours=100)
    add(ledger, cost=1.0, ago_hours=1)
    assert ledger.purge(older_than="2d") == 1
    assert ledger.totals().calls == 1


def test_purge_all_empties_it(ledger):
    add(ledger)
    add(ledger)
    assert ledger.purge() == 2
    assert ledger.totals().calls == 0


def test_schema_survives_reopening(tmp_path):
    """Two Ledger objects on one file must not fight over CREATE TABLE."""
    path = tmp_path / "l.sqlite3"
    add(Ledger(path), cost=0.01)
    add(Ledger(path), cost=0.02)
    assert Ledger(path).totals().calls == 2
