# conclave

Run Claude Code headlessly as a local inference layer — named experts,
multi-turn sessions, and bounded concurrency, on your own machine under your
own login.

`claude -p` is already a capable inference endpoint once you strip the coding
agent off it. What it lacks is everything around the call: a way to name and
reuse a configuration, conversations that survive a process, a concurrency
policy grounded in measurement rather than folklore, and a check that tells you
which account is about to be billed. That is what this adds.

```bash
pip install git+https://github.com/YashShelar007/conclave@main
conclave doctor          # what's set up, and whose account pays
```

> Not on PyPI yet — the name `conclave` there belongs to an unrelated project,
> so install from git until a distribution name is settled. The import name and
> the command are `conclave` either way.

```python
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
```

Zero dependencies. Python 3.11+. The only requirement is a working
[Claude Code](https://code.claude.com/docs/en/quickstart) install you have
already logged into.

---

## Scope, and a word about terms

**This runs the first-party `claude` binary on your machine, under your own
interactive login.** It is not a proxy, it exposes no network endpoint, and it
never extracts or replays your credentials. Those three properties are
deliberate and this project intends to keep them.

They matter because Anthropic's
[Consumer Terms §3](https://www.anthropic.com/legal/consumer-terms) restricts

> …access[ing] the Services through automated or non-human means, whether
> through a bot, script, or otherwise

with the carve-out **"except when you are accessing our Services via an
Anthropic API Key or where we otherwise explicitly permit it."** Claude Code's
headless mode is squarely inside that carve-out: Anthropic
[ships and documents it](https://code.claude.com/docs/en/headless), and states
plainly that non-bare `-p` uses your subscription login.

What is *not* inside it is turning a subscription into an API for other
software. Between January and April 2026 Anthropic
[blocked exactly that](https://venturebeat.com/technology/anthropic-cuts-off-the-ability-to-use-claude-subscriptions-with-openclaw-and)
— OpenClaw, OpenCode, Cline, Aider and the OpenAI-compatible proxies. The
common factor in every tool that got cut off was **exposing the subscription as
an endpoint to other programs.**

So conclave will not grow an HTTP server, an OpenAI-compatible shim, or a
credential-forwarding mode, and pull requests adding them will be declined.
This is a scope decision, not legal advice; read the terms yourself and decide
what you are comfortable with. Locality is not the test — the carve-out is.

---

## What this measured that the folklore gets wrong

Every number is reproducible with `conclave bench`; the full tables, with
sample sizes and build versions, are in [FINDINGS.md](FINDINGS.md).

**1. `claude -p` uses your subscription. `--bare` is the one that doesn't.**
The widely repeated claim that `-p` "bypasses OAuth and requires an API key" is
false, and Anthropic's own docs say so: it is `--bare` that
*"doesn't use your subscription login."* conclave therefore never passes
`--bare`, despite it being the faster and officially recommended flag for
scripts.

**2. Prompt caching works, and is worth ~9.5x.** The same 4.4k-token system
prompt across three separate processes:

| | `cache_creation` | `cache_read` | cost |
|---|---:|---:|---:|
| cold | 4434 | 0 | **$0.02670** |
| warm | 243 | 4191 | **$0.00281** |

The cache is server-side and survives process death. This inverts the usual
advice: **a long, stable system prompt is the cheap option.** It is the whole
reason experts exist here.

**3. Concurrency goes much further than "about 5."** One burst per row, no
retries:

| n | wall | errors |
|---:|---:|---:|
| 1 | 6.3s | 0 |
| 5 | 6.8s | 0 |
| 10 | 9.9s | 0 |
| 20 | 12.1s | **0** |

~10x the throughput of serial calls, zero rate-limit errors. 20 is the largest
burst tested, not a discovered ceiling. The default here is 12.

**4. Multi-turn works, two ways.** `--session-id`/`--resume` gives durable
conversations that outlive the process; `--input-format stream-json` gives a
hot process that holds one open. On a three-turn conversation the hot process
was **4.3x cheaper**, because resume re-establishes context every turn.

---

## Experts

An expert bundles everything that makes a call a *kind* of call, behind a name.

```toml
# experts.toml
[triage]
description   = "Sorts inbound support mail into a queue, with a confidence score"
model         = "claude-sonnet-5"
effort        = "low"
system_prompt = '''
You are a support triage specialist. You read one inbound customer message
and assign it to exactly one queue.
...the full rubric, worked examples, and edge cases...
'''

[triage.json_schema]
type = "object"
required = ["queue", "confidence", "rationale"]
additionalProperties = false
# ...
```

```python
from conclave import load_experts, ask_expert

load_experts("experts.toml")
r = ask_expert("triage", ticket_text)
r.structured_output   # {'queue': 'security', 'confidence': 0.98, ...}
```

Because of finding 2, the registry pushes you toward *long* prompts. Put the
whole rubric in the system prompt and only the per-item data in the user
prompt: you get better output and a smaller bill from the same change.

Two ways to get that wrong, both checked:

```bash
conclave experts lint
```

- A prompt too short to cache — every call pays full price.
- A prompt interpolated per call (`{}`, `%s`) — changing the prefix misses the
  cache every single time. Per-call data belongs in the user prompt.

### Running one over a batch

```bash
conclave run --expert triage --in tickets.jsonl --out results.jsonl
```

Bounded concurrency, resumable (a rerun skips ids already in `--out`), and it
warms the cache with one serial call before releasing the rest — so the batch
pays to *create* the cache entry once instead of once per call.

```
5/5 ok, 0 failed | 14.9s wall | $0.0322 | 4/5 warm cache reads
```

### Asking several at once

```python
from conclave import consult

await consult(["security", "performance", "style"], diff_text)
# {'security': Result(...), 'performance': Result(...), 'style': Result(...)}
```

A failing expert returns its exception rather than sinking the panel — the
useful thing about asking five specialists is usually the four who answered.

---

## Sessions

Two kinds, because they fail differently.

```python
from conclave import Session, LiveSession

# Durable: on disk, resumable from another process, another day, another cwd.
s = Session(system_prompt=RUBRIC)
s.ask("Here is the first document.")
print(s.id)                          # save this
later = Session.resume(saved_id, system_prompt=RUBRIC)

# Hot: one long-lived process. No per-turn startup, 4.3x cheaper, dies with
# the process.
with LiveSession(system_prompt=RUBRIC) as live:
    live.ask("First document.")
    live.ask("Now compare it to the second.")
```

For many concurrent conversations, `SessionPool` keeps *n* hot processes and
hands them out:

```python
async with SessionPool(size=4, system_prompt=RUBRIC) as pool:
    async with pool.acquire() as session:
        turn = await session.ask("...")
```

A pooled session keeps its history between checkouts — that is the point when
you want shared accumulated context, and a bug when you don't. Pass
`reset_between=True` for a fresh process per checkout.

> **Cost gotcha.** The two paths report `total_cost_usd` differently — per call
> for durable sessions, **cumulative** for live ones. Summing the raw field
> across a live session triple-counts. `Turn.cost_usd` is always the
> incremental figure; the raw one stays on `turn.result.cost_usd`.

---

## Which account am I billing?

The expensive failure mode is silent: an `ANTHROPIC_API_KEY` in the environment
outranks your subscription login, and the call succeeds identically while
billing API credits. [claude-code#37686](https://github.com/anthropics/claude-code/issues/37686)
reports $1,800 in two days from exactly this.

Two defences. conclave strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
from the subprocess environment on every subscription call, and:

```bash
conclave doctor
```

```
  [ok  ] claude binary  2.1.237 (Claude Code)
  [ok  ] api key        no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in the environment
  [ok  ] account        you@example.com (max, via claude.ai)
  [ok  ] state dir      ~/.conclave
  [ok  ] live probe     claude-sonnet-5 answered in 3.4s (in=282 out=4 cost=$0.00091)
```

In code, at the top of any batch job:

```python
from conclave import assert_account
assert_account("you@example.com")   # raises on wrong account, or an API key takeover
```

The live probe is not decoration. Anthropic's docs say `--bare` *"will become
the default for `-p` in a future release"*, and bare mode never reads OAuth. If
that lands, this whole approach stops working — the probe finds out with a real
call rather than guessing from a version string.

### Auth modes

| Mode | Uses | Setup | Billed to |
|---|---|---|---|
| `LOGIN` **(default)** | the `claude /login` you already did | none | your subscription |
| `SANDBOX_TOKEN` | its own long-lived token, isolated `HOME` | `conclave setup-token` | your subscription |
| `API_KEY` | `ANTHROPIC_API_KEY` | set the variable | API credits |

`API_KEY` works but is never selected implicitly — you have to name it.

---

## What you don't get

conclave is not a drop-in for the Messages API. Check here before promising a
caller a feature.

| | | |
|---|---|---|
| model choice | **yes** | aliases and full ids both resolve |
| system prompt | **yes** | replaces the agent prompt entirely |
| reasoning depth | **yes** | `effort="low".."max"` — the lever, not a token budget |
| JSON schema output | **yes** | enforced server-side, auto-retries bad JSON |
| per-call spend cap | **yes** | `max_budget_usd` |
| usage + cost reporting | **yes** | a list-price *estimate*, not a charge |
| multi-turn | **yes** | `Session` and `LiveSession` |
| prompt caching | **automatic** | no breakpoint control, but see finding 2 |
| model fallback | **yes** | `fallback_model` |
| images | **conditional** | file on disk, and allow `tools="Read"` for that call |
| streaming | **not wired** | the CLI supports it; this does not expose it yet |
| `max_tokens` | **no** | no such flag. Constrain via the prompt |
| `temperature` / `top_p` | **no** | also rejected by the Opus 5 / Sonnet 5 API |
| `stop_sequences` | **no** | |
| token counting endpoint | **no** | |
| Batch API (50% off) | **no** | |

Latency is ~3–9s per call. If you need sub-second responses, `max_tokens`
control, or the Batch API, use the `anthropic` SDK with an API key — and say so
plainly rather than working around it here.

---

## Prior art

This is a well-trodden idea and this repo is not the first crack at it. If a
different shape suits you better:

| Project | Shape | Notes |
|---|---|---|
| [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) | first-party | Python and TypeScript. The supported path for agentic work. Start here unless you specifically want inference-shaped calls |
| [RichardAtCT/claude-code-openai-wrapper](https://github.com/RichardAtCT/claude-code-openai-wrapper) | OpenAI-compatible server | What conclave deliberately is not — see *Scope* above |
| [dtzp555-max/ocp](https://github.com/dtzp555-max/ocp) | OpenAI-compatible server | LAN auth, per-key quotas, response cache |

The difference conclave is going for is not features, it is **verified claims**
— every number in [FINDINGS.md](FINDINGS.md) carries its sample size and the
build it came from, and `conclave bench` re-runs them on your machine.

---

## Gotchas

1. **Auth failure returns `subtype: "success"` and exit code 0.** `is_error` is
   the only reliable signal. Handled; don't "simplify" that check.
2. **Never drop the lockdown flags.** A plain `claude -p` costs ~88x more —
   verify on your own machine with `conclave doctor --lockdown`.
3. **Never add `--bare`.** It forces API-key auth and never reads OAuth.
4. **stdout is pure JSON; warnings go to stderr.** Don't merge the streams.
5. **`result_text` vs `structured_output`.** With a schema, read
   `structured_output` — it is already a parsed dict.
6. **A long session is a growing prompt.** Input tokens climb every turn.

---

## Migrating from `claude_cli.py`

This project began as a single file called `claude_cli.py`. That module still
exists as a shim, so `from claude_cli import ask, ask_json, run_claude_cli_sync`
keeps working, with a `DeprecationWarning`. It will be removed in 1.0.

| old | new |
|---|---|
| `run_claude_cli_sync(system_prompt=…, user_prompt=…, …)` | `run_sync(Call(…))` |
| `run_claude_cli(...)` | `await run(Call(…))` |
| `ClaudeCLIError` | `ConclaveError` (still exported under the old name) |
| `ClaudeResult` | `Result` |
| `r.result_text`, `r.total_cost_usd` | `r.text`, `r.cost_usd` (old names still work) |
| `sandbox_ready()` | `ready()` |
| `./setup.sh` | `conclave setup-token` |

State moved from `./data` and `./logs` to `~/.conclave`, overridable with
`CONCLAVE_HOME`.

---

## Contributing

The most valuable contribution is usually a **measurement**, not a patch — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the standard every empirical claim has
to meet, and [ROADMAP.md](ROADMAP.md) for what is planned and what is
deliberately out of scope.

[MIT](LICENSE).
