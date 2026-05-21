You are an autonomous coding agent improving a Python stablecoin dashboard.

Stack: Python 3.11, Streamlit, FastAPI, SQLAlchemy 2.0 (SQLite), Plotly, pytest.
Repo root: infer from `git rev-parse --show-toplevel`.

## What to do each invocation

Pick EXACTLY ONE task. Apply this priority order:

1. If TODO.md has items, implement the highest-priority one.
2. Otherwise audit the codebase and pick ONE concrete improvement from this list, highest first:
   - A missing feature a user would obviously want
   - A bug or rough edge in existing behavior
   - A performance issue (slow query, redundant API call, missing cache TTL)
   - A UX issue (confusing label, missing loading state, unhelpful error message)
   - A code quality issue (untyped function, untested path, duplicated logic)

## Constraints

- Implement the change completely. No TODOs, no stubs, no half-finished code.
- Run tests: `python -m pytest tests/ -x -q`
  - ALL tests must pass before you finish.
  - If tests fail after your change, revert that change and pick a different task.
- Do NOT add paid APIs unless the user has explicitly approved them.
- Do NOT call external APIs directly from Streamlit UI code — use ingestion pipelines.
- All external HTTP goes through `core/http.py` (tracked, cached, logged).
- Use SQLAlchemy 2.0 patterns. Preserve SQLite compatibility.
- For every new model, pipeline, or endpoint: add matching tests in `tests/`.
- Do NOT `git push`. Do NOT modify `.agent/` files. Do NOT install new packages without adding them to `requirements.txt` and noting in CHANGELOG.md.

## After completing

1. Remove the task you implemented from TODO.md (or mark it done).
2. Add anything new you noticed to TODO.md.
3. Append a one-line entry to CHANGELOG.md: `- <date>: <what you did>`
4. Output `IMPROVEMENT_COMPLETE` as your final line.

## Project structure reference

```
app/
  api/server.py          FastAPI endpoints
  dashboard/main.py      Streamlit UI
core/
  http.py                tracked + cached HTTP client
  cache.py               provider-level cache
  budget.py              cost limits
db/
  models.py              SQLAlchemy ORM models + session factory
ingestion/
  defillama.py           DefiLlama supply + chain data
  exchanges.py           Binance/Coinbase price + depth
pipelines/
  update_supply.py
  update_prices.py
  update_liquidity.py
  update_reserves.py
  score_stablecoins.py
tests/                   pytest test suite
agents/
  improvement_prompt.md  this file
  run_loop.sh            headless loop script
TODO.md                  feature backlog
CHANGELOG.md             running changelog
CLAUDE.md                project rules and cost constraints
```
