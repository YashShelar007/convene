# Contributing

This repo's product is **verified claims about an undocumented interface**, and
a small library that acts on them. The code is thin; the value is in knowing
which assertions are backed by how much evidence. That shapes everything below.

## Branches

| Branch | What it means |
|---|---|
| `main` | Claims that have been verified, and code that has been run. Treat it as published. |
| `develop` | Integration. New measurements and features land here first. |

- `develop` is the **default branch**. Open PRs against it.
- `main` changes only by PR from `develop`, tagged on merge.
- Maintainers push to `develop` directly for doc fixes and verified findings.
  Outside contributions come as PRs.

Releases are tagged when a **claim set** changes — a new measurement, a
retraction, a corrected derivation — or when the API changes. The version
people cite should match what they read.

## The claim standard

The most useful contribution here is usually a measurement, not a patch. It is
also the easiest thing to get wrong, because a single lucky run looks exactly
like a law.

**Every empirical claim carries its sample size and the build it came from.**

- Good: "cache engages between 914 and 1232 total prompt tokens
  (claude-sonnet-5, Claude Code 2.1.237, macOS arm64, n=1 per size across 8
  sizes)"
- Not good: "the cache threshold is 1024 tokens"

If you can't reach n>1, say so. "Observed once" is a fine claim.
"Anthropic changed X" is not, unless you have both sides of the change.

Claims about *cost* must report the tokens too. `total_cost_usd` is a
client-side list-price estimate, not a bill, and on subscription auth it is an
accounting figure rather than a charge. A cost claim without the token counts
cannot be checked.

## What's most wanted

The open questions are listed at the bottom of [FINDINGS.md](FINDINGS.md). The
highest-value ones:

1. **The real concurrency ceiling.** n=20 is simply the largest burst anyone
   has tried. Where does it actually start erroring, and what does the envelope
   look like when it does?
2. **A Linux or Windows column.** Every number in FINDINGS.md is macOS arm64.
3. **Cache TTL.** Warm reads were ~30s apart. How long does an entry live?
4. **A build newer than 2.1.237** — especially one where `--bare` has become
   the default for `-p`, which would break subscription auth entirely.
5. **Counter-examples.** A run that contradicts a table in FINDINGS.md is worth
   more than another confirmation of it.

Reproduce the existing numbers first:

```bash
convene bench
```

## Scope: what will be declined

convene runs the first-party `claude` binary locally, under the user's own
login. It is not a proxy. Three properties are load-bearing, and PRs that
change any of them will be declined regardless of quality:

- **No network endpoint.** No HTTP server, no OpenAI-compatible shim, no
  daemon that other machines or programs can call. See *Scope* in the README
  for why — this is the exact shape Anthropic blocked in 2026.
- **No credential extraction or forwarding.** The library reads no token files
  it did not create, and sends credentials nowhere.
- **No silent API-key fallback.** `AuthMode.API_KEY` must never be selected
  implicitly, and the key-stripping in `subprocess_env` must not be weakened.

Feature ideas inside scope are in [ROADMAP.md](ROADMAP.md).

## Hard rules for the code

Each corresponds to a way this has already gone wrong, or would.

- **Never pass `--bare`.** It forces API-key auth and never reads OAuth. It
  would silently move billing from a subscription to API credits.
- **Never weaken the `is_error` check.** Auth failure arrives as
  `subtype: "success"` with exit code 0. `subtype == "success"` alone does not
  catch it. Pinned by `tests/test_runtime.py`.
- **Never drop a lockdown flag** without a measurement showing the cost is
  unchanged. They are worth ~88x together.
- **Never strip `--session-id`/`--resume` calls of session persistence.**
  `--no-session-persistence` makes a session unresumable, which defeats the
  point of a durable session.
- **Keep the core dependency-free.** A tool for reducing spend should not drag
  in a tree of transitive dependencies. `tomllib` is why the floor is 3.11.
- **Normalise costs, don't pass them through.** Live sessions report cumulative
  cost, durable ones report per-call. `Turn.cost_usd` is always incremental.
- **Never store prompts or responses in the ledger.** It is an accounting
  record, and it should stay safe to share with someone who should not see the
  data that went through it. Pinned by `tests/test_ledger.py`.
- **A ledger failure must never lose a result the caller already paid for.**
  Recording is best-effort and logs on failure; it does not raise.

## Releasing

Releases go out through PyPI Trusted Publishing — there is no API token in this
repo. The trusted publisher is already configured; the setup it needed is
documented at the top of
[`.github/workflows/release.yml`](.github/workflows/release.yml) for reference.

A release is:

```bash
# on develop, with FINDINGS.md and the version in pyproject.toml both current
gh pr create --base main --head develop
# once merged:
git checkout main && git pull
git tag v0.1.1 && git push origin v0.1.1
```

The workflow refuses to publish if the tag does not match the version in
`pyproject.toml`, so bump that in the same PR.

## Testing a change

```bash
pip install -e ".[dev]"
pytest                 # 46 tests, no spend, no network
ruff check src tests
```

Tests that make real calls are marked `live` and excluded by default. Run them
before anything touching the runtime, sessions, or auth:

```bash
pytest -m live         # a few cents, needs a working `claude` login
```

If you change a documented number, change FINDINGS.md in the same PR and say in
the description which command you actually ran. A number in the docs that no
test or bench command reproduces is a bug.
