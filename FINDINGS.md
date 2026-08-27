# Findings

Measured behaviour of `claude -p` used as an inference endpoint. Everything
here carries its sample size and the build it came from, because this document
is the product — the code is a thin wrapper around these facts.

Reproduce any of it with:

```bash
conclave bench
```

**Environment for every measurement below unless stated otherwise:** Claude
Code **2.1.237**, macOS 15 (arm64), subscription auth (`authMethod:
claude.ai`, Max plan), model `claude-sonnet-5`, `effort=low`, calls locked down
with the flag set in [`runtime.py`](src/conclave/runtime.py).

---

## 1. `claude -p` uses your subscription login. `--bare` does not.

This is the most widely repeated wrong claim about headless Claude Code, and
it appears in blog posts and search summaries as *"the `-p` flag bypasses OAuth
and requires an `ANTHROPIC_API_KEY`."*

It is false, and Anthropic's own documentation says which half is true. From
[the headless docs](https://code.claude.com/docs/en/headless), on `--bare`:

> Set `ANTHROPIC_API_KEY` before running it, because bare mode doesn't use your
> subscription login.

> In bare mode, Claude Code never reads OAuth credentials or the system keychain.

So the API-key requirement belongs to `--bare`, not to `-p`. A plain `-p` run
reads your OAuth login normally. Every call in this document was made with no
API key in the environment (n≈150 calls, all succeeded).

**This is why conclave never passes `--bare`,** despite it being faster to
start and despite Anthropic recommending it for scripted calls.

### The forward risk

The same page says:

> `--bare` is the recommended mode for scripted and SDK calls, and will become
> the default for `-p` in a future release.

If that lands and there is no opt-out, subscription auth through this library
breaks. `conclave doctor` probes for it with a real call rather than comparing
version strings, because the failure will not announce itself.

---

## 2. The prompt cache works, and it is worth ~9.5x

Same ~4.4k-token system prompt, three **separate processes**, no session
resumption, ~30s apart (n=1 per row):

| | `cache_creation` | `cache_read` | `input_tokens` | cost | elapsed |
|---|---:|---:|---:|---:|---:|
| cold | 4434 | 0 | 2 | **$0.02670** | 7.1s |
| warm | 243 | 4191 | 2 | **$0.00281** | 6.5s |
| warm | 243 | 4191 | 2 | **$0.00281** | 6.5s |

**9.5x cheaper**, and the cache survives process death because it lives
server-side keyed on the prompt prefix.

The practical consequence inverts ordinary prompt advice: a long, *stable*
system prompt is the cheap option, and a short one you retune per call is the
expensive one. This is the entire argument for the expert registry.

Confirmed independently on a real workload — the `triage` expert in
[`examples/experts.toml`](examples/experts.toml), 5 support tickets, warm-up
enabled (n=1 run):

```
t1  cache_read=   0   $0.01311     <- cold, creates the entry
t2  cache_read=1504   $0.00469
t3  cache_read=1504   $0.00454
t4  cache_read=1504   $0.00469
t5  cache_read=1504   $0.00520
```

2.8x on a prompt a quarter the size of the synthetic one. The effect scales
with system-prompt length.

### Where the cache starts

Sweeping one system prompt's length, one call per size (n=1 per row):

| system prompt chars | total `input_tokens` | `cache_creation` | cached? |
|---:|---:|---:|:--|
| 285 | 354 | 0 | no |
| 570 | 434 | 0 | no |
| 1140 | 594 | 0 | no |
| 1710 | 754 | 0 | no |
| 2280 | 914 | 0 | no |
| 3420 | 2 | 1232 | **yes** |
| 5700 | 2 | 1872 | yes |
| 14820 | 2 | 241 (+4191 read) | yes |

The transition sits between **914 and 1232 total prompt tokens**, consistent
with the 1024-token minimum Anthropic documents for Sonnet and Opus. Haiku's
documented minimum is 2048; **that has not been measured here.**

Two things this table shows that are easy to miss:

- The threshold applies to the **whole prompt**, not the system prompt alone.
  A locked-down call carries ~150 tokens of harness before your text.
- Once a cache entry exists, `input_tokens` collapses to **2**. Almost the
  entire prompt moves into the cache accounting.

### Chars per token, and why the lint under-estimates

The sweep above gives 2.78–3.04 chars/token, but it repeats one sentence, and
repetitive text tokenises unusually well. The structured `triage` prompt —
markdown headings, short lines, lists — measured **2.2 chars/token** (2976
chars against ~1354 system tokens).

`conclave experts lint` uses 2.9, which therefore *under*-estimates real
prompts. That is deliberate: a false "might be too short to cache" warning
costs nothing, while false reassurance costs money on every call.

---

## 3. Concurrency runs much further than "about 5"

Identical trivial calls, one burst per row, no retries (n=1 burst per row):

| n | wall | per-call min / med / max | errors |
|---:|---:|---:|---:|
| 1 | 6.3s | 6.3 / 6.3 / 6.3 | 0 |
| 5 | 6.8s | 6.4 / 6.6 / 6.8 | 0 |
| 10 | 9.9s | 7.0 / 7.2 / 9.8 | 0 |
| 20 | 12.1s | 8.9 / 10.4 / 12.0 | 0 |

Throughput climbs from 0.16 to 1.65 calls/second — about **10x** — with zero
rate-limit errors. Latency degrades gracefully rather than failing.

**This is not a discovered ceiling.** Nobody has found where it actually
breaks; 20 is simply the largest burst tested. conclave defaults to 12 as a
deliberate midpoint that leaves headroom for the interactive Claude Code
sharing the same rate-limit pool. If you find the real limit,
[open an issue](https://github.com/YashShelar007/conclave/issues).

Each concurrent call is a full Node process, so local RAM becomes the binding
constraint before long.

---

## 4. Multi-turn works, two ways, and they cost differently

Three-turn conversation, "my favourite number is 41" → "+1?" → "×2?". Both
kinds answered 42 then 82, so both retained state (n=1 each):

| | turn 1 | turn 2 | turn 3 | total |
|---|---:|---:|---:|---:|
| `LiveSession` (one hot process) | 3.2s / $0.00099 | 2.3s / $0.00113 | 5.8s / $0.00130 | **$0.00342** |
| `Session` (`--session-id` + `--resume`) | 3.6s / $0.00257 | 3.4s / $0.00840 | 3.2s / $0.00388 | **$0.01485** |

The hot process was **4.3x cheaper** for the same conversation, because
`--resume` re-establishes context from disk on every turn.

Use `Session` anyway when the conversation must outlive the program — it is on
disk and resumable tomorrow, from any working directory since Claude Code
2.1.223. Use `LiveSession` when it does not.

### The cost-reporting trap

The two paths report `total_cost_usd` differently, and neither documents it:

```
LiveSession   0.000987 -> 0.002121 -> 0.003417    cumulative per session
Session       0.000990 -> 0.001130 -> 0.001300    per call
```

Summing the raw field across a live session's turns **triple-counts**.
conclave's `Turn.cost_usd` is always the incremental cost; the raw figure stays
on `turn.result.cost_usd`. Pinned by
[`tests/test_sessions.py`](tests/test_sessions.py).

Input tokens grow as history accumulates (299 → 363 → 417 on a trivial
conversation). A long session is a growing prompt, not a free one.

---

## 5. Auth failure does not look like a failure

The single nastiest behaviour in this interface. On an expired or missing
login, the CLI returns:

```json
{"subtype": "success", "is_error": true, "result": "Not logged in · Please run /login"}
```

…and exits **0**.

Neither the exit code nor `subtype` catches it. `is_error` is the only reliable
signal, which is why `decode_envelope` checks `subtype != "success" or
is_error`. That same condition also catches `error_*` subtypes such as
`error_max_structured_output_retries`.

Do not "simplify" it to `subtype == "success"`. Pinned by
[`tests/test_runtime.py`](tests/test_runtime.py).

---

## 6. The lockdown flags are worth ~88x

Measured on an identical one-word prompt (n=1, carried forward from this
project's predecessor and re-checkable with `conclave doctor --lockdown`):

```
locked down   in=  144   cache_create=    0   cost=$0.00051
plain -p      in=    2   cache_create= 9504   cost=$0.04478     88x
```

A default `claude -p` loads the full agent system prompt, every tool
definition, your skills, MCP servers, project settings and `CLAUDE.md`. The gap
depends on how much you have configured, so re-measure on your own machine:

```bash
conclave doctor --lockdown
```

---

## What has not been measured

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

1. **The real concurrency ceiling.** n=20 is the largest burst tried.
2. **Haiku's cache threshold.** Documented as 2048 tokens; unverified here.
3. **Cache TTL on this path.** Warm reads were ~30s apart. How long an entry
   survives is unknown.
4. **Linux and Windows.** Every number above is macOS arm64.
5. **Rate-limit behaviour at the weekly cap.** No measurement of what a
   subscription limit looks like in the envelope.
6. **Whether `--bare` has become the `-p` default** in any build after 2.1.237.
