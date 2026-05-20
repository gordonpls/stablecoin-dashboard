"""Stablecoin Dashboard — Streamlit MVP.

Reads directly from SQLite. Run ingestion pipelines first to populate data.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add repo root to sys.path so local packages (db, core, ingestion, pipelines)
# are importable regardless of which directory Streamlit uses as its working dir.
sys.path.insert(0, str(Path(__file__).parents[2]))

import json
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import func, select

from db.models import (
    ApiRequestLog,
    PriceSnapshot,
    RiskScore,
    SupplySnapshot,
    get_session,
    init_db,
)

st.set_page_config(
    page_title="Stablecoin Dashboard",
    page_icon=":bank:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

RISK_COLORS = {
    "Low Risk":  "#22c55e",
    "Moderate":  "#eab308",
    "Elevated":  "#f97316",
    "High Risk": "#ef4444",
}

SCORE_COLORS = ["#6366f1", "#22c55e", "#f59e0b", "#ec4899"]
SCORE_COLS   = ["peg_score", "liquidity_score", "reserve_score", "adoption_score"]


def risk_label(score: float | None) -> str:
    if score is None:
        return "—"
    if score >= 80:
        return "Low Risk"
    if score >= 60:
        return "Moderate"
    if score >= 40:
        return "Elevated"
    return "High Risk"


def _fmt_supply(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _fmt_freshness(scored_at: datetime | None) -> str:
    if scored_at is None:
        return "—"
    delta = datetime.utcnow() - scored_at
    mins = int(delta.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _parse_chain_data(supply_by_chain_json: str | None) -> tuple[str, float | None, float | None]:
    """Return (top_chain_name, prev_week_supply_usd, prev_month_supply_usd)."""
    if not supply_by_chain_json:
        return "—", None, None
    try:
        data: dict = json.loads(supply_by_chain_json)
        if not data:
            return "—", None, None

        def _usd(entry: dict, key: str) -> float:
            v = entry.get(key) or {}
            return (v.get("peggedUSD") or 0) if isinstance(v, dict) else 0

        top_chain  = max(data, key=lambda k: _usd(data[k], "current"))
        prev_week  = sum(_usd(v, "circulatingPrevWeek")  for v in data.values())
        prev_month = sum(_usd(v, "circulatingPrevMonth") for v in data.values())
        return top_chain, (prev_week or None), (prev_month or None)
    except Exception:
        return "—", None, None


# ── data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def load_overview() -> pd.DataFrame:
    with get_session() as session:
        supply_sq = (
            select(SupplySnapshot.symbol, func.max(SupplySnapshot.recorded_at).label("ts"))
            .group_by(SupplySnapshot.symbol)
            .subquery()
        )
        latest_supplies = {
            row.symbol: row
            for row in session.execute(
                select(SupplySnapshot).join(
                    supply_sq,
                    (SupplySnapshot.symbol == supply_sq.c.symbol)
                    & (SupplySnapshot.recorded_at == supply_sq.c.ts),
                )
            ).scalars().all()
        }

        score_sq = (
            select(RiskScore.symbol, func.max(RiskScore.scored_at).label("scored_at"))
            .group_by(RiskScore.symbol)
            .subquery()
        )
        latest_scores = {
            row.symbol: row
            for row in session.execute(
                select(RiskScore).join(
                    score_sq,
                    (RiskScore.symbol == score_sq.c.symbol)
                    & (RiskScore.scored_at == score_sq.c.scored_at),
                )
            ).scalars().all()
        }

        price_sq = (
            select(PriceSnapshot.symbol, func.max(PriceSnapshot.recorded_at).label("ts"))
            .group_by(PriceSnapshot.symbol)
            .subquery()
        )
        latest_prices = {
            row.symbol: row
            for row in session.execute(
                select(PriceSnapshot).join(
                    price_sq,
                    (PriceSnapshot.symbol == price_sq.c.symbol)
                    & (PriceSnapshot.recorded_at == price_sq.c.ts),
                )
            ).scalars().all()
        }

    rows = []
    for symbol, supply_row in latest_supplies.items():
        score_row = latest_scores.get(symbol)
        if score_row is None:
            continue
        price_row = latest_prices.get(symbol)

        top_chain, prev_week, prev_month = _parse_chain_data(supply_row.supply_by_chain)
        supply = supply_row.circulating_supply
        price  = price_row.price if price_row else 1.0

        change_7d  = ((supply - prev_week)  / prev_week  * 100) if prev_week  and prev_week  > 0 else None
        change_30d = ((supply - prev_month) / prev_month * 100) if prev_month and prev_month > 0 else None

        rows.append({
            "symbol":            symbol,
            "market_cap":        supply * price,
            "supply":            supply,
            "change_7d":         change_7d,
            "change_30d":        change_30d,
            "top_chain":         top_chain,
            "peg_deviation_bps": price_row.peg_deviation_bps if price_row else None,
            "overall_score":     score_row.overall_score,
            "risk_label":        risk_label(score_row.overall_score),
            "scored_at":         score_row.scored_at,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("supply", ascending=False).reset_index(drop=True)
    return df


@st.cache_data(ttl=120, show_spinner=False)
def load_latest_scores() -> pd.DataFrame:
    with get_session() as session:
        score_sq = (
            select(RiskScore.symbol, func.max(RiskScore.scored_at).label("scored_at"))
            .group_by(RiskScore.symbol)
            .subquery()
        )
        scores = session.execute(
            select(RiskScore).join(
                score_sq,
                (RiskScore.symbol == score_sq.c.symbol)
                & (RiskScore.scored_at == score_sq.c.scored_at),
            )
        ).scalars().all()

        supply_sq = (
            select(SupplySnapshot.symbol, func.max(SupplySnapshot.recorded_at).label("ts"))
            .group_by(SupplySnapshot.symbol)
            .subquery()
        )
        supplies = {
            row.symbol: row.circulating_supply
            for row in session.execute(
                select(SupplySnapshot).join(
                    supply_sq,
                    (SupplySnapshot.symbol == supply_sq.c.symbol)
                    & (SupplySnapshot.recorded_at == supply_sq.c.ts),
                )
            ).scalars().all()
        }

        price_sq = (
            select(PriceSnapshot.symbol, func.max(PriceSnapshot.recorded_at).label("ts"))
            .group_by(PriceSnapshot.symbol)
            .subquery()
        )
        prices = {
            row.symbol: row
            for row in session.execute(
                select(PriceSnapshot).join(
                    price_sq,
                    (PriceSnapshot.symbol == price_sq.c.symbol)
                    & (PriceSnapshot.recorded_at == price_sq.c.ts),
                )
            ).scalars().all()
        }

    rows = []
    for s in scores:
        p = prices.get(s.symbol)
        rows.append({
            "symbol":             s.symbol,
            "circulating_supply": supplies.get(s.symbol, 0),
            "price":              p.price if p else None,
            "peg_deviation_bps":  p.peg_deviation_bps if p else None,
            "bid_depth_usd":      p.bid_depth_usd if p else None,
            "ask_depth_usd":      p.ask_depth_usd if p else None,
            "peg_score":          s.peg_score,
            "liquidity_score":    s.liquidity_score,
            "reserve_score":      s.reserve_score,
            "adoption_score":     s.adoption_score,
            "overall_score":      s.overall_score,
            "scored_at":          s.scored_at,
            "risk_label":         risk_label(s.overall_score),
        })

    return pd.DataFrame(rows)


@st.cache_data(ttl=300, show_spinner=False)
def load_supply_history(symbol: str, days: int = 30) -> pd.DataFrame:
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as session:
        rows = session.execute(
            select(SupplySnapshot)
            .where(SupplySnapshot.symbol == symbol, SupplySnapshot.recorded_at >= cutoff)
            .order_by(SupplySnapshot.recorded_at)
        ).scalars().all()
    return pd.DataFrame([r.to_dict() for r in rows])


@st.cache_data(ttl=120, show_spinner=False)
def load_price_history(symbol: str, hours: int = 24) -> pd.DataFrame:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as session:
        rows = session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.symbol == symbol, PriceSnapshot.recorded_at >= cutoff)
            .order_by(PriceSnapshot.recorded_at)
        ).scalars().all()
    return pd.DataFrame([r.to_dict() for r in rows])


@st.cache_data(ttl=300, show_spinner=False)
def load_score_history(symbol: str, days: int = 30) -> pd.DataFrame:
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as session:
        rows = session.execute(
            select(RiskScore)
            .where(RiskScore.symbol == symbol, RiskScore.scored_at >= cutoff)
            .order_by(RiskScore.scored_at)
        ).scalars().all()
    return pd.DataFrame([r.to_dict() for r in rows])


@st.cache_data(ttl=300, show_spinner=False)
def load_api_usage() -> pd.DataFrame:
    with get_session() as session:
        rows = session.execute(
            select(
                ApiRequestLog.provider,
                ApiRequestLog.endpoint,
                func.count().label("calls"),
                func.max(ApiRequestLog.requested_at).label("last_call"),
            )
            .group_by(ApiRequestLog.provider, ApiRequestLog.endpoint)
            .order_by(ApiRequestLog.provider, ApiRequestLog.endpoint)
        ).all()
    return pd.DataFrame(rows, columns=["provider", "endpoint", "calls", "last_call"])


# ── static provider cost table ────────────────────────────────────────────────

PROVIDER_COSTS = pd.DataFrame([
    {"provider": "DefiLlama", "endpoint": "stablecoins",       "frequency": "daily",    "cost_usd": 0.00},
    {"provider": "DefiLlama", "endpoint": "stablecoin_charts", "frequency": "daily",    "cost_usd": 0.00},
    {"provider": "Binance",   "endpoint": "ticker_price",      "frequency": "1-5 min",  "cost_usd": 0.00},
    {"provider": "Binance",   "endpoint": "order_book_depth",  "frequency": "hourly",   "cost_usd": 0.00},
    {"provider": "Coinbase",  "endpoint": "spot_price",        "frequency": "fallback", "cost_usd": 0.00},
])


# ── page sections ─────────────────────────────────────────────────────────────

def render_header(df: pd.DataFrame) -> None:
    st.title("Stablecoin Dashboard")
    st.caption(
        "A stablecoin is a cryptocurrency designed to maintain a fixed value — "
        "almost always $1.00 USD. This dashboard tracks their supply, peg stability, "
        "liquidity, and overall risk across the market."
    )
    if df.empty:
        return
    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    total = df["supply"].sum()
    c1.metric(
        "Total Supply", _fmt_supply(total),
        help=(
            "Combined circulating supply of all tracked stablecoins in USD. "
            "Because each token targets $1.00, this closely approximates total market capitalization."
        ),
    )
    c2.metric(
        "Assets Tracked", len(df),
        help="Number of distinct stablecoin assets with risk scores in the database.",
    )
    avg_dev = df["peg_deviation_bps"].dropna().mean() if "peg_deviation_bps" in df.columns else None
    c3.metric(
        "Avg Peg Deviation",
        f"{avg_dev:.1f} bps" if pd.notna(avg_dev) else "—",
        help=(
            "Average distance from $1.00 across assets with live price data, measured in basis points. "
            "1 basis point = 0.01 cents. Lower is better."
        ),
    )
    high_risk = int((df["overall_score"] < 50).sum())
    c4.metric(
        "High Risk Assets", high_risk,
        delta_color="inverse",
        help=(
            "Assets with an overall risk score below 50 out of 100. "
            "A low score can indicate a weak peg, thin liquidity, or outdated reserve disclosures."
        ),
    )


def render_overview_tab(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No data yet. Run the ingestion pipelines first.")
        return

    st.markdown(
        "The table below summarises every tracked stablecoin in one place. "
        "Click any column header to sort. Use the expander below if you want to understand what each column means."
    )

    with st.expander("What do these columns mean?"):
        st.markdown("""
| Column | What it measures |
|---|---|
| **Market Cap** | Total value of all tokens in circulation. For a $1.00-pegged coin this equals supply. |
| **Supply** | Number of tokens outstanding, converted to USD at the current price. |
| **7D / 30D Change** | How much supply grew or shrank over the past 7 or 30 days. Rapid growth suggests rising adoption; a sharp drop may signal redemptions or distress. |
| **Top Chain** | The blockchain network holding the largest share of this stablecoin's supply (e.g. Ethereum, Tron, BSC). |
| **Peg Deviation** | How far the market price is from $1.00, in basis points (bps). 1 bps = 0.01 cents. A healthy stablecoin typically stays within 10 bps. |
| **Risk Score** | A composite score from 0 to 100. Higher is safer. See the Risk Scores tab for the full breakdown. |
| **Risk Level** | A plain-English label derived from the risk score: Low Risk (80+), Moderate (60–79), Elevated (40–59), High Risk (<40). |
| **Data Freshness** | How recently the risk score was calculated. Stale scores may not reflect current market conditions. |
        """)

    st.caption(f"{len(df)} assets · sorted by supply")

    display = df[[
        "symbol", "market_cap", "supply",
        "change_7d", "change_30d",
        "top_chain", "peg_deviation_bps",
        "overall_score", "risk_label", "scored_at",
    ]].copy()

    display["market_cap"]        = display["market_cap"].apply(_fmt_supply)
    display["supply"]            = display["supply"].apply(_fmt_supply)
    display["change_7d"]         = display["change_7d"].apply(_fmt_pct)
    display["change_30d"]        = display["change_30d"].apply(_fmt_pct)
    display["peg_deviation_bps"] = display["peg_deviation_bps"].apply(
        lambda v: f"{v:.1f} bps" if pd.notna(v) else "—"
    )
    display["overall_score"] = display["overall_score"].apply(lambda v: f"{v:.0f}")
    display["scored_at"]     = display["scored_at"].apply(_fmt_freshness)

    display.columns = [
        "Symbol", "Market Cap", "Supply",
        "7D Change", "30D Change",
        "Top Chain", "Peg Deviation",
        "Risk Score", "Risk Level", "Data Freshness",
    ]

    def color_risk_level(val: str) -> str:
        c = RISK_COLORS.get(val, "")
        return f"color: {c}; font-weight: bold" if c else ""

    st.dataframe(
        display.style.map(color_risk_level, subset=["Risk Level"]),
        use_container_width=True,
        hide_index=True,
    )


def render_supply_tab(df: pd.DataFrame) -> None:
    st.subheader("Circulating Supply")
    if df.empty:
        st.info("No supply data yet. Run `python -m pipelines.update_supply`.")
        return

    st.markdown(
        "Circulating supply is the total number of stablecoin tokens currently in existence, "
        "valued in USD. Because each token targets $1.00, supply closely tracks market capitalization — "
        "a useful proxy for how widely adopted a stablecoin is."
    )

    with st.expander("Why does supply matter?"):
        st.markdown("""
**Large, growing supply** generally reflects strong adoption and broad market trust. Tether (USDT)
and USD Coin (USDC) are the two largest by supply and serve as the backbone of crypto trading.

**Sudden supply drops** can signal a bank-run dynamic — holders redeeming tokens faster than new
tokens are minted — which sometimes precedes a peg break.

**Supply by chain** shows where users hold the asset. An asset concentrated on a single chain
carries more platform risk than one distributed across many networks.

**Reading the log-scale chart:** The y-axis uses a logarithmic scale by default so you can compare
assets that differ by orders of magnitude (e.g., USDT at $110B vs a small stablecoin at $10M).
Each step up the y-axis represents a 10× increase.
        """)

    sorted_df = df.sort_values("circulating_supply", ascending=False)

    c1, c2 = st.columns([3, 1])
    top_n     = c1.slider("Assets to show (by supply)", min_value=5, max_value=50, value=25, step=5, key="supply_top_n")
    log_scale = c2.checkbox("Log scale", value=True, key="supply_log")

    chart_df = sorted_df.head(top_n)
    fig = px.bar(
        chart_df,
        x="symbol",
        y="circulating_supply",
        color="circulating_supply",
        color_continuous_scale="Blues",
        labels={"circulating_supply": "Supply (USD)", "symbol": ""},
        log_y=log_scale,
    )
    fig.update_layout(coloraxis_showscale=False, plot_bgcolor="rgba(0,0,0,0)")
    if not log_scale:
        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Supply History")
    st.caption("How has a single asset's supply changed over the past 30 days?")
    sym = st.selectbox("Asset", sorted_df["symbol"].tolist(), key="supply_chain_sym")
    history = load_supply_history(sym, days=30)
    if not history.empty and len(history) > 1:
        fig2 = px.area(
            history,
            x="recorded_at",
            y="circulating_supply",
            labels={"recorded_at": "", "circulating_supply": "Supply (USD)"},
        )
        fig2.update_yaxes(tickprefix="$", tickformat=",.0f")
        fig2.update_layout(plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Supply history will appear after multiple pipeline runs.")

    st.divider()
    display = sorted_df[["symbol", "circulating_supply"]].copy()
    display["circulating_supply"] = display["circulating_supply"].apply(_fmt_supply)
    display.columns = ["Symbol", "Circulating Supply"]
    st.dataframe(display, use_container_width=True, hide_index=True)


def render_peg_tab(df: pd.DataFrame) -> None:
    st.subheader("Peg Deviation")
    if df.empty:
        st.info("No price data yet. Run `python -m pipelines.update_prices`.")
        return

    st.markdown(
        "A stablecoin's core promise is to stay worth exactly $1.00. "
        "Peg deviation measures how far the current market price has drifted from that target, "
        "expressed in **basis points (bps)**. Smaller is better — a well-functioning stablecoin "
        "should rarely exceed 10 bps under normal conditions."
    )

    with st.expander("What is a basis point, and what deviation levels should I watch for?"):
        st.markdown("""
**1 basis point (bps) = 0.01% = $0.0001**

| Deviation | Dollar value | Interpretation |
|---|---|---|
| < 10 bps | < $0.0010 | Normal — within expected noise |
| 10–50 bps | $0.0010–$0.0050 | Elevated — worth monitoring |
| 50–100 bps | $0.0050–$0.0100 | Warning — liquidity or confidence stress |
| > 100 bps | > $0.0100 | Critical — potential peg break |

**Why do stablecoins lose their peg?**

- **Fiat-backed (USDT, USDC):** Concerns about the issuer's solvency or the quality of reserves can
  trigger sell pressure that pushes the price below $1.00. Strong buy pressure (e.g., during
  crypto market crashes when people seek safety) can briefly push it above $1.00.
- **Crypto-backed (DAI):** If the value of the collateral (e.g., ETH) falls sharply, the protocol
  must liquidate positions to maintain the peg. Extreme volatility can temporarily break it.
- **Algorithmic:** Rely on supply-and-demand mechanisms rather than held assets. These have historically
  been the most fragile — the Terra/LUNA collapse in 2022 is the most prominent example.
        """)

    peg_df = df.dropna(subset=["peg_deviation_bps"]).sort_values("peg_deviation_bps", ascending=False)

    fig = px.bar(
        peg_df,
        x="symbol",
        y="peg_deviation_bps",
        color="peg_deviation_bps",
        color_continuous_scale="RdYlGn_r",
        labels={"peg_deviation_bps": "Deviation (bps)", "symbol": ""},
    )
    fig.add_hline(y=10, line_dash="dot", line_color="orange", annotation_text="10 bps — elevated")
    fig.add_hline(y=50, line_dash="dot", line_color="red",    annotation_text="50 bps — warning")
    fig.update_layout(coloraxis_showscale=False, plot_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Price History")
    st.caption("Each data point is one price snapshot. The dashed line marks the $1.00 target.")
    sym = st.selectbox("Asset", peg_df["symbol"].tolist(), key="peg_sym")
    history = load_price_history(sym, hours=24)
    if not history.empty and len(history) > 1:
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=history["recorded_at"],
            y=history["price"],
            mode="lines+markers",
            name="Price",
            line=dict(color="#6366f1"),
        ))
        fig2.add_hline(y=1.0, line_dash="dash", line_color="gray", annotation_text="$1.00 peg")
        fig2.update_layout(
            yaxis_title="Price (USD)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(tickformat="$.4f"),
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Price history will appear after multiple pipeline runs.")

    st.divider()
    table = peg_df[["symbol", "price", "peg_deviation_bps", "bid_depth_usd", "ask_depth_usd"]].copy()
    table.columns = ["Symbol", "Price", "Deviation (bps)", "Bid Depth (USD)", "Ask Depth (USD)"]
    st.dataframe(table, use_container_width=True, hide_index=True)


def render_risk_tab(df: pd.DataFrame) -> None:
    st.subheader("Risk Scores")
    if df.empty:
        st.info("No scores yet. Run `python -m pipelines.score_stablecoins`.")
        return

    st.markdown(
        "Each stablecoin is rated across four dimensions on a scale of 0 to 100. "
        "**Higher scores indicate lower risk.** The overall score is a weighted average "
        "designed to surface assets that are stable, liquid, transparent, and widely adopted."
    )

    with st.expander("How are the scores calculated?"):
        st.markdown("""
| Dimension | Weight | What it measures | How it is scored |
|---|---|---|---|
| **Peg Score** | 35% | Closeness to $1.00 | 100 = perfect peg; drops linearly to 0 at 100 bps deviation |
| **Liquidity Score** | 25% | Order book depth | 100 = $50M+ combined bid/ask depth; 0 = no depth data |
| **Reserve Score** | 25% | Transparency of backing | Based on how recently the reserve report was published and whether an independent auditor signed off |
| **Adoption Score** | 15% | Market size | 100 = $5B+ circulating supply; scales linearly from 0 |

**Overall score = Peg × 0.35 + Liquidity × 0.25 + Reserve × 0.25 + Adoption × 0.15**

**Risk levels:**
- **Low Risk (80–100):** Stable peg, deep liquidity, fresh audited reserves, large market.
- **Moderate (60–79):** Generally healthy but with one weaker dimension worth watching.
- **Elevated (40–59):** Meaningful gaps in at least one area — treat with caution.
- **High Risk (< 40):** Multiple risk factors present. Approach with significant caution.

*Scores are a quantitative starting point, not financial advice. Always read the issuer's disclosures.*
        """)

    sorted_df = df.sort_values("circulating_supply", ascending=False)

    top_n    = st.slider("Assets to show (by supply)", min_value=5, max_value=50, value=20, step=5)
    chart_df = sorted_df.head(top_n).sort_values("overall_score", ascending=False)

    fig = go.Figure()
    for col, color in zip(SCORE_COLS, SCORE_COLORS):
        fig.add_trace(go.Bar(
            name=col.replace("_", " ").title(),
            x=chart_df["symbol"],
            y=chart_df[col],
            marker_color=color,
        ))
    fig.update_layout(
        barmode="group",
        yaxis=dict(range=[0, 100], title="Score (0 = highest risk, 100 = lowest risk)"),
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        height=420,
    )
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Score History")
    st.caption(
        "Track how an asset's scores change over time. A declining peg or liquidity score "
        "can be an early warning sign before a larger problem emerges."
    )
    all_syms = sorted_df["symbol"].tolist()
    sym      = st.selectbox("Asset", all_syms, key="risk_history_sym")
    history  = load_score_history(sym, days=30)
    if not history.empty and len(history) > 1:
        fig2 = go.Figure()
        line_styles = ["solid", "dash", "dot", "dashdot"]
        for col, color, dash in zip(SCORE_COLS, SCORE_COLORS, line_styles):
            fig2.add_trace(go.Scatter(
                x=history["scored_at"],
                y=history[col],
                name=col.replace("_", " ").title(),
                mode="lines",
                line=dict(color=color, dash=dash),
            ))
        fig2.update_layout(
            yaxis=dict(range=[0, 100], title="Score"),
            plot_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            height=350,
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Score history will appear after multiple pipeline runs.")

    st.divider()
    display = sorted_df.head(top_n).sort_values("overall_score", ascending=False)[[
        "symbol", "overall_score", "risk_label",
        "peg_score", "liquidity_score", "reserve_score", "adoption_score",
    ]].copy()
    display.columns = ["Symbol", "Overall", "Risk Level", "Peg", "Liquidity", "Reserve", "Adoption"]

    def color_risk(val: str) -> str:
        c = RISK_COLORS.get(val, "")
        return f"color: {c}; font-weight: bold" if c else ""

    st.dataframe(
        display.style.map(color_risk, subset=["Risk Level"]),
        use_container_width=True,
        hide_index=True,
    )


def render_api_tab() -> None:
    st.subheader("Provider Cost Table")
    st.dataframe(PROVIDER_COSTS, use_container_width=True, hide_index=True)

    st.subheader("Live API Usage")
    usage_df = load_api_usage()
    if usage_df.empty:
        st.info("No API calls logged yet.")
    else:
        st.dataframe(usage_df, use_container_width=True, hide_index=True)

        fig = px.bar(
            usage_df,
            x="endpoint",
            y="calls",
            color="provider",
            labels={"calls": "Total Calls", "endpoint": ""},
        )
        fig.update_layout(plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    init_db()

    overview_df = load_overview()
    scores_df   = load_latest_scores()

    render_header(overview_df)

    tab_overview, tab_supply, tab_peg, tab_risk, tab_api = st.tabs([
        "Overview",
        "Supply",
        "Peg Deviation",
        "Risk Scores",
        "API Usage",
    ])

    with tab_overview:
        render_overview_tab(overview_df)

    with tab_supply:
        render_supply_tab(scores_df)

    with tab_peg:
        render_peg_tab(scores_df)

    with tab_risk:
        render_risk_tab(scores_df)

    with tab_api:
        render_api_tab()

    st.sidebar.markdown("---")
    st.sidebar.caption(f"Data as of {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")

    st.sidebar.markdown("---")
    st.sidebar.subheader("Manual Refresh")
    pwd = st.sidebar.text_input("Password", type="password", key="refresh_pwd")
    if st.sidebar.button("Refresh Data"):
        if pwd == "2026":
            import core.cache as _api_cache
            import pipelines.update_supply as _supply
            import pipelines.update_prices as _prices
            import pipelines.update_reserves as _reserves
            import pipelines.score_stablecoins as _scores
            all_ok = True
            with st.sidebar.status("Refreshing...", expanded=True) as status:
                try:
                    st.write("Fetching supply...")
                    _api_cache.clear("defillama")
                    _supply.run()

                    st.write("Fetching prices...")
                    _api_cache.clear("binance")
                    _api_cache.clear("coinbase")
                    _prices.run()

                    st.write("Updating reserves...")
                    _reserves.run()

                    st.write("Scoring...")
                    _scores.run()

                    status.update(label="Done!", state="complete")
                except Exception as exc:
                    all_ok = False
                    status.update(label=f"Error: {exc}", state="error")
            if all_ok:
                st.cache_data.clear()
                st.rerun()
        else:
            st.sidebar.error("Incorrect password")


if __name__ == "__main__":
    main()
