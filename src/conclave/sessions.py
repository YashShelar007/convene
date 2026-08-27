"""Multi-turn conversations, in the two shapes the CLI actually supports.

The original claim that ``claude -p`` cannot hold a conversation is wrong.
There are two mechanisms, with different trade-offs, and both are verified
against Claude Code 2.1.237.

:class:`Session` -- durable
    Backed by ``--session-id`` and ``--resume``. Each turn is a fresh
    subprocess, so every turn pays process startup (~1s), but the conversation
    is written to disk and survives your program exiting, crashing, or being
    rescheduled onto another machine's cron slot tomorrow. Since Claude Code
    2.1.223 a session is found by id from any working directory.

:class:`LiveSession` -- hot
    Backed by ``--input-format stream-json`` with ``--output-format
    stream-json``: one long-lived process that takes turn after turn on stdin.
    No per-turn startup cost, but the conversation dies with the process.

Measured, three turns, same conversation:

    LiveSession   6.37s -> 5.42s -> 4.32s   state retained, one session id
    Session       7.24s -> 8.82s -> 6.54s   state retained, one session id

A gotcha that will bite you if you sum costs
--------------------------------------------
The two paths report ``total_cost_usd`` differently, and neither documents it:

    LiveSession   0.000987 -> 0.002121 -> 0.003417   **cumulative** per session
    Session       0.000990 -> 0.001130 -> 0.001300   **per call**

So summing the field naively across a live session's turns triple-counts.
:attr:`Turn.cost_usd` on this module's objects is always the *incremental* cost
of that turn; the raw envelope figure stays available as ``turn.result.cost_usd``.

Both paths grow input tokens as history accumulates (299 -> 363 -> 417 on a
trivial conversation). A long session is not free; it is a growing prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .auth import DEFAULT_AUTH, AuthMode, preflight, subprocess_env
from .config import (
    DEFAULT_MODEL,
    SANDBOX_WORKDIR,
    WORKER_TURN_TIMEOUT_S,
    EffortLevel,
)
from .errors import SessionError
from .logging_ import get_logger
from .runtime import LOCKDOWN_FLAGS, Call, Result, run, run_sync


@dataclass
class Turn:
    """One exchange in a conversation."""

    text: str | None
    structured_output: dict[str, Any] | None
    #: Incremental cost of *this* turn, normalised across both session kinds.
    cost_usd: float
    elapsed_s: float
    result: Result

    @property
    def input_tokens(self) -> int:
        return self.result.input_tokens

    @property
    def output_tokens(self) -> int:
        return self.result.output_tokens


# ---------------------------------------------------------------------------
# Durable sessions
# ---------------------------------------------------------------------------
@dataclass
class Session:
    """A conversation that lives on disk and can be resumed later.

    Usage::

        s = Session(system_prompt=RUBRIC, model="claude-sonnet-5")
        s.ask("Here is the first document. Summarise it.")
        s.ask("Now compare it to the second.")
        print(s.id)          # save this; resume tomorrow

        later = Session.resume(saved_id, system_prompt=RUBRIC)
    """

    system_prompt: str = "You are a concise, accurate assistant."
    model: str = DEFAULT_MODEL
    effort: EffortLevel | None = None
    json_schema: dict | None = None
    max_budget_usd: float | None = None
    tools: str = ""
    auth_mode: AuthMode = DEFAULT_AUTH
    #: Session UUID. Generated if not supplied.
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    #: False until the first turn has created the session on disk.
    started: bool = False
    turns: list[Turn] = field(default_factory=list)

    @classmethod
    def resume(cls, session_id: str, **kwargs: Any) -> Session:
        """Reattach to a session created earlier, in this process or another."""
        return cls(id=session_id, started=True, **kwargs)

    def _call(self, user_prompt: str, **overrides: Any) -> Call:
        return Call(
            user_prompt=user_prompt,
            system_prompt=self.system_prompt,
            model=self.model,
            json_schema=self.json_schema,
            effort=self.effort,
            max_budget_usd=self.max_budget_usd,
            tools=self.tools,
            # The first turn names the session to create it; later turns resume
            # it. Persistence must stay on for either to mean anything.
            session_id=None if self.started else self.id,
            resume=self.id if self.started else None,
            ephemeral=False,
            log_tag="session",
            log_context=f"sid={self.id[:8]}",
            **overrides,
        )

    def _record(self, result: Result, elapsed: float) -> Turn:
        # The durable path reports cost per call, so no delta arithmetic.
        turn = Turn(
            text=result.text,
            structured_output=result.structured_output,
            cost_usd=result.cost_usd,
            elapsed_s=elapsed,
            result=result,
        )
        self.turns.append(turn)
        self.started = True
        return turn

    def ask(self, user_prompt: str, **overrides: Any) -> Turn:
        """Take one turn, blocking."""
        start = time.monotonic()
        result = run_sync(self._call(user_prompt, **overrides), auth_mode=self.auth_mode)
        return self._record(result, time.monotonic() - start)

    async def ask_async(self, user_prompt: str, **overrides: Any) -> Turn:
        """Take one turn, async."""
        start = time.monotonic()
        result = await run(self._call(user_prompt, **overrides), auth_mode=self.auth_mode)
        return self._record(result, time.monotonic() - start)

    @property
    def total_cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.turns)


# ---------------------------------------------------------------------------
# Live sessions
# ---------------------------------------------------------------------------
def _live_argv(
    *,
    system_prompt: str,
    model: str,
    effort: EffortLevel | None,
    json_schema: dict | None,
    max_budget_usd: float | None,
    tools: str,
) -> list[str]:
    """Command line for a persistent, turn-taking process.

    Note that ``--json-schema`` is fixed for the process lifetime: a live
    session answers in one shape. If you need different shapes, use different
    sessions.
    """
    argv = [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        # stream-json output requires verbose; without it the CLI refuses.
        "--verbose",
        "--system-prompt",
        system_prompt,
        "--model",
        model,
    ]
    flags = list(LOCKDOWN_FLAGS)
    if tools != "":
        flags[flags.index("--tools") + 1] = tools
    argv += flags
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
    if effort is not None:
        argv += ["--effort", effort]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    return argv


def _user_message(text: str) -> str:
    """Encode one turn in the stream-json input protocol."""
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    )


def _turn_from_envelope(
    envelope: dict[str, Any], prior_cost: float, elapsed: float, model: str
) -> tuple[Turn, float]:
    """Build a Turn from a live session's ``result`` message.

    Returns the turn and the new running cost. The live path reports cumulative
    session cost, so the incremental cost is the difference.
    """
    if envelope.get("subtype") != "success" or envelope.get("is_error"):
        message = str(envelope.get("result", "")).strip()
        raise SessionError(
            f"turn failed (subtype={envelope.get('subtype')}, "
            f"is_error={envelope.get('is_error')}): {(message or '?')[:300]}"
        )

    usage = envelope.get("usage") or {}
    cumulative = float(envelope.get("total_cost_usd") or 0.0)
    result = Result(
        structured_output=envelope.get("structured_output"),
        text=envelope.get("result"),
        model=model,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
        cost_usd=cumulative,
        session_id=str(envelope.get("session_id", "")),
        num_turns=int(envelope.get("num_turns", 0)),
        elapsed_s=elapsed,
        raw=envelope,
    )
    turn = Turn(
        text=result.text,
        structured_output=result.structured_output,
        # max() guards against a provider that ever switches to per-call
        # reporting; a negative incremental cost is never right.
        cost_usd=max(0.0, cumulative - prior_cost),
        elapsed_s=elapsed,
        result=result,
    )
    return turn, cumulative


@dataclass
class LiveSession:
    """A conversation held open in one long-lived process.

    Skips per-turn process startup and keeps history in the child. Dies with
    the process -- nothing is written to disk -- so use :class:`Session` when
    the conversation has to outlive the program.

    Use it as a context manager so the child is always reaped::

        with LiveSession(system_prompt=RUBRIC) as s:
            s.ask("First document: ...")
            s.ask("Now compare to the second.")
    """

    system_prompt: str = "You are a concise, accurate assistant."
    model: str = DEFAULT_MODEL
    effort: EffortLevel | None = None
    json_schema: dict | None = None
    max_budget_usd: float | None = None
    tools: str = ""
    auth_mode: AuthMode = DEFAULT_AUTH
    turn_timeout_s: float = WORKER_TURN_TIMEOUT_S

    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _cumulative_cost: float = 0.0
    turns: list[Turn] = field(default_factory=list)
    session_id: str = ""

    def start(self) -> LiveSession:
        """Spawn the child process. Idempotent."""
        if self._proc is not None:
            return self
        preflight(self.auth_mode)
        argv = _live_argv(
            system_prompt=self.system_prompt,
            model=self.model,
            effort=self.effort,
            json_schema=self.json_schema,
            max_budget_usd=self.max_budget_usd,
            tools=self.tools,
        )
        self._proc = subprocess.Popen(
            argv,
            cwd=str(SANDBOX_WORKDIR),
            env=subprocess_env(self.auth_mode),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return self

    def ask(self, user_prompt: str) -> Turn:
        """Take one turn. Blocks until the child emits its ``result`` message."""
        if self._proc is None:
            self.start()
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None

        if proc.poll() is not None:
            raise SessionError(
                f"live session process already exited with code {proc.returncode}. "
                "Start a new LiveSession."
            )

        start = time.monotonic()
        try:
            proc.stdin.write(_user_message(user_prompt) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise SessionError(f"live session stdin is closed: {e}") from e

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # The stream carries only JSON lines; anything else is a bug
                # worth seeing rather than swallowing silently.
                get_logger().warning("live-session non-JSON line: %s", line[:200])
                continue
            if message.get("type") == "result":
                elapsed = time.monotonic() - start
                turn, self._cumulative_cost = _turn_from_envelope(
                    message, self._cumulative_cost, elapsed, self.model
                )
                self.session_id = turn.result.session_id or self.session_id
                self.turns.append(turn)
                get_logger().info(
                    "live-turn model=%s elapsed=%.1fs in=%d out=%d cost=%.5f sid=%s",
                    self.model,
                    elapsed,
                    turn.input_tokens,
                    turn.output_tokens,
                    turn.cost_usd,
                    self.session_id[:8],
                )
                return turn
            if time.monotonic() - start > self.turn_timeout_s:
                raise SessionError(
                    f"live session turn exceeded {self.turn_timeout_s:.0f}s"
                )

        stderr = ""
        if proc.stderr is not None:
            try:
                stderr = proc.stderr.read() or ""
            except (OSError, ValueError):
                stderr = ""
        raise SessionError(
            "live session ended without returning a result"
            + (f": {stderr.strip()[:300]}" if stderr.strip() else "")
        )

    def close(self) -> None:
        """Close stdin and reap the child. Idempotent."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    @property
    def total_cost_usd(self) -> float:
        return self._cumulative_cost

    def __enter__(self) -> LiveSession:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class AsyncLiveSession:
    """Async twin of :class:`LiveSession`, for pooling."""

    system_prompt: str = "You are a concise, accurate assistant."
    model: str = DEFAULT_MODEL
    effort: EffortLevel | None = None
    json_schema: dict | None = None
    max_budget_usd: float | None = None
    tools: str = ""
    auth_mode: AuthMode = DEFAULT_AUTH
    turn_timeout_s: float = WORKER_TURN_TIMEOUT_S

    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _cumulative_cost: float = 0.0
    turns: list[Turn] = field(default_factory=list)
    session_id: str = ""

    async def start(self) -> AsyncLiveSession:
        if self._proc is not None:
            return self
        preflight(self.auth_mode)
        argv = _live_argv(
            system_prompt=self.system_prompt,
            model=self.model,
            effort=self.effort,
            json_schema=self.json_schema,
            max_budget_usd=self.max_budget_usd,
            tools=self.tools,
        )
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(SANDBOX_WORKDIR),
            env=subprocess_env(self.auth_mode),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return self

    async def ask(self, user_prompt: str) -> Turn:
        """Take one turn. Serialised per session -- a conversation is ordered."""
        if self._proc is None:
            await self.start()
        async with self._lock:
            return await self._ask_locked(user_prompt)

    async def _ask_locked(self, user_prompt: str) -> Turn:
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        if proc.returncode is not None:
            raise SessionError(
                f"live session process already exited with code {proc.returncode}"
            )

        start = time.monotonic()
        try:
            proc.stdin.write((_user_message(user_prompt) + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise SessionError(f"live session stdin is closed: {e}") from e

        async def read_result() -> dict[str, Any]:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    raise SessionError("live session ended without returning a result")
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    get_logger().warning("live-session non-JSON line: %s", line[:200])
                    continue
                if message.get("type") == "result":
                    return message

        try:
            envelope = await asyncio.wait_for(read_result(), timeout=self.turn_timeout_s)
        except TimeoutError:
            raise SessionError(
                f"live session turn exceeded {self.turn_timeout_s:.0f}s"
            ) from None

        elapsed = time.monotonic() - start
        turn, self._cumulative_cost = _turn_from_envelope(
            envelope, self._cumulative_cost, elapsed, self.model
        )
        self.session_id = turn.result.session_id or self.session_id
        self.turns.append(turn)
        return turn

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ConnectionResetError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()

    @property
    def total_cost_usd(self) -> float:
        return self._cumulative_cost

    async def __aenter__(self) -> AsyncLiveSession:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class SessionPool:
    """A fixed set of live sessions, checked out one caller at a time.

    Amortises process startup across many independent conversations. Note that
    a pooled session **keeps its history between checkouts** -- it is a warm
    process, not a blank one. That is the point when you want a shared context
    (a rubric plus accumulated examples), and a bug when you do not. Pass
    ``reset_between=True`` to get a fresh process per checkout instead, which
    keeps the pool purely as a startup-cost optimisation.

    Usage::

        async with SessionPool(size=4, system_prompt=RUBRIC) as pool:
            async with pool.acquire() as session:
                turn = await session.ask("...")
    """

    def __init__(
        self,
        size: int = 4,
        *,
        reset_between: bool = False,
        **session_kwargs: Any,
    ) -> None:
        if size < 1:
            raise ValueError("pool size must be at least 1")
        self.size = size
        self.reset_between = reset_between
        self._session_kwargs = session_kwargs
        self._free: asyncio.Queue[AsyncLiveSession] = asyncio.Queue()
        self._all: list[AsyncLiveSession] = []
        self._started = False

    async def start(self) -> SessionPool:
        """Spawn every session up front, concurrently."""
        if self._started:
            return self
        self._all = [AsyncLiveSession(**self._session_kwargs) for _ in range(self.size)]
        await asyncio.gather(*(s.start() for s in self._all))
        for s in self._all:
            self._free.put_nowait(s)
        self._started = True
        return self

    def acquire(self) -> _PoolCheckout:
        """Check out a session. Use as an async context manager."""
        return _PoolCheckout(self)

    async def _replace(self, session: AsyncLiveSession) -> AsyncLiveSession:
        """Retire a session and put a fresh one in its place."""
        await session.close()
        fresh = AsyncLiveSession(**self._session_kwargs)
        await fresh.start()
        self._all = [fresh if s is session else s for s in self._all]
        return fresh

    async def close(self) -> None:
        await asyncio.gather(*(s.close() for s in self._all), return_exceptions=True)
        self._all = []
        self._started = False

    @property
    def total_cost_usd(self) -> float:
        return sum(s.total_cost_usd for s in self._all)

    async def __aenter__(self) -> SessionPool:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class _PoolCheckout:
    """Async context manager yielding one session from a pool."""

    def __init__(self, pool: SessionPool) -> None:
        self._pool = pool
        self._session: AsyncLiveSession | None = None

    async def __aenter__(self) -> AsyncLiveSession:
        if not self._pool._started:
            await self._pool.start()
        self._session = await self._pool._free.get()
        return self._session

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        # A session whose process died, or whose turn raised, is not safe to
        # hand to the next caller.
        died = session._proc is None or session._proc.returncode is not None
        if died or self._pool.reset_between or exc_type is not None:
            try:
                session = await self._pool._replace(session)
            except Exception:
                get_logger().exception("failed to replace a pooled session")
                return
        self._pool._free.put_nowait(session)
