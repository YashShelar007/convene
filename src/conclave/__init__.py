"""conclave -- run Claude Code headlessly as a local inference layer.

Wraps the official ``claude`` CLI in non-interactive mode, stripped back from a
coding agent to a plain inference endpoint, and adds the things you need to run
real workloads on it: named experts, multi-turn sessions, bounded concurrency,
and diagnostics that tell you which account is about to be billed.

Quick start::

    from conclave import ask, ask_json

    ask("Name the capital of France in one word.")
    # 'Paris'

    ask_json("Extract: Ada Lovelace, born 1815.", {
        "type": "object",
        "properties": {"name": {"type": "string"}, "born": {"type": "integer"}},
        "required": ["name", "born"],
        "additionalProperties": False,
    })
    # {'name': 'Ada Lovelace', 'born': 1815}

This runs the first-party Claude Code CLI on your own machine, under your own
login. It is not a proxy, it does not expose a network endpoint, and it does
not extract or replay your credentials. See the README on scope and terms
before building anything that changes those three facts.
"""

from __future__ import annotations

from typing import Any

from .auth import (
    DEFAULT_AUTH,
    AuthMode,
    assert_account,
    binary_available,
    cli_version,
    is_authed,
    login_status,
    ready,
    whoami,
)
from .config import (
    CHEAP_MODEL,
    DEFAULT_CONCURRENCY,
    DEFAULT_MODEL,
    FAST_MODEL,
    STATE_ROOT,
)
from .errors import (
    AuthError,
    BudgetError,
    CLIError,
    ConclaveError,
    ExpertNotFound,
    SessionError,
)
from .experts import (
    Expert,
    Registry,
    ask_expert,
    ask_expert_async,
    consult,
    default_registry,
    load_experts,
    map_expert,
    register,
)
from .runtime import Call, Result, run, run_sync
from .sessions import (
    AsyncLiveSession,
    LiveSession,
    Session,
    SessionPool,
    Turn,
)

__version__ = "0.1.0"

# Grouped by concept rather than sorted: this list doubles as the shape of the
# public API, and alphabetising it would scatter each group.
__all__ = [  # noqa: RUF022
    # engine
    "Call",
    "Result",
    "run",
    "run_sync",
    "ask",
    "ask_json",
    "ask_async",
    # experts
    "Expert",
    "Registry",
    "register",
    "load_experts",
    "default_registry",
    "ask_expert",
    "ask_expert_async",
    "consult",
    "map_expert",
    # sessions
    "Session",
    "LiveSession",
    "AsyncLiveSession",
    "SessionPool",
    "Turn",
    # auth
    "AuthMode",
    "DEFAULT_AUTH",
    "whoami",
    "assert_account",
    "login_status",
    "is_authed",
    "ready",
    "binary_available",
    "cli_version",
    # config
    "DEFAULT_MODEL",
    "CHEAP_MODEL",
    "FAST_MODEL",
    "DEFAULT_CONCURRENCY",
    "STATE_ROOT",
    # errors
    "ConclaveError",
    "CLIError",
    "AuthError",
    "BudgetError",
    "ExpertNotFound",
    "SessionError",
    "__version__",
]

_DEFAULT_SYSTEM = "You are a concise, accurate assistant."
_EXTRACT_SYSTEM = "You extract structured data. Follow the schema exactly."


def ask(
    user_prompt: str,
    *,
    system_prompt: str = _DEFAULT_SYSTEM,
    auth_mode: AuthMode = DEFAULT_AUTH,
    **kwargs: Any,
) -> str:
    """Plain text in, plain text out."""
    call = Call(user_prompt=user_prompt, system_prompt=system_prompt, **kwargs)
    return run_sync(call, auth_mode=auth_mode).text or ""


async def ask_async(
    user_prompt: str,
    *,
    system_prompt: str = _DEFAULT_SYSTEM,
    auth_mode: AuthMode = DEFAULT_AUTH,
    **kwargs: Any,
) -> str:
    """Async :func:`ask`."""
    call = Call(user_prompt=user_prompt, system_prompt=system_prompt, **kwargs)
    return (await run(call, auth_mode=auth_mode)).text or ""


def ask_json(
    user_prompt: str,
    schema: dict,
    *,
    system_prompt: str = _EXTRACT_SYSTEM,
    auth_mode: AuthMode = DEFAULT_AUTH,
    **kwargs: Any,
) -> dict[str, Any]:
    """Schema-validated dict out.

    The CLI enforces the schema server-side and auto-retries invalid JSON, so
    no defensive parsing is needed on this side.
    """
    call = Call(
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        json_schema=schema,
        **kwargs,
    )
    data = run_sync(call, auth_mode=auth_mode).structured_output
    if not isinstance(data, dict):
        raise CLIError("claude CLI returned no structured_output")
    return data
