# Roadmap

What is built, what is planned, and what is deliberately excluded. Ordered
roughly by value, not by effort.

Everything here is inside the scope rule in the [README](README.md#scope-and-a-word-about-terms):
local, first-party binary, no network endpoint. Items that would cross that
line are in *Out of scope* at the bottom and will not be built.

---

## Shipped in 0.1

- [x] Locked-down `claude -p` engine, sync and async (~88x cheaper than plain `-p`)
- [x] `is_error` envelope handling — the auth failure that exits 0
- [x] API-key stripping on every subscription call, and `assert_account`
- [x] JSON Schema output, per-call budget caps, model fallback
- [x] **Expert registry** — TOML, named specialists, schema-per-expert
- [x] **Cache lint** — flags prompts too short to cache or interpolated per call
- [x] **Cache warm-up** in batch runs — one serial call creates the entry
- [x] **Durable sessions** via `--session-id` / `--resume`
- [x] **Live sessions** via `--input-format stream-json`, plus `SessionPool`
- [x] Normalised incremental cost across both session kinds
- [x] `consult()` — one question to a panel of experts, failures isolated
- [x] `conclave doctor` — setup, billing account, and a live `--bare` probe
- [x] `conclave bench` — reproduces every number in FINDINGS.md
- [x] `conclave run` — resumable JSONL batches
- [x] `conclave chat` — interactive live session
- [x] `claude_cli.py` back-compat shim

---

## Next

### 1. Cost ledger and budgets — *highest value*
The flat log file answers "what happened"; it cannot answer "how much has this
expert cost me this week" or "stop at $5/day".

- SQLite ledger: one row per call — timestamp, expert, model, tokens, cache
  read/create, cost, tag, session.
- Budget enforcement at three scopes: per call (exists, via
  `--max-budget-usd`), per run, per rolling window. Raise `BudgetError`
  *before* spending, not after.
- `conclave usage --since 7d --by expert` with cache-hit rate per expert,
  which is the number that tells you whether your prompts are structured well.

### 2. Adaptive concurrency
The current default of 12 is a measured midpoint, but it is still a constant.
The `stream-json` output carries `system/api_retry` events with an `error`
field taking values including `rate_limit` and `overloaded` — real backpressure
signal that nothing currently reads.

- Parse `api_retry` events and back off on `rate_limit` / `overloaded`.
- Additive-increase / multiplicative-decrease around the configured ceiling.
- Surface the observed ceiling so FINDINGS.md can finally answer "where does it
  actually break".

### 3. Response cache
Distinct from Anthropic's prompt cache: a local content-addressed store keyed
on `(system_prompt, user_prompt, model, effort, schema)`. Re-running a batch
after fixing a bug in the *consumer* currently re-pays for every call.

- Opt-in, TTL'd, in the SQLite ledger.
- `--no-cache` to force, `conclave cache purge`.

### 4. Streaming
The CLI supports `--include-partial-messages` for token-level deltas. Worth
exposing for `conclave chat` and for any interactive consumer.

- `run_streaming()` yielding text deltas.
- Wire into `chat` so replies appear as they generate.

### 5. Retry and fallback policy
`--fallback-model` handles overload at the CLI level, but nothing retries a
timeout or a transport failure.

- Configurable retry with jittered backoff, per expert.
- Distinguish retryable (`overloaded`, `server_error`, timeout) from terminal
  (`invalid_request`, `authentication_failed`) using the documented
  `api_retry.error` categories.

### 6. Vision
`tools="Read"` plus a file path already works, but it is undocumented here and
untested.

- `ask_image(path, prompt)` helper, a measurement of what the `Read` tool's
  definition costs in tokens, and a test.

### 7. Model-driven panels via `--agents`
The CLI accepts `--agents '{"reviewer": {...}}'` and can fan out to subagents
inside one process. That is a genuinely different shape from `consult()`:
orchestration decided by the model rather than by your code.

- Measure it first. It re-adds the agent system prompt and the Task tool
  definition, so it is likely far more expensive than N parallel locked-down
  calls. **If the measurement says it is not worth it, document that and don't
  build it** — a negative result is a finding.

### 8. Ergonomics
- `conclave experts new <name>` scaffolding a well-shaped, cacheable prompt.
- `conclave run --dry-run` printing what would be called and the estimated cost.
- Structured log output (JSONL) alongside the human-readable line.

---

## Out of scope

Not "later" — these will not be built, and PRs adding them will be declined.
See [CONTRIBUTING.md](CONTRIBUTING.md#scope-what-will-be-declined).

- **HTTP server / OpenAI-compatible endpoint.** The exact shape Anthropic
  blocked across 2026. Everything this library does is reachable in-process.
- **Multi-user or multi-tenant anything.** One machine, one login, one person.
- **Credential extraction, forwarding, or sharing.**
- **Silent API-key fallback.** `AuthMode.API_KEY` stays explicit forever.
- **Vendoring or patching `cli.js`.** conclave calls the official binary and
  breaks honestly when it changes.
- **Circumventing rate limits** by rotating accounts, tokens, or machines.
