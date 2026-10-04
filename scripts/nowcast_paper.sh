#!/usr/bin/env bash
# One paper step of the 16:00-solar nowcast. State lives in state/nowcast on the
# tradebot-state branch (shared with the other bots; one writer at a time via
# the workflows' concurrency group).
set -uo pipefail
STATE_BRANCH="${STATE_BRANCH:-tradebot-state}"
STATE_DIR="${STATE_DIR:-state}"

if git fetch -q origin "$STATE_BRANCH" 2>/dev/null; then
  git worktree add -q --detach "$STATE_DIR" FETCH_HEAD || exit 1
  git -C "$STATE_DIR" switch -q -C "$STATE_BRANCH" || exit 1
else
  git worktree add -q --detach "$STATE_DIR" || exit 1
  git -C "$STATE_DIR" switch -q --orphan "$STATE_BRANCH" || exit 1
fi
mkdir -p "$STATE_DIR/nowcast"

python -m weatherbot nowcast-paper --state "$STATE_DIR/nowcast"
code=$?

git -C "$STATE_DIR" add -A
git -C "$STATE_DIR" -c user.name="tradebot" -c user.email="tradebot@users.noreply.github.com" \
  commit -q -m "nowcast paper $(date -u +%FT%H:%MZ)" || true
if [[ "${PUSH_STATE:-true}" == "true" ]]; then
  git -C "$STATE_DIR" push -q origin "$STATE_BRANCH" || { echo "state push failed" >&2; exit 1; }
fi
exit $code
