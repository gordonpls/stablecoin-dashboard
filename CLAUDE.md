# Stablecoin Agent Project

You are building a cost-conscious stablecoin and crypto dashboard.

## Goal

Build a dashboard that tracks stablecoin health, liquidity, peg risk, supply growth, reserve transparency, and market activity. We will continue adding features and building upon this dashboard. It will also teach the user about crypto/stablecoins.

## Priorities

1. Use free APIs first.
2. Cache every API response.
3. Never call paid APIs from the frontend.
4. Prefer batch API requests over one request per asset.
5. Store time-series snapshots in the database.
6. Add tests for every ingestion source.
7. Estimate API cost before adding a new provider.
8. Fail gracefully when an API is unavailable.
9. Log API usage per provider.
10. Do not add a paid API unless I explicitly approve it.

## First data sources

- DefiLlama for stablecoin supply, TVL, chain data, and yields.
- Exchange public APIs for peg prices and order book snapshots.
- CoinGecko or CoinMarketCap only as fallback market data.
- The Graph only after the MVP is working.
- Alchemy or Infura only for direct on-chain reads.

## Core metrics

Stablecoin metrics:
- circulating supply
- supply by chain
- 7 day supply change
- 30 day supply change
- peg deviation in basis points
- volume
- liquidity depth
- reserve report freshness
- reserve composition
- issuer
- risk score

Risk scores:
- peg_score
- liquidity_score
- reserve_score
- adoption_score
- overall_score

## Cost rules

- Cache API responses.
- Add rate limits.
- Add request logging.
- Add a provider budget config.
- Avoid high frequency polling unless needed.
- Use daily refresh for slow data.
- Use 1 to 5 minute refresh only for peg prices.
- Manual API refresh on calls that are limited.