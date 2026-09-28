#!/usr/bin/env bash
# Re-record the README demo from the repo root:
#
#   bash docs/demo/record.sh
#
# Needs a logged-in Claude Code, uv, jq and ffmpeg. Makes three real calls to the
# `triage` expert in examples/experts.toml (about $0.02 at list price, drawn from
# your subscription). Writes docs/demo/convene.{cast,gif,mp4,png}.
#
# Everything runs in /tmp/convene-demo with a stripped environment, so the only
# path that can reach the screen is that one. HOME stays real because that is
# where Claude Code keeps its login; nothing convene writes goes there
# (CONVENE_HOME points into the stage). Check every frame before committing:
# the ledger path and the Claude Code version are the only machine details shown.
set -euo pipefail

demo="$(cd "$(dirname "$0")" && pwd)"
stage=/tmp/convene-demo
claude_bin="$(dirname "$(command -v claude)")"

rm -rf "$stage"
mkdir -p "$stage"
cp "$demo/../../examples/experts.toml" "$demo/tickets.jsonl" "$demo/demo.sh" "$stage/"
uv venv -q --seed --python '>=3.11' "$stage/.venv"

cd "$stage"
uvx asciinema@2.4.0 rec --overwrite -q --cols 80 --rows 26 \
  -c "env -i HOME=$HOME USER=$USER TERM=xterm-256color LANG=en_US.UTF-8 \
      PATH=$stage/.venv/bin:$claude_bin:/usr/bin:/bin \
      CONVENE_HOME=$stage/state PIP_DISABLE_PIP_VERSION_CHECK=1 \
      bash demo.sh" \
  "$demo/convene.cast"

cd "$demo"
uv run render.py convene.cast
