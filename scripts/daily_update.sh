#!/usr/bin/env bash
# Daily update: supply, reserves, liquidity, scores
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting daily update"

python -m pipelines.update_supply   && echo "  supply OK"
python -m pipelines.update_reserves && echo "  reserves OK"
python -m pipelines.update_liquidity && echo "  liquidity OK"
python -m pipelines.score_stablecoins && echo "  scoring OK"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Daily update complete"
