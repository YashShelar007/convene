"""Named specialists, and why a long system prompt is the cheap option.

An expert bundles everything that makes a call a *kind* of call -- system
prompt, model, effort, output schema, budget -- behind a name, so calling code
says what it wants rather than how to get it::

    from convene import ask_expert
    ask_expert("scorer", listing_text)

The cost argument
-----------------
This is not only organisation. Anthropic's prompt cache applies to the
subscription path, and it is worth a great deal. Measured on Claude Code
2.1.237, the same ~4.4k-token system prompt sent as three separate processes:

    cold    cache_create=4434  cache_read=   0   cost=$0.02670
    warm    cache_create= 243  cache_read=4191   cost=$0.00281
    warm    cache_create= 243  cache_read=4191   cost=$0.00281

That is **9.5x cheaper**, and it survives process death, because the cache
lives server-side keyed on the prompt prefix.

The consequence inverts the usual advice. A short, hand-tuned system prompt
that changes per call is billed in full every time. A long, *stable* one --
full rubric, worked examples, edge cases, output conventions -- is billed once
and then read back at a discount. Under an expert registry you get better
output and a smaller bill from the same change.

Two rules follow, and :meth:`Registry.lint` checks both:

1. **Keep the prompt stable.** Interpolating anything per-call (a timestamp, a
   record id, a user name) changes the prefix and misses the cache every time.
   Per-call data belongs in the user prompt.
2. **Keep it long enough to cache.** Below roughly 1k tokens the cache does not
   engage at all, and a 40-token system prompt never cached in testing.
"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .auth import DEFAULT_AUTH, AuthMode
from .config import (
    CACHE_MIN_TOKENS_HINT,
    CHARS_PER_TOKEN_ESTIMATE,
    DEFAULT_CONCURRENCY,
    DEFAULT_MODEL,
    HARNESS_OVERHEAD_TOKENS,
    SUBPROCESS_TIMEOUT_S,
    EffortLevel,
)
from .errors import ConveneError, ExpertNotFound
from .runtime import Call, Result, run, run_sync


def estimate_tokens(text: str) -> int:
    """Very rough token count, for cache-eligibility warnings only.

    Never used for billing or for any decision beyond emitting a lint warning.
    """
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE)


@dataclass(frozen=True)
class Expert:
    """A named specialist: everything about a call except the input."""

    name: str
    system_prompt: str
    description: str = ""
    model: str = DEFAULT_MODEL
    effort: EffortLevel | None = None
    #: JSON Schema enforced server-side. The CLI auto-retries invalid JSON.
    json_schema: dict | None = None
    #: Hard per-call spend ceiling, passed to ``--max-budget-usd``.
    max_budget_usd: float | None = None
    #: Model to fall back to when the primary is overloaded or unavailable.
    fallback_model: str | None = None
    #: Built-in tools to allow. Empty means none, which is what you want unless
    #: the expert needs to read an image off disk.
    tools: str = ""
    timeout_s: float = SUBPROCESS_TIMEOUT_S

    def call_for(self, user_prompt: str, **overrides: Any) -> Call:
        """Build the :class:`~convene.runtime.Call` this expert represents."""
        call = Call(
            user_prompt=user_prompt,
            system_prompt=self.system_prompt,
            model=self.model,
            json_schema=self.json_schema,
            effort=self.effort,
            max_budget_usd=self.max_budget_usd,
            fallback_model=self.fallback_model,
            tools=self.tools,
            timeout_s=self.timeout_s,
            log_tag=self.name,
        )
        return replace(call, **overrides) if overrides else call

    @property
    def cacheable(self) -> bool:
        """Is the prompt long enough for the cache to engage?

        The cache threshold applies to the whole prompt, so the harness
        overhead counts toward it alongside the system prompt.
        """
        return (
            estimate_tokens(self.system_prompt) + HARNESS_OVERHEAD_TOKENS
            >= CACHE_MIN_TOKENS_HINT
        )

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> Expert:
        """Build from a parsed TOML table, rejecting unknown keys.

        Unknown keys are an error rather than a warning: a typo'd ``modle``
        that silently left you on the default model would be an expensive
        thing to discover from a bill.
        """
        known = {f.name for f in cls.__dataclass_fields__.values()} - {"name"}
        unknown = set(data) - known
        if unknown:
            raise ConveneError(
                f"expert {name!r} has unknown key(s): {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(sorted(known))}"
            )
        if "system_prompt" not in data:
            raise ConveneError(f"expert {name!r} is missing system_prompt")
        return cls(name=name, **data)


@dataclass
class Registry:
    """A set of experts, usually loaded from a TOML file."""

    experts: dict[str, Expert] = field(default_factory=dict)
    source: Path | None = None

    def __iter__(self) -> Iterator[Expert]:
        return iter(self.experts.values())

    def __len__(self) -> int:
        return len(self.experts)

    def __contains__(self, name: object) -> bool:
        return name in self.experts

    def add(self, expert: Expert) -> Expert:
        """Register an expert, replacing any of the same name."""
        self.experts[expert.name] = expert
        return expert

    def get(self, name: str) -> Expert:
        """Look up an expert, with a useful error if it is missing."""
        try:
            return self.experts[name]
        except KeyError:
            known = ", ".join(sorted(self.experts)) or "(none registered)"
            where = f" in {self.source}" if self.source else ""
            raise ExpertNotFound(
                f"no expert named {name!r}{where}. Registered: {known}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self.experts)

    # ---- loading ----------------------------------------------------------
    @classmethod
    def from_toml_text(cls, text: str, source: Path | None = None) -> Registry:
        """Parse a registry from TOML source.

        Each top-level table is one expert, keyed by its name::

            [scorer]
            description  = "Scores a job listing against a candidate profile"
            model        = "claude-sonnet-5"
            effort       = "low"
            system_prompt = '''
            ...
            '''
        """
        data = tomllib.loads(text)
        registry = cls(source=source)
        for name, table in data.items():
            if not isinstance(table, dict):
                raise ConveneError(
                    f"top-level key {name!r} is a bare value; every entry in an "
                    "expert registry must be a [table]"
                )
            registry.add(Expert.from_dict(name, table))
        return registry

    @classmethod
    def load(cls, path: str | Path) -> Registry:
        """Load a registry from a TOML file."""
        p = Path(path).expanduser()
        if not p.exists():
            raise ConveneError(f"expert registry not found: {p}")
        return cls.from_toml_text(p.read_text(encoding="utf-8"), source=p)

    # ---- quality checks ---------------------------------------------------
    def lint(self) -> list[str]:
        """Report prompts that will waste money. Empty list means clean.

        Checks the two cache rules from the module docstring: prompts too short
        to cache, and prompts that look like they are interpolated per call.
        """
        problems: list[str] = []
        for expert in self:
            tokens = estimate_tokens(expert.system_prompt)
            if not expert.cacheable:
                needed = CACHE_MIN_TOKENS_HINT - HARNESS_OVERHEAD_TOKENS
                problems.append(
                    f"{expert.name}: system prompt is ~{tokens} tokens; roughly "
                    f"{needed} are needed before the prompt cache engages. Every "
                    f"call pays full price. Consider folding your rubric, worked "
                    f"examples and edge cases into it -- with caching, a longer "
                    f"prompt is usually the cheaper one."
                )
            for marker in ("{}", "{0}", "%s"):
                if marker in expert.system_prompt:
                    problems.append(
                        f"{expert.name}: system prompt contains {marker!r}, which "
                        f"suggests per-call interpolation. That changes the cache "
                        f"prefix on every call and misses the cache every time. "
                        f"Move per-call data into the user prompt."
                    )
                    break
        return problems


# ---- Default registry ------------------------------------------------------
_default = Registry()


def default_registry() -> Registry:
    """The process-wide registry used by the module-level helpers."""
    return _default


def register(expert: Expert) -> Expert:
    """Add an expert to the default registry."""
    return _default.add(expert)


def load_experts(path: str | Path, *, replace_existing: bool = False) -> Registry:
    """Load a TOML registry into the default registry."""
    loaded = Registry.load(path)
    if replace_existing:
        _default.experts.clear()
    _default.experts.update(loaded.experts)
    _default.source = loaded.source
    return _default


# ---- Calling ---------------------------------------------------------------
def ask_expert(
    name: str,
    user_prompt: str,
    *,
    registry: Registry | None = None,
    auth_mode: AuthMode = DEFAULT_AUTH,
    **overrides: Any,
) -> Result:
    """Consult one expert, blocking."""
    expert = (registry or _default).get(name)
    return run_sync(expert.call_for(user_prompt, **overrides), auth_mode=auth_mode)


async def ask_expert_async(
    name: str,
    user_prompt: str,
    *,
    registry: Registry | None = None,
    auth_mode: AuthMode = DEFAULT_AUTH,
    **overrides: Any,
) -> Result:
    """Consult one expert, async."""
    expert = (registry or _default).get(name)
    return await run(expert.call_for(user_prompt, **overrides), auth_mode=auth_mode)


async def consult(
    names: Iterable[str],
    user_prompt: str,
    *,
    registry: Registry | None = None,
    auth_mode: AuthMode = DEFAULT_AUTH,
    concurrency: int = DEFAULT_CONCURRENCY,
    **overrides: Any,
) -> dict[str, Result | Exception]:
    """Put the same question to several experts at once.

    Returns a name-keyed dict. A failing expert yields its exception rather
    than sinking the whole panel, because the useful thing about asking five
    specialists is usually the four who answered.
    """
    reg = registry or _default
    experts = [reg.get(n) for n in names]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(expert: Expert) -> Result | Exception:
        async with sem:
            try:
                return await run(
                    expert.call_for(user_prompt, **overrides), auth_mode=auth_mode
                )
            except Exception as e:
                return e

    results = await asyncio.gather(*(one(e) for e in experts))
    return dict(zip((e.name for e in experts), results, strict=True))


async def map_expert(
    name: str,
    prompts: Iterable[str],
    *,
    registry: Registry | None = None,
    auth_mode: AuthMode = DEFAULT_AUTH,
    concurrency: int = DEFAULT_CONCURRENCY,
    warm: bool = True,
    **overrides: Any,
) -> list[Result | Exception]:
    """Run one expert over many inputs, in order, with bounded concurrency.

    ``warm=True`` sends the first prompt alone before releasing the rest. That
    single serial call creates the prompt cache entry, so the remaining calls
    read it warm instead of each paying to create it. On a 4.4k-token system
    prompt that is the difference between $0.0267 and $0.0028 per call, and it
    costs one call's worth of latency once.
    """
    reg = registry or _default
    expert = reg.get(name)
    items = list(prompts)
    if not items:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(prompt: str) -> Result | Exception:
        async with sem:
            try:
                return await run(
                    expert.call_for(prompt, **overrides), auth_mode=auth_mode
                )
            except Exception as e:
                return e

    if warm and len(items) > 1 and expert.cacheable:
        first = await one(items[0])
        rest = await asyncio.gather(*(one(p) for p in items[1:]))
        return [first, *rest]

    return list(await asyncio.gather(*(one(p) for p in items)))
