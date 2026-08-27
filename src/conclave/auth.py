"""Which account gets billed, and how to be sure before you spend anything.

The failure this module exists to prevent: a run that quietly bills Anthropic
API credits instead of the subscription you meant to use. It is a real and
expensive mistake -- anthropics/claude-code#37686 reports $1,800 in two days
from exactly this -- and nothing downstream notices, because a key-billed call
succeeds identically to a subscription-billed one.

Two defences, both here:

1. :func:`subprocess_env` strips ``ANTHROPIC_API_KEY`` and
   ``ANTHROPIC_AUTH_TOKEN`` from the child environment in both subscription
   modes. Either variable silently outranks the OAuth login.
2. :func:`assert_account` refuses to start when ``authMethod`` is not
   ``claude.ai``, or when the logged-in email is not the one you named.

:class:`AuthMode.API_KEY` exists and works, but is never selected implicitly.
You have to ask for it by name.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from enum import StrEnum
from typing import Any

from .config import (
    SANDBOX_CREDENTIALS,
    SANDBOX_DIR,
    SANDBOX_HOME,
    SANDBOX_TOKEN_FILE,
    SANDBOX_WORKDIR,
)
from .errors import AuthError

# Either of these outranks both the claude.ai login and CLAUDE_CODE_OAUTH_TOKEN.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class AuthMode(StrEnum):
    """How the subprocess authenticates."""

    #: Reuse the login you already did with ``claude /login``. Billed to your
    #: subscription. Nothing to set up; the credentials refresh themselves.
    LOGIN = "login"

    #: An isolated sandbox with its own long-lived token from
    #: ``claude setup-token``, its own ``HOME`` and its own config dir. Also
    #: subscription-billed. Use for cron and servers, or to stay unaffected by
    #: logging out of interactive Claude Code.
    SANDBOX_TOKEN = "sandbox_token"

    #: Anthropic API credits, per token. Never chosen implicitly.
    API_KEY = "api_key"


class CLIMissing(AuthError):
    """The ``claude`` binary is not installed or not on PATH."""


DEFAULT_AUTH = AuthMode.LOGIN

#: Modes that draw on a Claude subscription rather than API credits.
SUBSCRIPTION_MODES = frozenset({AuthMode.LOGIN, AuthMode.SANDBOX_TOKEN})


def binary_available() -> bool:
    """Is the ``claude`` CLI on PATH?"""
    return shutil.which("claude") is not None


def cli_version() -> str | None:
    """Version string reported by ``claude --version``, or None if unavailable."""
    if not binary_available():
        return None
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "").strip() or None


def login_status() -> dict[str, Any]:
    """Ask the CLI who it thinks it is.

    Shells out, so it costs ~0.5s. Callers should cache; :func:`is_authed`
    already does.
    """
    if not binary_available():
        return {}
    env = {k: v for k, v in os.environ.items() if k not in API_KEY_VARS}
    try:
        proc = subprocess.run(
            ["claude", "auth", "status", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


_login_ok: bool | None = None


def is_authed(auth_mode: AuthMode = DEFAULT_AUTH, *, recheck: bool = False) -> bool:
    """Can this mode authenticate?

    The LOGIN answer is cached, because it shells out and would otherwise add
    ~0.5s to every single inference call.
    """
    global _login_ok
    if auth_mode is AuthMode.API_KEY:
        return any(os.environ.get(v) for v in API_KEY_VARS)
    if auth_mode is AuthMode.SANDBOX_TOKEN:
        return SANDBOX_TOKEN_FILE.exists() or SANDBOX_CREDENTIALS.exists()
    if _login_ok is None or recheck:
        _login_ok = bool(login_status().get("loggedIn"))
    return _login_ok


def ready(auth_mode: AuthMode = DEFAULT_AUTH) -> bool:
    """Cheap gate for "should a router pick this backend?"."""
    return binary_available() and is_authed(auth_mode)


def whoami() -> str:
    """One-line summary of which account the CLI will bill."""
    s = login_status()
    if not s.get("loggedIn"):
        return "not logged in"
    return (
        f"{s.get('email') or 'unknown'} "
        f"({s.get('subscriptionType') or 'no subscription'}, "
        f"via {s.get('authMethod') or '?'})"
    )


def assert_account(expected_email: str | None = None) -> dict[str, Any]:
    """Fail loudly, before a batch, if the wrong account would be billed.

    Rejects three things: not logged in; an ``authMethod`` that is not
    ``claude.ai`` (an API key has taken over); and the wrong email.

    Call this at the top of any job that will make more than a handful of
    calls. Nothing further down the pipeline will notice a billing mistake.
    """
    s = login_status()
    if not s.get("loggedIn"):
        raise AuthError("Not logged in. Run `claude /login`.")
    method = s.get("authMethod")
    if method != "claude.ai":
        raise AuthError(
            f"Auth is {method!r}, not a claude.ai subscription login. "
            f"An API key is probably set, and would bill API credits. "
            f"Unset {' / '.join(API_KEY_VARS)} to fall back to the login."
        )
    if expected_email and s.get("email") != expected_email:
        raise AuthError(
            f"Logged in as {s.get('email')!r}, expected {expected_email!r}. "
            "Run `claude auth logout` then `claude auth login`."
        )
    return s


def ensure_dirs() -> None:
    """Create the sandbox tree. Idempotent."""
    for d in (SANDBOX_DIR, SANDBOX_HOME, SANDBOX_WORKDIR):
        d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        SANDBOX_DIR.chmod(0o700)


def subprocess_env(auth_mode: AuthMode = DEFAULT_AUTH) -> dict[str, str]:
    """Environment for the child process.

    LOGIN keeps your real ``HOME`` so the CLI can reach the claude.ai
    credentials you already have. SANDBOX_TOKEN redirects ``HOME``,
    ``XDG_CONFIG_HOME`` and ``CLAUDE_CONFIG_DIR`` at an isolated directory and
    supplies its own token.
    """
    strip: set[str] = set()
    if auth_mode in SUBSCRIPTION_MODES:
        strip = set(API_KEY_VARS)

    env = {k: v for k, v in os.environ.items() if k not in strip}

    if auth_mode is AuthMode.SANDBOX_TOKEN:
        env["CLAUDE_CONFIG_DIR"] = str(SANDBOX_DIR)
        env["HOME"] = str(SANDBOX_HOME)
        env["XDG_CONFIG_HOME"] = str(SANDBOX_HOME / ".config")
        if SANDBOX_TOKEN_FILE.exists():
            env["CLAUDE_CODE_OAUTH_TOKEN"] = SANDBOX_TOKEN_FILE.read_text(
                encoding="utf-8"
            ).strip()
    return env


def preflight(auth_mode: AuthMode = DEFAULT_AUTH) -> None:
    """Raise before spending anything if this mode cannot work."""
    if not binary_available():
        raise CLIMissing(
            "`claude` is not on PATH. Install Claude Code: "
            "https://code.claude.com/docs/en/quickstart"
        )
    if not is_authed(auth_mode):
        if auth_mode is AuthMode.API_KEY:
            raise AuthError(
                f"AuthMode.API_KEY selected but none of {', '.join(API_KEY_VARS)} "
                "is set. Note that this mode bills Anthropic API credits, not "
                "your subscription -- AuthMode.LOGIN is the default for a reason."
            )
        if auth_mode is AuthMode.SANDBOX_TOKEN:
            raise AuthError(
                f"Sandbox is not authed. Run `conclave setup-token` to mint one "
                f"(looked for {SANDBOX_TOKEN_FILE} and {SANDBOX_CREDENTIALS})."
            )
        raise AuthError(
            "Not logged in to Claude Code. Run `claude /login` in a terminal, "
            "then retry. Do not set an ANTHROPIC_API_KEY to work around this -- "
            "that bills API credits instead of your subscription."
        )
    ensure_dirs()
