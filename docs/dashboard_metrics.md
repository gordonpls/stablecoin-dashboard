# Dashboard Metrics

## Summary Cards

| Metric | Source | Refresh |
|---|---|---|
| Total stablecoin supply (USD) | DefiLlama | Daily |
| Assets tracked | DB | On load |
| Average peg deviation (bps) | Exchange prices | 1–5 min |
| High-risk assets (score < 50) | Risk scores | After each score run |

## Per-Asset Table

| Column | Description | Source |
|---|---|---|
| Symbol | Ticker | DefiLlama |
| Name | Full name | DefiLlama |
| Circulating Supply | USD value | DefiLlama daily |
| 7d Supply Change | % change | DefiLlama chart |
| 30d Supply Change | % change | DefiLlama chart |
| Price | Current market price | Exchange |
| Peg Deviation (bps) | `abs(price - 1) × 10000` | Exchange |
| Bid Depth | USD depth on bid side | Exchange |
| Ask Depth | USD depth on ask side | Exchange |
| Reserve Date | Latest attestation date | Manual / pipeline |
| Auditor | Audit firm | Manual |
| Peg Score | 0–100 | Risk pipeline |
| Liquidity Score | 0–100 | Risk pipeline |
| Reserve Score | 0–100 | Risk pipeline |
| Adoption Score | 0–100 | Risk pipeline |
| Overall Score | 0–100 weighted | Risk pipeline |

## Scoring Formula

```
overall = peg_score × 0.35
        + liquidity_score × 0.25
        + reserve_score × 0.25
        + adoption_score × 0.15
```

### Peg Score
- `100 - (deviation_bps / 100) × 100`
- 0 bps → 100; 100 bps → 0; unknown → 50

### Liquidity Score
- `min(100, total_depth_usd / $50M × 100)`
- $50M+ depth → 100; unknown → 50

### Reserve Score
- Fresh report + auditor → up to 100
- Stale (>90 days) → near 0
- No report → 20

### Adoption Score
- `min(100, supply_usd / $5B × 100)`
- $5B+ supply → 100; $0 → 0

## Risk Thresholds

| Score | Label | Color |
|---|---|---|
| 80–100 | Low Risk | Green |
| 60–79 | Moderate | Yellow |
| 40–59 | Elevated | Orange |
| 0–39 | High Risk | Red |

## Refresh Schedule

| Data | Schedule |
|---|---|
| Peg prices + depth | Every 1–5 minutes (exchange polling) |
| Supply + chains | Daily (DefiLlama) |
| Yield data | Every 30 minutes |
| Reserve reports | Manual / daily check |
| Risk scores | After every price or supply update |
