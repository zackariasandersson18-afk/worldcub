#!/usr/bin/env bash
# Minute-by-minute liquidity-rewards measurement. State lives in rewards/ on its own
# branch (rewards-state); the first run seeds it from tradebot-state.
set -uo pipefail
BRANCH="${REWARDS_BRANCH:-rewards-state}"
STATE_DIR="${STATE_DIR:-state_rewards_loop}"

if git fetch -q origin "$BRANCH" 2>/dev/null; then
  git worktree add -q --detach "$STATE_DIR" FETCH_HEAD || exit 1
  git -C "$STATE_DIR" switch -q -C "$BRANCH" || exit 1
else
  git worktree add -q --detach "$STATE_DIR" || exit 1
  git -C "$STATE_DIR" switch -q --orphan "$BRANCH" || exit 1
  git -C "$STATE_DIR" rm -rqf . 2>/dev/null || true
  mkdir -p "$STATE_DIR/rewards"
  git fetch -q origin tradebot-state
  for f in $(git ls-tree --name-only origin/tradebot-state rewards/); do
    git show "origin/tradebot-state:$f" > "$STATE_DIR/$f"
  done
fi
mkdir -p "$STATE_DIR/rewards"
export REWARDS_LISTING_CACHE="${RUNNER_TEMP:-/tmp}/rewards_listing_cache.json"
python -m rewardbot.loop --state "$STATE_DIR/rewards" --repo "$STATE_DIR" --branch "$BRANCH" \
  --minutes "${LOOP_MINUTES:-345}"
