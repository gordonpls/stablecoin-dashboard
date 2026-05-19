# API Cost Estimates

All sources below are free tier unless marked otherwise.

## DefiLlama

| Endpoint | Call frequency | Est. calls/month | Cost |
|---|---|---|---|
| `/stablecoins` | 1×/day | 30 | $0 |
| `/stablecoincharts/{id}` | 1×/day per asset | ~600 | $0 |
| `/v2/historicalChainTvl/{chain}` | 1×/day per chain | ~300 | $0 |
| `/pools` (yields) | 2×/day | 60 | $0 |

**Rate limit**: ~30 req/min (unenforced, be polite)

## Exchange Public APIs

| Exchange | Endpoint | Frequency | Cost |
|---|---|---|---|
| Binance | `/ticker/price` | 1–5 min | $0 |
| Binance | `/depth` | 1×/hour | $0 |
| Kraken | `/Ticker` | fallback only | $0 |

**Rate limits**: Binance 1200 req/min weight; Kraken 15 req/s

## CoinGecko (fallback only)

| Plan | Rate limit | Monthly cost |
|---|---|---|
| Demo (free) | 30 req/min | $0 |
| Pro | 500 req/min | ~$129/mo |

**Rule**: Only call CoinGecko when DefiLlama + exchanges cannot provide the data.

## Etherscan

| Plan | Rate limit | Monthly cost |
|---|---|---|
| Free | 5 req/s, 100k req/day | $0 |
| Standard | higher | $199/mo |

**Rule**: Use only for on-chain supply reads. One call per asset per day.

## The Graph

| Plan | Units/month free | Overage |
|---|---|---|
| Free | 100,000 queries | $0.0004/query |

**Rule**: Disabled until post-MVP. Estimated need: ~300 queries/month = $0 on free plan.

## Total Monthly Estimate (MVP)

| Provider | Est. cost |
|---|---|
| DefiLlama | $0 |
| Exchanges | $0 |
| CoinGecko | $0 |
| Etherscan | $0 |
| The Graph | $0 (disabled) |
| **Total** | **$0** |
