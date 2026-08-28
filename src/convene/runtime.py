"""The engine: build argv, run `claude -p`, decode the envelope.

Everything else in convene is a layer over :func:`run` / :func:`run_sync`.

Why the flag pile
-----------------
A plain ``claude -p`` is a coding agent: it loads the full agent system prompt,
every tool definition, your skills, MCP servers, project settings and
CLAUDE.md. Measured on an identical one-word prompt:

    locked down   in=  144   cache_create=    0   cost=$0.00051
    plain -p      in=    2   cache_create= 9504   cost=$0.04478    # 88x

So the lockdown flags are not tidiness, they are the difference between this
being usable and being absurd. Do not drop them casually.

Why not ``--bare``
------------------
``--bare`` is faster to start and is what Anthropic recommends for scripted
calls, but its own documentation says: *"In bare mode, Claude Code never reads
OAuth credentials or the system keychain"* and *"bare mode doesn't use your
subscription login."* It requires an API key. Using it would defeat the entire
point of subscription auth, so convene never passes it.

Anthropic also states ``--bare`` *"will become the default for -p in a future
release."* If that lands, subscription auth through this path breaks. `convene
doctor` probes for it empirically rather than sniffing version numbers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .auth import DEFAULT_AUTH, AuthMode, preflight, subprocess_env
from .config import (
    DEFAULT_MODEL,
    SANDBOX_WORKDIR,
    SUBPROCESS_TIMEOUT_S,
    EffortLevel,
)
from .errors import CLIError
from .ledger import check_budgets, get_ledger, recording_enabled
from .logging_ import get_logger

# Flags that strip the coding agent back to an inference endpoint. Each one was
# measured; see the module docstring and FINDINGS.md.
LOCKDOWN_FLAGS: tuple[str, ...] = (
    # No tools at all. Removing these is the single biggest cost saving.
    "--tools",
    "",
    # No skills.
    "--disable-slash-commands",
    # Ignore project and local settings; user settings only.
    "--setting-sources",
    "user",
    # Ignore every configured MCP server.
    "--strict-mcp-config",
    # There are no tools left to gate, and an approval prompt would hang
    # forever because no subprocess can answer it.
    "--permission-mode",
    "bypassPermissions",
)


@dataclass(frozen=True)
class Call:
    """One inference request. Immutable so it is safe to reuse and to log."""

    user_prompt: str
    system_prompt: str = "You are a concise, accurate assistant."
    model: str = DEFAULT_MODEL
    json_schema: dict | None = None
    effort: EffortLevel | None = None
    max_budget_usd: float | None = None
    fallback_model: str | None = None
    #: Built-in tools to allow, as the CLI's ``--tools`` value. The default of
    #: ``""`` means none. Set to e.g. ``"Read"`` to pass an image file path.
    #: Anything non-empty re-adds that tool's definition to the prompt and
    #: costs tokens, so opt in deliberately.
    tools: str = ""
    #: Reuse a specific session UUID (creates it if new).
    session_id: str | None = None
    #: Resume an existing session by UUID, carrying its conversation history.
    resume: str | None = None
    #: Session files are written to disk unless this is True. Persistence is
    #: required for ``resume`` to work later.
    ephemeral: bool = True
    timeout_s: float = SUBPROCESS_TIMEOUT_S
    log_tag: str = "call"
    log_context: str = ""


@dataclass
class Result:
    """What came back, plus what it cost."""

    structured_output: dict[str, Any] | None
    text: str | None
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    #: Client-side list-price estimate, not a charge. On subscription auth it
    #: is an accounting figure for comparing prompts, not a bill.
    cost_usd: float
    session_id: str
    num_turns: int
    elapsed_s: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def cache_hit(self) -> bool:
        """Did this call read a warm prompt cache?"""
        return self.cache_read_input_tokens > 0

    @property
    def billable_input_tokens(self) -> int:
        """Input tokens excluding cache reads, which are billed at a discount."""
        return self.input_tokens + self.cache_creation_input_tokens

    # ---- back-compat aliases ----------------------------------------------
    # The predecessor to this package returned a ClaudeResult with these two
    # names, and its documented field list is what existing callers reach for.
    # Keeping them as properties means `from claude_cli import ...` code keeps
    # working unchanged rather than failing with AttributeError.
    @property
    def result_text(self) -> str | None:
        """Deprecated alias for :attr:`text`."""
        return self.text

    @property
    def total_cost_usd(self) -> float:
        """Deprecated alias for :attr:`cost_usd`."""
        return self.cost_usd


def build_argv(call: Call) -> list[str]:
    """Translate a :class:`Call` into a `claude` command line."""
    argv: list[str] = [
        "claude",
        "-p",
        call.user_prompt,
        "--output-format",
        "json",
        "--system-prompt",
        call.system_prompt,
        "--model",
        call.model,
    ]

    # --tools is part of the lockdown set, so swap in a custom value rather
    # than emitting the flag twice; the CLI takes the last occurrence, but
    # relying on that is fragile.
    flags = list(LOCKDOWN_FLAGS)
    if call.tools != "":
        flags[flags.index("--tools") + 1] = call.tools
    argv += flags

    if call.ephemeral and not call.session_id and not call.resume:
        # Nothing written to disk. Incompatible with resuming later, which is
        # why any call that names a session opts out.
        argv.append("--no-session-persistence")
    if call.session_id:
        argv += ["--session-id", call.session_id]
    if call.resume:
        argv += ["--resume", call.resume]
    if call.json_schema is not None:
        # Enforced server-side; the CLI auto-retries invalid JSON before giving
        # up with subtype "error_max_structured_output_retries".
        argv += ["--json-schema", json.dumps(call.json_schema, separators=(",", ":"))]
    if call.effort is not None:
        argv += ["--effort", call.effort]
    if call.max_budget_usd is not None:
        argv += ["--max-budget-usd", str(call.max_budget_usd)]
    if call.fallback_model:
        argv += ["--fallback-model", call.fallback_model]
    return argv


def decode_envelope(stdout: str, call: Call, elapsed: float) -> Result:
    """Parse and validate the ``--output-format json`` envelope.

    The trap this function exists for: **auth failure does not look like a
    failure.** The CLI returns

        {"subtype": "success", "is_error": true, "result": "Not logged in..."}

    and exits **0**. Neither the exit code nor ``subtype`` catches it.
    ``is_error`` is the only reliable signal. Do not "simplify" the check below
    to ``subtype == "success"``.
    """
    logger = get_logger()
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.error(
            "bad-envelope tag=%s model=%s elapsed=%.1fs stdout=%s",
            call.log_tag,
            call.model,
            elapsed,
            stdout[:400],
        )
        raise CLIError(f"claude CLI returned a non-JSON envelope: {e}") from e

    subtype = envelope.get("subtype", "?")
    is_error = bool(envelope.get("is_error", False))
    if subtype != "success" or is_error:
        message = str(envelope.get("result", "")).strip()
        logger.warning(
            "fail subtype=%s is_error=%s tag=%s model=%s elapsed=%.1fs result=%s",
            subtype,
            is_error,
            call.log_tag,
            call.model,
            elapsed,
            message[:200],
        )
        raise CLIError(
            f"claude CLI failed (subtype={subtype}, is_error={is_error}): "
            f"{(message or subtype)[:300]}"
        )

    usage = envelope.get("usage") or {}
    result = Result(
        structured_output=envelope.get("structured_output"),
        text=envelope.get("result"),
        model=call.model,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        session_id=str(envelope.get("session_id", "")),
        num_turns=int(envelope.get("num_turns", 0)),
        elapsed_s=elapsed,
        raw=envelope,
    )
    logger.info(
        "ok tag=%s model=%s elapsed=%.1fs in=%d out=%d cache_read=%d "
        "cache_create=%d cost=%.5f context=%s",
        call.log_tag,
        call.model,
        elapsed,
        result.input_tokens,
        result.output_tokens,
        result.cache_read_input_tokens,
        result.cache_creation_input_tokens,
        result.cost_usd,
        call.log_context,
    )
    return result


def _record(call: Call, result: Result) -> None:
    """Append this call to the spend ledger. Never fatal.

    A ledger write failing must not lose a result the caller already paid for,
    so this swallows storage errors after logging them.
    """
    if not recording_enabled():
        return
    try:
        get_ledger().record(
            tag=call.log_tag,
            context=call.log_context,
            model=result.model,
            effort=call.effort,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_input_tokens,
            cache_creation_tokens=result.cache_creation_input_tokens,
            cost_usd=result.cost_usd,
            elapsed_s=result.elapsed_s,
            session_id=result.session_id,
            kind="call",
        )
    except Exception:
        get_logger().exception("failed to write to the spend ledger")


def run_sync(call: Call, *, auth_mode: AuthMode = DEFAULT_AUTH) -> Result:
    """Run one inference, blocking. Raises :class:`CLIError` on failure.

    Raises :class:`~convene.errors.BudgetError` *before* spending anything if a
    registered budget has already been reached.
    """
    preflight(auth_mode)
    check_budgets(call.log_tag)
    argv = build_argv(call)
    logger = get_logger()
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            env=subprocess_env(auth_mode),
            cwd=str(SANDBOX_WORKDIR),
            capture_output=True,
            text=True,
            timeout=call.timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise CLIError(f"failed to spawn the claude CLI: {e}") from e
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        logger.warning(
            "timeout tag=%s model=%s elapsed=%.1fs context=%s",
            call.log_tag,
            call.model,
            elapsed,
            call.log_context,
        )
        raise CLIError(f"claude CLI timed out after {call.timeout_s:.0f}s") from None

    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        _log_exit(logger, call, proc.returncode, proc.stderr or "", elapsed)
        raise CLIError(
            f"claude CLI exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
        )
    result = decode_envelope(proc.stdout, call, elapsed)
    _record(call, result)
    return result


async def run(call: Call, *, auth_mode: AuthMode = DEFAULT_AUTH) -> Result:
    """Run one inference, async. Raises :class:`CLIError` on failure.

    Raises :class:`~convene.errors.BudgetError` *before* spending anything if a
    registered budget has already been reached.
    """
    preflight(auth_mode)
    check_budgets(call.log_tag)
    argv = build_argv(call)
    logger = get_logger()
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=subprocess_env(auth_mode),
            cwd=str(SANDBOX_WORKDIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise CLIError(f"failed to spawn the claude CLI: {e}") from e

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=call.timeout_s
        )
    except TimeoutError:
        proc.kill()
        # Reap the killed child so it does not linger as a zombie.
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        elapsed = time.monotonic() - start
        logger.warning(
            "timeout tag=%s model=%s elapsed=%.1fs context=%s",
            call.log_tag,
            call.model,
            elapsed,
            call.log_context,
        )
        raise CLIError(f"claude CLI timed out after {call.timeout_s:.0f}s") from None

    elapsed = time.monotonic() - start
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        _log_exit(logger, call, proc.returncode or -1, stderr, elapsed)
        raise CLIError(f"claude CLI exited {proc.returncode}: {stderr.strip()[:300]}")
    result = decode_envelope(stdout, call, elapsed)
    _record(call, result)
    return result


def _log_exit(logger, call: Call, code: int, stderr: str, elapsed: float) -> None:
    logger.error(
        "exit=%d tag=%s model=%s elapsed=%.1fs stderr=%s",
        code,
        call.log_tag,
        call.model,
        elapsed,
        stderr.strip()[:400],
    )
