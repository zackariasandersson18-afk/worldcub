#!/usr/bin/env bash
# One hourly weatherbot paper step. State lives in state/weather on the
# tradebot-state branch (shared with the BTC bot; the workflows share a
# concurrency group so their pushes never race).
#
# Env: IGNORE_GATES=true trades even if the backtest rejected the strategy (paper only)
#      REBACKTEST=true reruns the backtest and rewrites backtest.json
set -uo pipefail
STATE_BRANCH="${STATE_BRANCH:-tradebot-state}"
STATE_DIR="${STATE_DIR:-state}"
W="$STATE_DIR/weather"

if git fetch -q origin "$STATE_BRANCH" 2>/dev/null; then
  git worktree add -q --detach "$STATE_DIR" FETCH_HEAD || exit 1
  git -C "$STATE_DIR" switch -q -C "$STATE_BRANCH" || exit 1
else
  git worktree add -q --detach "$STATE_DIR" || exit 1
  git -C "$STATE_DIR" switch -q --orphan "$STATE_BRANCH" || exit 1
fi
mkdir -p "$W"

if [[ ! -f "$W/backtest.json" || "${REBACKTEST:-false}" == "true" ]]; then
  echo "== backtest (seeds calibration) =="
  python -m weatherbot backtest --days "${BACKTEST_DAYS:-45}" --state "$W" | tail -40
fi

flags=()
[[ "${IGNORE_GATES:-false}" == "true" ]] && flags+=(--ignore-gates)
python -m weatherbot paper --state "$W" "${flags[@]}"
code=$?

git -C "$STATE_DIR" add -A
git -C "$STATE_DIR" -c user.name="tradebot" -c user.email="tradebot@users.noreply.github.com" \
  commit -q -m "weatherbot paper $(date -u +%FT%H:%MZ)" || true
if [[ "${PUSH_STATE:-true}" == "true" ]]; then
  git -C "$STATE_DIR" push -q origin "$STATE_BRANCH" || { echo "state push failed" >&2; exit 1; }
fi
exit $code
