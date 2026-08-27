"""Expert registry parsing, and the cache lint."""

from __future__ import annotations

import pytest

from convene.config import CACHE_MIN_TOKENS_HINT, HARNESS_OVERHEAD_TOKENS
from convene.errors import ConveneError, ExpertNotFound
from convene.experts import Expert, Registry, estimate_tokens

LONG = "You classify documents against a detailed rubric. " * 120
SHORT = "Be terse."


def test_parses_a_registry():
    registry = Registry.from_toml_text(
        """
        [scorer]
        description   = "Scores things"
        model         = "claude-sonnet-5"
        effort        = "low"
        system_prompt = "You score things."
        """
    )
    assert len(registry) == 1
    scorer = registry.get("scorer")
    assert scorer.model == "claude-sonnet-5"
    assert scorer.effort == "low"
    assert "scorer" in registry


def test_unknown_key_is_an_error_not_a_warning():
    """A typo'd `modle` that silently left you on the default model would be
    an expensive thing to learn from a bill."""
    with pytest.raises(ConveneError, match="unknown key"):
        Registry.from_toml_text('[a]\nsystem_prompt = "x"\nmodle = "claude-sonnet-5"\n')


def test_missing_system_prompt_is_an_error():
    with pytest.raises(ConveneError, match="missing system_prompt"):
        Registry.from_toml_text('[a]\nmodel = "claude-sonnet-5"\n')


def test_bare_top_level_value_is_rejected():
    with pytest.raises(ConveneError, match=r"must be a \[table\]"):
        Registry.from_toml_text('name = "not a table"\n')


def test_missing_expert_names_the_alternatives():
    registry = Registry.from_toml_text('[alpha]\nsystem_prompt = "x"\n')
    with pytest.raises(ExpertNotFound, match="alpha"):
        registry.get("beta")


def test_json_schema_survives_toml_nesting():
    registry = Registry.from_toml_text(
        """
        [triage]
        system_prompt = "Route it."

        [triage.json_schema]
        type = "object"
        required = ["queue"]

        [triage.json_schema.properties.queue]
        type = "string"
        """
    )
    schema = registry.get("triage").json_schema
    assert schema["type"] == "object"
    assert schema["properties"]["queue"]["type"] == "string"


# ---- the cache lint --------------------------------------------------------
def test_short_prompt_is_flagged_as_uncacheable():
    expert = Expert(name="tiny", system_prompt=SHORT)
    assert expert.cacheable is False
    problems = Registry({"tiny": expert}).lint()
    assert len(problems) == 1
    assert "cache" in problems[0]


def test_long_prompt_is_cacheable():
    assert Expert(name="big", system_prompt=LONG).cacheable is True
    assert Registry({"big": Expert(name="big", system_prompt=LONG)}).lint() == []


def test_cacheable_accounts_for_harness_overhead():
    """The cache threshold applies to the whole prompt, not just the system
    prompt, so the harness's own tokens count toward it."""
    just_under = "x " * int(
        (CACHE_MIN_TOKENS_HINT - HARNESS_OVERHEAD_TOKENS + 20) * 2.9 / 2
    )
    assert Expert(name="e", system_prompt=just_under).cacheable is True


def test_interpolation_markers_are_flagged():
    """A per-call placeholder changes the cache prefix and misses every time."""
    expert = Expert(name="bad", system_prompt=LONG + " Today is {}.")
    problems = Registry({"bad": expert}).lint()
    assert any("interpolation" in p for p in problems)


def test_estimator_is_conservative():
    """It must under-estimate tokens, so the lint errs toward warning."""
    # 2.9 chars/token; real structured prompts measured ~2.2, so the estimate
    # is lower than reality. False warnings are safe; false reassurance is not.
    assert estimate_tokens("a" * 2900) <= 1000


def test_call_for_carries_every_expert_setting():
    expert = Expert(
        name="e",
        system_prompt=LONG,
        model="claude-haiku-4-5",
        effort="high",
        max_budget_usd=0.25,
        fallback_model="claude-sonnet-5",
        json_schema={"type": "object"},
    )
    call = expert.call_for("input")
    assert call.model == "claude-haiku-4-5"
    assert call.effort == "high"
    assert call.max_budget_usd == 0.25
    assert call.fallback_model == "claude-sonnet-5"
    assert call.json_schema == {"type": "object"}
    assert call.log_tag == "e"
    assert call.user_prompt == "input"


def test_overrides_win_over_expert_defaults():
    expert = Expert(name="e", system_prompt=LONG, model="claude-opus-5")
    assert expert.call_for("x", model="claude-haiku-4-5").model == "claude-haiku-4-5"
