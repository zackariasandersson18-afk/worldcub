#!/usr/bin/env bash
# One daily tradebot step on the demo account. State (approval, wallet,
# trade history, log) lives on the orphan branch $STATE_BRANCH so it
# survives between runs.
#
# Env:
#   SYMBOL           default BTCUSDT
#   BROKER           paper | testnet (default: testnet if keys are set, else paper)
#   IGNORE_GATES     true = trade even if the strategy failed the gates (demo only)
#   REVALIDATE       true = rerun the gates and rewrite approval.json
#   STATE_BRANCH     default tradebot-state
#   PUSH_STATE       false = don't push (local testing)
set -uo pipefail

SYMBOL="${SYMBOL:-BTCUSDT}"
STATE_BRANCH="${STATE_BRANCH:-tradebot-state}"
STATE_DIR="${STATE_DIR:-state}"
if [[ -z "${BROKER:-}" ]]; then
  if [[ -n "${BINANCE_TESTNET_API_KEY:-}" && -n "${BINANCE_TESTNET_API_SECRET:-}" ]]; then
    BROKER=testnet
  else
    BROKER=paper
  fi
fi

# --- check out the state branch into $STATE_DIR -----------------------------
if git fetch -q origin "$STATE_BRANCH" 2>/dev/null; then
  git worktree add -q --detach "$STATE_DIR" FETCH_HEAD || exit 1
  git -C "$STATE_DIR" switch -q -C "$STATE_BRANCH" || exit 1
else
  echo "no $STATE_BRANCH branch yet, starting fresh"
  git worktree add -q --detach "$STATE_DIR" || exit 1
  git -C "$STATE_DIR" switch -q --orphan "$STATE_BRANCH" || exit 1
fi

# --- gates: validate once (or on request) ------------------------------------
# also revalidate approvals written before the dashboard fields existed
if [[ ! -f "$STATE_DIR/approval.json" || "${REVALIDATE:-false}" == "true" ]] \
   || ! grep -q '"walk_forward"' "$STATE_DIR/approval.json"; then
  echo "== validating $SYMBOL =="
  python -m tradebot run --binance "$SYMBOL" --n-trials 25 \
    --save-approval "$STATE_DIR/approval.json" | tee "$STATE_DIR/validation.txt"
  if [[ ! -f "$STATE_DIR/approval.json" ]]; then
    echo "validation failed to produce an approval file" >&2
    exit 1
  fi
fi

# --- one live step -----------------------------------------------------------
flags=()
[[ "${IGNORE_GATES:-false}" == "true" ]] && flags+=(--ignore-gates)
echo "== trade step: broker=$BROKER symbol=$SYMBOL =="
python -m tradebot trade --approval "$STATE_DIR/approval.json" \
  --state "$STATE_DIR/trade_state.json" --paper-wallet "$STATE_DIR/paper_wallet.json" \
  --broker "$BROKER" --symbol "$SYMBOL" --execute "${flags[@]}" > run_out.txt
code=$?
cat run_out.txt
# The schedule has backup times in case GitHub skips one. A backup run that finds
# today's bar already handled changes nothing: keep the real run's report.
if [[ $code -eq 0 ]] && grep -q '"action": "SKIP"' run_out.txt; then
  echo "today's bar was already handled by an earlier run; nothing to save"
  exit 0
fi
mv run_out.txt "$STATE_DIR/last_run.txt"
{
  echo "--- $(date -u +%FT%TZ) broker=$BROKER exit=$code"
  cat "$STATE_DIR/last_run.txt"
} >> "$STATE_DIR/runs.log"

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "### tradebot $SYMBOL ($BROKER) exit=$code"
    echo '```json'
    sed -n '/^{/,$p' "$STATE_DIR/last_run.txt"
    echo '```'
  } >> "$GITHUB_STEP_SUMMARY"
fi

# --- save state --------------------------------------------------------------
git -C "$STATE_DIR" add -A
git -C "$STATE_DIR" -c user.name="tradebot" -c user.email="tradebot@users.noreply.github.com" \
  commit -q -m "tradebot state $(date -u +%F) ($BROKER, exit $code)" || true
if [[ "${PUSH_STATE:-true}" == "true" ]]; then
  git -C "$STATE_DIR" push -q origin "$STATE_BRANCH" || { echo "state push failed" >&2; exit 1; }
fi

# exit 2 = HALT, 1 = refused: fail the job so GitHub notifies you
exit $code
