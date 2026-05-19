#!/usr/bin/env bash
# Hourly update: peg prices only (exchange rate-limits allow ~60 req/min)
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting hourly price update"

python -m pipelines.update_prices && echo "  prices OK"
python -m pipelines.score_stablecoins && echo "  scoring OK"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Hourly update complete"
