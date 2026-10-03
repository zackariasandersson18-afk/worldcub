#!/usr/bin/env bash
# One-shot setup on a fresh Ubuntu/Debian server (run as root):
#   curl -fsSL https://raw.githubusercontent.com/zackariasandersson18-afk/worldcub/claude/football-betting-optimizer-45ox0q/deploy/install.sh | bash -s -- <GITHUB_TOKEN>
set -euo pipefail
TOKEN="${1:?usage: install.sh <GITHUB_TOKEN> [BINANCE_TESTNET_API_KEY BINANCE_TESTNET_API_SECRET]}"
apt-get update -q && apt-get install -yq docker.io git
rm -rf /opt/tradebot
git clone -q --depth 1 --branch claude/football-betting-optimizer-45ox0q \
  https://github.com/zackariasandersson18-afk/worldcub.git /opt/tradebot
docker build -q -t tradebot /opt/tradebot
umask 077
{
  echo "GITHUB_TOKEN=$TOKEN"
  [[ -n "${2:-}" ]] && echo "BINANCE_TESTNET_API_KEY=$2"
  [[ -n "${3:-}" ]] && echo "BINANCE_TESTNET_API_SECRET=$3"
} > /etc/tradebot.env
cp /opt/tradebot/deploy/tradebot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tradebot
echo "tradebot server started. Live log: journalctl -u tradebot -f"
