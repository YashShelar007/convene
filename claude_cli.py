"""Backwards-compatible shim for the pre-rename module.

This project used to be a single file called ``claude_cli.py`` with an API of
``ask`` / ``ask_json`` / ``run_claude_cli_sync`` / ``run_claude_cli``. That API
still works, so existing callers doing::

    import sys
    sys.path.insert(0, "/path/to/this/repo")
    from claude_cli import ask, ask_json, run_claude_cli_sync

do not need to change anything today. New code should import from ``conclave``.

The old functions took a flat pile of keyword arguments; the new engine takes a
:class:`conclave.Call`. This module translates between them.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any

# Work whether or not the package has been installed.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from conclave import (  # noqa: E402
    AuthMode,
    Call,
    Result,
    ask,
    ask_json,
    assert_account,
    is_authed,
    ready,
    run,
    run_sync,
    whoami,
)
from conclave.config import CHEAP_MODEL, DEFAULT_MODEL  # noqa: E402
from conclave.errors import ConclaveError  # noqa: E402

#: The old name for the error type. ``ConclaveError`` is the base class of
#: every error this library raises, so ``except ClaudeCLIError`` still catches
#: everything it used to.
ClaudeCLIError = ConclaveError
ClaudeResult = Result

__all__ = [
    "CHEAP_MODEL",
    "DEFAULT_MODEL",
    "AuthMode",
    "ClaudeCLIError",
    "ClaudeResult",
    "ask",
    "ask_json",
    "assert_account",
    "run_claude_cli",
    "run_claude_cli_sync",
    "sandbox_authed",
    "sandbox_ready",
    "whoami",
]

_WARNED = False


def _warn_once() -> None:
    global _WARNED
    if not _WARNED:
        warnings.warn(
            "claude_cli is the old module name for this project; it now wraps "
            "`conclave`. Import from `conclave` instead -- this shim will be "
            "removed in 1.0.",
            DeprecationWarning,
            stacklevel=3,
        )
        _WARNED = True


def _to_call(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    json_schema: dict | None = None,
    effort: str | None = None,
    max_budget_usd: float | None = None,
    log_tag: str = "llm",
    log_context: str = "",
    timeout_s: float = 120.0,
) -> Call:
    return Call(
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        model=model,
        json_schema=json_schema,
        effort=effort,  # type: ignore[arg-type]
        max_budget_usd=max_budget_usd,
        log_tag=log_tag,
        log_context=log_context,
        timeout_s=timeout_s,
    )


def run_claude_cli_sync(*, auth_mode: AuthMode = AuthMode.LOGIN, **kwargs: Any) -> Result:
    """Old blocking entry point. See :func:`conclave.run_sync`."""
    _warn_once()
    return run_sync(_to_call(**kwargs), auth_mode=auth_mode)


async def run_claude_cli(
    *, auth_mode: AuthMode = AuthMode.LOGIN, **kwargs: Any
) -> Result:
    """Old async entry point. See :func:`conclave.run`."""
    _warn_once()
    return await run(_to_call(**kwargs), auth_mode=auth_mode)


def sandbox_ready(auth_mode: AuthMode = AuthMode.LOGIN) -> bool:
    """Old name for :func:`conclave.ready`."""
    return ready(auth_mode)


def sandbox_authed(
    auth_mode: AuthMode = AuthMode.LOGIN, *, recheck: bool = False
) -> bool:
    """Old name for :func:`conclave.is_authed`."""
    return is_authed(auth_mode, recheck=recheck)


if __name__ == "__main__":
    from conclave.cli import main

    sys.exit(main(["doctor"]))
