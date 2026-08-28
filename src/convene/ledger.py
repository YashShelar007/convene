"""A spend ledger, and ceilings that stop a runaway loop before it spends.

The flat log file answers "what happened". It cannot answer "how much has the
triage expert cost me this week", or "stop before this batch goes past $5",
and those are the two questions that matter once concurrency is 12 wide and a
retry loop can spawn calls faster than you can read them.

What is recorded
----------------
One row per call: timestamp, tag, model, tokens, cache reads, cost, elapsed.
**Prompts and responses are never stored.** The ledger is for accounting, not
transcripts -- it should be safe to hand to someone without handing them your
data.

What a budget can and cannot do
-------------------------------
:func:`check` runs *before* a call, against spend already recorded. That stops
a loop, a runaway batch, or a misconfigured cron job. It is not a transaction
limit, and it is deliberately not sold as one:

- **Concurrency overshoots.** With 12 calls in flight, the ceiling can be
  crossed by up to 12 calls' worth before the next check sees it. Reservation
  accounting would fix this and is not implemented; see ROADMAP.md.
- **A single call is not bounded by it.** Use ``max_budget_usd`` on the call
  for that -- the CLI enforces it server-side.

So a budget is a guard rail, not a fence. Set it below what would actually
hurt.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import LEDGER_FILE, STATE_ROOT
from .errors import BudgetError, ConveneError

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    TEXT    NOT NULL,
    tag                   TEXT    NOT NULL,
    context               TEXT    NOT NULL DEFAULT '',
    kind                  TEXT    NOT NULL DEFAULT 'call',
    model                 TEXT    NOT NULL,
    effort                TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL    NOT NULL DEFAULT 0.0,
    elapsed_s             REAL    NOT NULL DEFAULT 0.0,
    session_id            TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calls_ts  ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_tag ON calls(tag, ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

#: Windows accepted by :class:`Budget` and ``convene usage --since``.
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_window(window: str) -> timedelta | None:
    """Turn ``"7d"`` into a timedelta. ``"all"`` means no lower bound.

    Raises :class:`ConveneError` on anything else, rather than silently
    defaulting -- a mistyped window on a budget would make the ceiling
    meaningless in a way nothing else would catch.
    """
    w = window.strip().lower()
    if w in {"all", "forever", ""}:
        return None
    unit = w[-1]
    if unit not in _UNITS or not w[:-1].isdigit():
        raise ConveneError(
            f"bad window {window!r}. Use a number followed by "
            f"{'/'.join(sorted(_UNITS))}, e.g. '30m', '24h', '7d', '2w', or 'all'."
        )
    return timedelta(**{_UNITS[unit]: int(w[:-1])})


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Budget:
    """A spend ceiling over a rolling window.

    ``Budget(5.0, "1d")`` means "stop once $5 has been spent in the last 24
    hours". ``tag`` scopes it to one expert; ``None`` covers everything.
    """

    limit_usd: float
    window: str = "1d"
    tag: str | None = None

    def __post_init__(self) -> None:
        if self.limit_usd <= 0:
            raise ConveneError("budget limit_usd must be positive")
        parse_window(self.window)  # validate eagerly, not at first call

    def describe(self) -> str:
        scope = f" for tag {self.tag!r}" if self.tag else ""
        return f"${self.limit_usd:.2f} per {self.window}{scope}"


@dataclass
class Totals:
    """Aggregated spend over a slice of the ledger."""

    calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    elapsed_s: float = 0.0
    cache_hits: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """Share of calls that read a warm prompt cache.

        The single most useful number here. A low rate on a high-volume expert
        means its system prompt is too short, unstable, or interpolated -- and
        that each call is paying roughly 3-10x what it could.
        """
        return (self.cache_hits / self.calls) if self.calls else 0.0

    @property
    def avg_cost_usd(self) -> float:
        return (self.cost_usd / self.calls) if self.calls else 0.0

    @property
    def avg_elapsed_s(self) -> float:
        return (self.elapsed_s / self.calls) if self.calls else 0.0


class Ledger:
    """SQLite-backed record of every call.

    A connection is opened per operation rather than held open. That costs
    ~0.1ms against calls that take 3-9 seconds, and in exchange the ledger is
    safe to use from threads, from asyncio, and from several processes at once
    without any locking of our own.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else LEDGER_FILE
        self._initialised = False
        self._lock = threading.Lock()

    # ---- plumbing ---------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._ensure_schema()
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        if self._initialised:
            return
        with self._lock:
            if self._initialised:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=15.0)
            try:
                # WAL lets readers and a writer coexist, which matters when a
                # batch is writing while `convene usage` reads.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                conn.commit()
            finally:
                conn.close()
            with contextlib.suppress(OSError):
                STATE_ROOT.chmod(0o700)
            self._initialised = True

    # ---- writing ----------------------------------------------------------
    def record(
        self,
        *,
        tag: str,
        model: str,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        elapsed_s: float = 0.0,
        session_id: str = "",
        context: str = "",
        effort: str | None = None,
        kind: str = "call",
        when: datetime | None = None,
    ) -> None:
        """Append one row. Never stores prompt or response text."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO calls
                   (ts, tag, context, kind, model, effort, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    cost_usd, elapsed_s, session_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _iso(when or _now()),
                    tag,
                    context,
                    kind,
                    model,
                    effort,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_creation_tokens,
                    cost_usd,
                    elapsed_s,
                    session_id,
                ),
            )

    # ---- reading ----------------------------------------------------------
    def _where(self, since: timedelta | None, tag: str | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(_iso(_now() - since))
        if tag is not None:
            clauses.append("tag = ?")
            params.append(tag)
        return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params

    _AGG = """SELECT COUNT(*) AS calls,
                     COALESCE(SUM(cost_usd), 0)              AS cost_usd,
                     COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                     COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                     COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
                     COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                     COALESCE(SUM(elapsed_s), 0)             AS elapsed_s,
                     COALESCE(SUM(cache_read_tokens > 0), 0) AS cache_hits
              FROM calls"""

    def totals(self, *, window: str = "all", tag: str | None = None) -> Totals:
        """Aggregate spend over a rolling window."""
        since = parse_window(window)
        where, params = self._where(since, tag)
        with self._connect() as conn:
            row = conn.execute(self._AGG + where, params).fetchone()
        # sqlite3.Row iterates values, not keys, so `k in row` (what SIM118
        # suggests) is False for every column name and would yield {}.
        return Totals(**{k: row[k] for k in row.keys()})  # noqa: SIM118

    def by_tag(self, *, window: str = "all") -> dict[str, Totals]:
        """Aggregate per tag, busiest spend first."""
        since = parse_window(window)
        where, params = self._where(since, None)
        query = (
            self._AGG.replace("SELECT ", "SELECT tag, ")
            + where
            + " GROUP BY tag ORDER BY cost_usd DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {
            r["tag"]: Totals(**{k: r[k] for k in r.keys() if k != "tag"})  # noqa: SIM118
            for r in rows
        }

    def by_day(self, *, window: str = "30d") -> dict[str, Totals]:
        """Aggregate per calendar day (UTC), oldest first."""
        since = parse_window(window)
        where, params = self._where(since, None)
        query = (
            self._AGG.replace("SELECT ", "SELECT substr(ts, 1, 10) AS day, ")
            + where
            + " GROUP BY day ORDER BY day"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {
            r["day"]: Totals(**{k: r[k] for k in r.keys() if k != "day"})  # noqa: SIM118
            for r in rows
        }

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def purge(self, *, older_than: str | None = None) -> int:
        """Delete rows. Without ``older_than``, empties the ledger."""
        with self._connect() as conn:
            if older_than is None:
                n = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
                conn.execute("DELETE FROM calls")
            else:
                delta = parse_window(older_than)
                if delta is None:
                    raise ConveneError("purge --older-than needs a window, not 'all'")
                cutoff = _iso(_now() - delta)
                n = conn.execute(
                    "SELECT COUNT(*) FROM calls WHERE ts < ?", (cutoff,)
                ).fetchone()[0]
                conn.execute("DELETE FROM calls WHERE ts < ?", (cutoff,))
        return int(n)

    # ---- budgets ----------------------------------------------------------
    def check(self, budgets: Sequence[Budget], tag: str | None = None) -> None:
        """Raise :class:`BudgetError` if any applicable ceiling is reached.

        ``tag`` is the tag of the call about to be made. A budget scoped to a
        different tag does not apply to it -- a $1/day ceiling on ``triage``
        must not stop a ``summarise`` call, which is the whole point of scoping
        one. Pass ``None`` to evaluate every budget regardless of scope, which
        is what a standalone "am I over budget?" check wants.

        Called before a request is sent. See the module docstring for what this
        does and does not guarantee under concurrency.
        """
        for budget in budgets:
            if tag is not None and budget.tag is not None and budget.tag != tag:
                continue
            spent = self.totals(window=budget.window, tag=budget.tag).cost_usd
            if spent >= budget.limit_usd:
                raise BudgetError(
                    f"budget reached: {budget.describe()} — "
                    f"${spent:.4f} already spent in the last {budget.window}. "
                    f"Raise the limit, wait for the window to roll, or run "
                    f"`convene usage --since {budget.window}` to see where it went."
                )


# ---- Process-wide default --------------------------------------------------
_ledger: Ledger | None = None
_budgets: list[Budget] = []
_recording: bool | None = None


def get_ledger() -> Ledger:
    """The process-wide ledger."""
    global _ledger
    if _ledger is None:
        _ledger = Ledger()
    return _ledger


def reset_ledger() -> None:
    """Drop the cached ledger and budgets. For tests that move CONVENE_HOME."""
    global _ledger, _recording
    _ledger = None
    _recording = None
    _budgets.clear()


def recording_enabled() -> bool:
    """Is call recording on? Set ``CONVENE_LEDGER=0`` to turn it off."""
    global _recording
    if _recording is None:
        _recording = os.environ.get("CONVENE_LEDGER", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
    return _recording


def add_budget(budget: Budget) -> Budget:
    """Register a ceiling checked before every call in this process."""
    _budgets.append(budget)
    return budget


def clear_budgets() -> None:
    _budgets.clear()


def active_budgets() -> list[Budget]:
    """Budgets registered in code, plus one from the environment if set.

    ``CONVENE_BUDGET_USD=5`` (optionally with ``CONVENE_BUDGET_WINDOW=1d``)
    gives a spend ceiling with no code change at all, which is the version most
    people actually want on a cron job.
    """
    budgets = list(_budgets)
    raw = os.environ.get("CONVENE_BUDGET_USD", "").strip()
    if raw:
        try:
            limit = float(raw)
        except ValueError as e:
            raise ConveneError(f"CONVENE_BUDGET_USD={raw!r} is not a number") from e
        budgets.append(
            Budget(
                limit_usd=limit,
                window=os.environ.get("CONVENE_BUDGET_WINDOW", "1d").strip() or "1d",
            )
        )
    return budgets


def check_budgets(tag: str | None = None) -> None:
    """Enforce every budget that applies to a call with this tag.

    Raises :class:`BudgetError`.
    """
    budgets = active_budgets()
    if budgets:
        get_ledger().check(budgets, tag=tag)
