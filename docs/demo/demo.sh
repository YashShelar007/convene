#!/usr/bin/env bash
# The on-screen session behind docs/demo/convene.gif. Run by record.sh, not by hand.
# Every command runs for real; only the keystrokes are simulated.
set -euo pipefail

type_run() {
  printf '\033[38;2;245;165;36m$\033[0m \033[38;2;232;237;242m'
  for ((i = 0; i < ${#1}; i++)); do printf '%s' "${1:i:1}"; sleep 0.025; done
  sleep 0.4
  printf '\033[0m\n'
  eval "$1"
  sleep 1.2
}

# CONVENE_SPEC records unreleased code honestly: the install line on screen is
# the one that ran. Unset, it installs the latest release from PyPI.
if [[ -n "${CONVENE_SPEC:-}" ]]; then
  type_run "pip install -q $CONVENE_SPEC"
else
  type_run 'pip install -q convene && convene --version'
fi
type_run 'cat tickets.jsonl'
type_run 'convene run --expert triage --in tickets.jsonl | jq -r .output.queue'
type_run 'convene usage'
printf '\033[38;2;245;165;36m$\033[0m '
sleep 1
