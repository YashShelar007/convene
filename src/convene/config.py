"""Paths, defaults, and the numbers that came out of measurement.

Anything here with a comment citing a measurement is load-bearing: it was
chosen from an observed number, not a guess. See FINDINGS.md for the runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

# ---- Where state lives -----------------------------------------------------
# Everything convene writes goes under one root, overridable so several
# projects on one machine can keep separate ledgers and sandboxes.
STATE_ROOT = Path(os.environ.get("CONVENE_HOME", Path.home() / ".convene")).expanduser()

SANDBOX_DIR = STATE_ROOT / "sandbox"
SANDBOX_HOME = SANDBOX_DIR / "fake-home"
SANDBOX_WORKDIR = SANDBOX_DIR / "workdir"
SANDBOX_TOKEN_FILE = SANDBOX_DIR / "oauth-token"
# An interactive `claude /login` run inside the sandbox writes credentials
# here; either this or the token file counts as evidence the sandbox is authed.
SANDBOX_CREDENTIALS = SANDBOX_DIR / ".credentials.json"

LOG_DIR = STATE_ROOT / "logs"
LOG_FILE = LOG_DIR / "convene.log"
LEDGER_FILE = STATE_ROOT / "ledger.sqlite3"

# ---- Models ----------------------------------------------------------------
DEFAULT_MODEL = "claude-opus-5"
CHEAP_MODEL = "claude-sonnet-5"
FAST_MODEL = "claude-haiku-4-5"

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]

# ---- Timing ----------------------------------------------------------------
# A one-turn call is 4-9s in practice (subprocess start plus inference), so 120s
# is generous headroom rather than a tuned value. Long `effort="max"` calls can
# legitimately exceed it -- raise it per call rather than globally.
SUBPROCESS_TIMEOUT_S = 120.0

# A live worker holds a conversation open; the first turn pays process startup
# and later turns do not, but a turn can still be slow. Same ceiling, per turn.
WORKER_TURN_TIMEOUT_S = 180.0

# ---- Concurrency -----------------------------------------------------------
# Measured on Claude Code 2.1.237, subscription auth, claude-sonnet-5 / low
# effort, trivial prompts (n=1/5/10/20, one burst each):
#
#     n= 1  wall= 6.3s  per-call med= 6.3s  errors=0
#     n= 5  wall= 6.8s  per-call med= 6.6s  errors=0
#     n=10  wall= 9.9s  per-call med= 7.2s  errors=0
#     n=20  wall=12.1s  per-call med=10.4s  errors=0
#
# Throughput climbs ~10x from n=1 to n=20 with zero rate-limit errors, so the
# widely repeated "keep it near 5" is too conservative. 12 is a deliberate
# midpoint: most of the throughput, well short of the largest burst tested, and
# it leaves headroom for the interactive Claude Code you are probably also
# running, which shares the same rate-limit pool.
#
# This is a starting point, not a discovered ceiling. Nobody has found the
# actual limit; see FINDINGS.md.
DEFAULT_CONCURRENCY = 12

# Each concurrent call is a full Node process. Past this, you are more likely to
# be limited by local RAM than by Anthropic.
CONCURRENCY_HARD_CAP = 32

# ---- Prompt caching --------------------------------------------------------
# Anthropic's prompt cache has a minimum cacheable prefix. Below it, a repeated
# system prompt is billed in full on every call.
#
# Measured by sweeping one system prompt's length, one call per size,
# claude-sonnet-5 / low effort, Claude Code 2.1.237 (n=1 per row):
#
#     sys chars   total in   cache_create   cached?
#          2280        914              0   no
#          3420          2           1232   yes
#          5700          2           1872   yes
#
# The jump lands between 914 and 1232 total prompt tokens, which matches the
# 1024-token minimum Anthropic documents for Sonnet and Opus. Haiku's
# documented minimum is 2048; that has NOT been measured here.
#
# Note the threshold applies to the whole prompt, not the system prompt alone.
# A locked-down call carries roughly 150 tokens of CLI overhead before your
# text, so a system prompt somewhat under 1024 can still cache.
#
# This gates a lint warning, never behaviour.
CACHE_MIN_TOKENS_HINT = 1024

# Tokens the locked-down harness costs before any of your own text. Measured
# at 144 on a one-word prompt; used only to make the lint estimate honest.
HARNESS_OVERHEAD_TOKENS = 150

# Chars per token. Deliberately conservative, and known to be imprecise.
#
# The sweep above gives 2.78-3.04 chars/token, but it repeats one sentence, and
# repetitive text tokenises unusually well. The structured example expert in
# examples/experts.toml -- markdown headings, short lines, lists -- measured
# 2976 chars against ~1354 system-prompt tokens, or 2.2 chars/token. Real
# prompts land denser than prose.
#
# 2.9 therefore *under*-estimates token counts for structured prompts, which is
# the safe direction for a lint: it warns that a prompt may be too short to
# cache when it might in fact cache. A false warning costs you nothing; false
# reassurance costs you money on every call. An earlier guess of 3.8 was wrong
# in the unsafe direction and mislabelled a genuinely cacheable prompt.
#
# Estimation only -- never used for billing.
CHARS_PER_TOKEN_ESTIMATE = 2.9
