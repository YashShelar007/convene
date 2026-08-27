"""Diagnostics: is this set up, and which account is about to be billed?

`convene doctor` exists because the two worst failures in this design are both
silent. An API key that outranks your login bills API credits while succeeding
normally -- anthropics/claude-code#37686 reports $1,800 in two days from that.
And an auth failure returns ``subtype: "success"`` with exit code 0.

The last check is a live probe rather than a version comparison. Anthropic's
documentation says ``--bare`` *"will become the default for -p in a future
release"*, and bare mode never reads OAuth credentials. If that lands, every
subscription call through this library starts failing. Rather than guess from a
version string, the probe spends about a tenth of a cent finding out.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Literal

from .auth import (
    API_KEY_VARS,
    DEFAULT_AUTH,
    SUBSCRIPTION_MODES,
    AuthMode,
    binary_available,
    cli_version,
    is_authed,
    login_status,
)
from .config import CHEAP_MODEL, LEDGER_FILE, LOG_FILE, SANDBOX_DIR, STATE_ROOT
from .errors import ConveneError
from .runtime import Call, run_sync

Status = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    """One diagnostic result."""

    name: str
    status: Status
    detail: str
    fix: str = ""

    @property
    def icon(self) -> str:
        return {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[self.status]


def check_binary() -> Check:
    if not binary_available():
        return Check(
            "claude binary",
            "fail",
            "not found on PATH",
            "Install Claude Code: https://code.claude.com/docs/en/quickstart",
        )
    version = cli_version() or "unknown version"
    return Check("claude binary", "ok", version)


def check_api_key_contamination() -> Check:
    """Is an API key set that would silently outrank the subscription login?"""
    present = [v for v in API_KEY_VARS if os.environ.get(v)]
    if not present:
        return Check(
            "api key",
            "ok",
            "no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in the environment",
        )
    return Check(
        "api key",
        "warn",
        f"{', '.join(present)} is set. convene strips it from every "
        f"subscription call, so your calls are safe -- but a bare `claude -p` "
        f"run by hand, or any other tool, would bill API credits.",
        f"unset {' '.join(present)}",
    )


def check_account(expected_email: str | None = None) -> Check:
    status = login_status()
    if not status.get("loggedIn"):
        return Check(
            "account", "fail", "not logged in", "run `claude /login` in a terminal"
        )
    email = status.get("email") or "unknown"
    method = status.get("authMethod") or "?"
    plan = status.get("subscriptionType") or "no subscription"
    if method != "claude.ai":
        return Check(
            "account",
            "fail",
            f"{email} via {method} -- this is not a subscription login and "
            f"would bill API credits",
            f"unset {' '.join(API_KEY_VARS)}, then `claude /login`",
        )
    if expected_email and email != expected_email:
        return Check(
            "account",
            "fail",
            f"logged in as {email}, expected {expected_email}",
            "`claude auth logout` then `claude auth login`",
        )
    return Check("account", "ok", f"{email} ({plan}, via {method})")


def check_state_dir() -> Check:
    """Is the state directory present, and not world-readable?"""
    if not STATE_ROOT.exists():
        return Check(
            "state dir",
            "ok",
            f"{STATE_ROOT} (will be created on first call)",
        )
    problems = []
    for path in (SANDBOX_DIR, STATE_ROOT):
        if not path.exists():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            problems.append(f"{path} is mode {mode:o}")
    if problems:
        return Check(
            "state dir",
            "warn",
            "readable by other users: " + "; ".join(problems),
            f"chmod 700 {SANDBOX_DIR}",
        )
    return Check("state dir", "ok", str(STATE_ROOT))


def check_probe(auth_mode: AuthMode = DEFAULT_AUTH) -> Check:
    """Actually make a call. Costs roughly $0.001.

    This is the check that catches a future release flipping ``--bare`` on by
    default, which would break subscription auth without changing any flag we
    pass.
    """
    if not binary_available() or not is_authed(auth_mode):
        return Check("live probe", "warn", "skipped -- not authed")
    try:
        result = run_sync(
            Call(
                user_prompt="Reply with the single word: ok",
                system_prompt="You reply with exactly one word.",
                model=CHEAP_MODEL,
                effort="low",
                log_tag="doctor",
            ),
            auth_mode=auth_mode,
        )
    except ConveneError as e:
        message = str(e)
        if "Not logged in" in message or "login" in message.lower():
            return Check(
                "live probe",
                "fail",
                f"a locked-down call failed to authenticate: {message[:200]}",
                "If this appeared after a Claude Code update, check whether "
                "`-p` now defaults to --bare, which never reads OAuth. "
                "Pin the previous version and open an issue.",
            )
        return Check("live probe", "fail", message[:250])
    return Check(
        "live probe",
        "ok",
        f"{result.model} answered in {result.elapsed_s:.1f}s "
        f"(in={result.input_tokens} out={result.output_tokens} "
        f"cost=${result.cost_usd:.5f})",
    )


def check_lockdown(auth_mode: AuthMode = DEFAULT_AUTH) -> Check:
    """Compare a locked-down call against a default one. Costs a few cents.

    The gap is the whole economic argument for this library, and it is worth
    re-measuring on your own machine, because it depends on how much you have
    in your CLAUDE.md, skills and MCP config.
    """
    import json
    import subprocess
    import time

    from .auth import subprocess_env
    from .config import SANDBOX_WORKDIR

    prompt = "Name the capital of France in one word."

    locked = run_sync(
        Call(
            user_prompt=prompt,
            system_prompt="You are terse.",
            model=CHEAP_MODEL,
            effort="low",
            log_tag="doctor-locked",
        ),
        auth_mode=auth_mode,
    )

    start = time.monotonic()
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json", "--model", CHEAP_MODEL],
        cwd=str(SANDBOX_WORKDIR),
        env=subprocess_env(auth_mode),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    plain_elapsed = time.monotonic() - start
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return Check(
            "lockdown value",
            "warn",
            f"locked-down call cost ${locked.cost_usd:.5f}; the unlocked "
            f"comparison could not be parsed",
        )

    plain_cost = float(envelope.get("total_cost_usd") or 0.0)
    plain_usage = envelope.get("usage") or {}
    ratio = (plain_cost / locked.cost_usd) if locked.cost_usd else 0.0
    return Check(
        "lockdown value",
        "ok" if ratio > 2 else "warn",
        f"locked down: in={locked.input_tokens} "
        f"cache_create={locked.cache_creation_input_tokens} "
        f"cost=${locked.cost_usd:.5f} ({locked.elapsed_s:.1f}s) | "
        f"plain -p: in={plain_usage.get('input_tokens', 0)} "
        f"cache_create={plain_usage.get('cache_creation_input_tokens', 0)} "
        f"cost=${plain_cost:.5f} ({plain_elapsed:.1f}s) | "
        f"{ratio:.0f}x",
    )


def run_checks(
    *,
    auth_mode: AuthMode = DEFAULT_AUTH,
    expected_email: str | None = None,
    probe: bool = True,
    lockdown: bool = False,
) -> list[Check]:
    """Run the diagnostic suite in dependency order."""
    checks = [check_binary()]
    if checks[0].status == "fail":
        return checks

    checks.append(check_api_key_contamination())
    if auth_mode in SUBSCRIPTION_MODES:
        checks.append(check_account(expected_email))
    checks.append(check_state_dir())

    if probe and checks[0].status != "fail":
        checks.append(check_probe(auth_mode))
    if lockdown:
        try:
            checks.append(check_lockdown(auth_mode))
        except ConveneError as e:
            checks.append(Check("lockdown value", "warn", str(e)[:200]))

    checks.append(
        Check(
            "logs",
            "ok",
            f"calls: {LOG_FILE}"
            + (f" | ledger: {LEDGER_FILE}" if LEDGER_FILE.exists() else ""),
        )
    )
    return checks


def worst(checks: list[Check]) -> Status:
    """The most severe status in a set of checks."""
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "ok"
