"""Stablecoin Dashboard — Streamlit MVP.

Reads directly from SQLite. Run ingestion pipelines first to populate data.
"""

from __future__ import annotations

import sys
import time
import logging
from pathlib import Path

# Ensure repo root is on sys.path regardless of Streamlit's working directory.
sys.path.insert(0, str(Path(__file__).parents[2]))

import json
from datetime import datetime, timedelta

import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

import pandas as pd
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

logger = logging.getLogger(__name__)

# Per CLAUDE.md: "1 to 5 minute refresh for peg prices", "daily refresh for slow data".
PRICE_REFRESH_SECS  = 600   # price/score pipeline cadence (10 minutes)
SUPPLY_REFRESH_SECS = 3600  # supply + reserves cadence (1 hour)

# Process-level timestamps so the timer survives multiple browser sessions
# without hammering the APIs on every new connection.
_PIPELINE_LAST_RUN: dict[str, float] = {"prices": 0.0, "supply": 0.0}


def _run_scheduled_pipelines() -> bool:
    """Run pipelines whose interval has elapsed. Returns True if any pipeline ran."""
    import core.cache as _api_cache
    import pipelines.update_prices     as _prices
    import pipelines.update_supply     as _supply
    import pipelines.update_reserves   as _reserves
    import pipelines.score_stablecoins as _scores

    now = time.time()
    ran = False

    if now - _PIPELINE_LAST_RUN["supply"] >= SUPPLY_REFRESH_SECS:
        try:
            _api_cache.clear("defillama")
            _supply.run()
            _reserves.run()
            _PIPELINE_LAST_RUN["supply"] = now
            ran = True
        except Exception as exc:
            logger.warning("auto_supply_pipeline_failed error=%s", exc)

    if now - _PIPELINE_LAST_RUN["prices"] >= PRICE_REFRESH_SECS:
        try:
            _api_cache.clear("binance")
            _api_cache.clear("coinbase")
            _prices.run()
            _scores.run()
            _PIPELINE_LAST_RUN["prices"] = now
            ran = True
        except Exception as exc:
            logger.warning("auto_price_pipeline_failed error=%s", exc)

    return ran


st.set_page_config(
    page_title="Stablecoin Dashboard",
    page_icon=":bank:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── design tokens ─────────────────────────────────────────────────────────────

C_PRIMARY  = "#6366f1"
C_BLUE     = "#3b82f6"
C_GREEN    = "#22c55e"
C_AMBER    = "#f59e0b"
C_ORANGE   = "#f97316"
C_RED      = "#ef4444"
C_MUTED    = "rgba(100,100,100,0.9)"

RISK_COLORS = {
    "Low Risk":  C_GREEN,
    "Moderate":  C_AMBER,
    "Elevated":  C_ORANGE,
    "High Risk": C_RED,
}

SCORE_COLORS = [C_PRIMARY, C_GREEN, C_AMBER, "#ec4899"]
SCORE_COLS   = ["peg_score", "liquidity_score", "reserve_score", "adoption_score"]
SCORE_LABELS = ["Peg", "Liquidity", "Reserve", "Adoption"]


# ── helpers ───────────────────────────────────────────────────────────────────

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
    mins  = int(delta.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _parse_chain_data(supply_by_chain_json: str | None) -> tuple[str, float | None, float | None]:
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


def _chart_layout(title: str = "", height: int = 400, **kwargs) -> dict:
    """Consistent Plotly layout across all charts."""
    base: dict = {
        "title":         {"text": title, "font": {"size": 15, "color": "rgba(200,200,200,0.9)"}, "x": 0, "xanchor": "left"},
        "plot_bgcolor":  "rgba(0,0,0,0)",
        "paper_bgcolor": "rgba(0,0,0,0)",
        "font":          {"size": 12},
        "height":        height,
        "margin":        {"t": 48, "r": 16, "b": 48, "l": 60},
        "hoverlabel":    {"bgcolor": "rgba(30,30,30,0.95)", "font_color": "white", "bordercolor": "rgba(255,255,255,0.1)"},
        "xaxis":         {"showgrid": False, "zeroline": False},
        "yaxis":         {"gridcolor": "rgba(128,128,128,0.12)", "zeroline": False},
    }
    base.update(kwargs)
    return base


def _section_header(title: str, description: str) -> None:
    st.markdown(f"### {title}")
    st.markdown(
        f"<p style='color:{C_MUTED}; font-size:14px; margin-top:-10px; "
        f"margin-bottom:20px; line-height:1.6;'>{description}</p>",
        unsafe_allow_html=True,
    )


def _callout(text: str, kind: str = "info") -> None:
    colors = {"info": C_BLUE, "warning": C_AMBER, "danger": C_RED}
    c = colors.get(kind, C_BLUE)
    st.markdown(
        f"<div style='border-left:3px solid {c}; background:rgba(128,128,128,0.07); "
        f"padding:10px 16px; border-radius:0 6px 6px 0; font-size:13px; "
        f"line-height:1.5; margin-bottom:16px;'>{text}</div>",
        unsafe_allow_html=True,
    )


# ── data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def load_overview() -> pd.DataFrame:
    with get_session() as session:
        supply_sq = (
            select(SupplySnapshot.symbol, func.max(SupplySnapshot.recorded_at).label("ts"))
            .group_by(SupplySnapshot.symbol).subquery()
        )
        latest_supplies = {
            row.symbol: row for row in session.execute(
                select(SupplySnapshot).join(supply_sq,
                    (SupplySnapshot.symbol == supply_sq.c.symbol) &
                    (SupplySnapshot.recorded_at == supply_sq.c.ts))
            ).scalars().all()
        }

        score_sq = (
            select(RiskScore.symbol, func.max(RiskScore.scored_at).label("scored_at"))
            .group_by(RiskScore.symbol).subquery()
        )
        latest_scores = {
            row.symbol: row for row in session.execute(
                select(RiskScore).join(score_sq,
                    (RiskScore.symbol == score_sq.c.symbol) &
                    (RiskScore.scored_at == score_sq.c.scored_at))
            ).scalars().all()
        }

        price_sq = (
            select(PriceSnapshot.symbol, func.max(PriceSnapshot.recorded_at).label("ts"))
            .group_by(PriceSnapshot.symbol).subquery()
        )
        latest_prices = {
            row.symbol: row for row in session.execute(
                select(PriceSnapshot).join(price_sq,
                    (PriceSnapshot.symbol == price_sq.c.symbol) &
                    (PriceSnapshot.recorded_at == price_sq.c.ts))
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
        rows.append({
            "symbol":            symbol,
            "market_cap":        supply * price,
            "supply":            supply,
            "change_7d":         ((supply - prev_week)  / prev_week  * 100) if prev_week  and prev_week  > 0 else None,
            "change_30d":        ((supply - prev_month) / prev_month * 100) if prev_month and prev_month > 0 else None,
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
            .group_by(RiskScore.symbol).subquery()
        )
        scores = session.execute(
            select(RiskScore).join(score_sq,
                (RiskScore.symbol == score_sq.c.symbol) &
                (RiskScore.scored_at == score_sq.c.scored_at))
        ).scalars().all()

        supply_sq = (
            select(SupplySnapshot.symbol, func.max(SupplySnapshot.recorded_at).label("ts"))
            .group_by(SupplySnapshot.symbol).subquery()
        )
        supplies = {
            row.symbol: row.circulating_supply for row in session.execute(
                select(SupplySnapshot).join(supply_sq,
                    (SupplySnapshot.symbol == supply_sq.c.symbol) &
                    (SupplySnapshot.recorded_at == supply_sq.c.ts))
            ).scalars().all()
        }

        price_sq = (
            select(PriceSnapshot.symbol, func.max(PriceSnapshot.recorded_at).label("ts"))
            .group_by(PriceSnapshot.symbol).subquery()
        )
        prices = {
            row.symbol: row for row in session.execute(
                select(PriceSnapshot).join(price_sq,
                    (PriceSnapshot.symbol == price_sq.c.symbol) &
                    (PriceSnapshot.recorded_at == price_sq.c.ts))
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


@st.cache_data(ttl=120, show_spinner=False)
def load_score_explanation(symbol: str) -> dict | None:
    from services.score_explanation import explain_scores

    return explain_scores(symbol)


@st.cache_data(ttl=120, show_spinner=False)
def load_market_changes(limit: int = 30) -> list[dict]:
    from services.market_changes import compute_market_changes

    return compute_market_changes(limit=limit)


@st.cache_data(ttl=120, show_spinner=False)
def load_profile(symbol: str) -> dict | None:
    from services.profile import get_stablecoin_profile

    return get_stablecoin_profile(symbol)


@st.cache_data(ttl=120, show_spinner=False)
def load_data_freshness() -> dict:
    from services.freshness import compute_data_freshness

    return compute_data_freshness()


@st.cache_data(ttl=120, show_spinner=False)
def load_risk_events(limit: int = 300) -> list[dict]:
    from services.risk_events import query_events

    return query_events(limit=limit)


@st.cache_data(ttl=60, show_spinner=False)
def load_pipeline_runs(limit: int = 100) -> dict:
    from services.pipeline_runs import pipeline_status_summary, query_runs

    return {
        "summary": pipeline_status_summary(),
        "runs": query_runs(limit=limit),
    }


@st.cache_data(ttl=120, show_spinner=False)
def load_data_quality(limit: int = 200) -> dict:
    from services.data_validation import query_warnings, warning_summary

    return {
        "summary": warning_summary(),
        "warnings": query_warnings(limit=limit),
    }


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


# ── static data ───────────────────────────────────────────────────────────────

PROVIDER_COSTS = pd.DataFrame([
    {"provider": "DefiLlama", "endpoint": "stablecoins",       "frequency": "daily",    "cost_usd": 0.00},
    {"provider": "DefiLlama", "endpoint": "stablecoin_charts", "frequency": "daily",    "cost_usd": 0.00},
    {"provider": "Binance",   "endpoint": "ticker_price",      "frequency": "1-5 min",  "cost_usd": 0.00},
    {"provider": "Binance",   "endpoint": "order_book_depth",  "frequency": "hourly",   "cost_usd": 0.00},
    {"provider": "Coinbase",  "endpoint": "spot_price",        "frequency": "fallback", "cost_usd": 0.00},
])


# ── styles ────────────────────────────────────────────────────────────────────

def _inject_styles() -> None:
    st.markdown("""
<style>
/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 0;
    border-bottom: 2px solid rgba(128,128,128,0.15);
    margin-bottom: 20px;
}
.stTabs [data-baseweb="tab"] {
    padding: 18px 36px 12px;
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    border-radius: 0;
    border-bottom: 3px solid transparent;
    margin-bottom: -2px;
    justify-content: center;
}
.stTabs [aria-selected="true"] {
    border-bottom: 3px solid #6366f1 !important;
    color: #6366f1 !important;
}
.stTabs [data-baseweb="tab-highlight"],
.stTabs [data-baseweb="tab-border"] { display: none; }

/* ── Metric cards equal height ── */
[data-testid="column"] > div { height: 100%; }

/* ── Cleaner expanders ── */
.streamlit-expanderHeader {
    font-size: 13px !important;
    font-weight: 600 !important;
    color: rgba(90,90,90,0.9) !important;
}

/* ── Tighter selectbox / slider labels ── */
.stSelectbox label, .stSlider label {
    font-size: 12px !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.06em !important;
    opacity: 0.85 !important;
}

/* ── Divider spacing ── */
hr { margin: 28px 0 !important; opacity: 0.15 !important; }
</style>
""", unsafe_allow_html=True)


def _stat_card(label: str, value: str, description: str, accent: str) -> str:
    return (
        f'<div style="padding:20px 22px; border-radius:10px; height:100%;'
        f' border:1px solid rgba(128,128,128,0.13); border-top:3px solid {accent};">'
        f'<div style="font-size:10px; font-weight:800; text-transform:uppercase;'
        f' letter-spacing:0.1em; color:{C_MUTED}; margin-bottom:10px;">{label}</div>'
        f'<div style="font-size:30px; font-weight:800; line-height:1; margin-bottom:10px;">{value}</div>'
        f'<div style="font-size:12px; color:{C_MUTED}; line-height:1.5;">{description}</div>'
        f'</div>'
    )


# ── page sections ─────────────────────────────────────────────────────────────

def render_header(df: pd.DataFrame) -> None:
    st.markdown(
        "<h1 style='margin-bottom:2px;'>Stablecoin Dashboard</h1>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<p style='color:{C_MUTED}; font-size:15px; margin-top:0; margin-bottom:24px;'>"
        "A stablecoin is a cryptocurrency pegged to $1.00 USD. This dashboard tracks supply, "
        "peg stability, liquidity, and risk across the market.</p>",
        unsafe_allow_html=True,
    )
    if df.empty:
        return

    total     = df["supply"].sum()
    avg_dev   = df["peg_deviation_bps"].dropna().mean() if "peg_deviation_bps" in df.columns else None
    high_risk = int((df["overall_score"] < 50).sum())

    peg_accent  = C_AMBER if pd.notna(avg_dev) and avg_dev > 10 else C_GREEN
    risk_accent = C_RED   if high_risk > 0 else C_GREEN

    c1, c2, c3, c4 = st.columns(4, gap="medium")
    c1.markdown(_stat_card(
        "Total Supply", _fmt_supply(total),
        "Combined circulating supply. Because each token targets $1.00, this approximates total market cap.",
        C_BLUE,
    ), unsafe_allow_html=True)
    c2.markdown(_stat_card(
        "Assets Tracked", str(len(df)),
        "Distinct stablecoin assets with active risk scores in the database.",
        C_PRIMARY,
    ), unsafe_allow_html=True)
    c3.markdown(_stat_card(
        "Avg Peg Deviation",
        f"{avg_dev:.1f} bps" if pd.notna(avg_dev) else "—",
        "Average distance from $1.00 with live price data. 1 bps = $0.0001. Amber above 10 bps.",
        peg_accent,
    ), unsafe_allow_html=True)
    c4.markdown(_stat_card(
        "High Risk Assets", str(high_risk),
        "Assets scoring below 50 / 100. May signal a weak peg, thin liquidity, or stale reserves.",
        risk_accent,
    ), unsafe_allow_html=True)

    st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)


SEVERITY_COLORS = {
    "high":   C_RED,
    "medium": C_ORANGE,
    "low":    C_AMBER,
    "info":   C_BLUE,
}

METRIC_LABELS = {
    "supply":            "Supply",
    "peg_deviation_bps": "Peg Deviation",
    "liquidity_usd":     "Liquidity Depth",
    "overall_score":     "Risk Score",
}

EVENT_TYPE_LABELS = {
    "PEG_DEVIATION":  "Peg Deviation",
    "LIQUIDITY_DROP": "Liquidity Drop",
    "SUPPLY_SHOCK":   "Supply Shock",
    "SCORE_CHANGE":   "Score Change",
    "RESERVE_STALE":  "Reserve Stale",
    "API_FAILURE":    "API Failure",
}

WARNING_TYPE_LABELS = {
    "IMPOSSIBLE_PRICE":           "Impossible Price",
    "NON_POSITIVE_SUPPLY":        "Non-Positive Supply",
    "PEG_DEVIATION_MISMATCH":     "Peg Deviation Mismatch",
    "SUPPLY_JUMP":                "Implausible Supply Jump",
    "DUPLICATE_SNAPSHOT":         "Duplicate Snapshot",
    "MISSING_CHAIN_DISTRIBUTION": "Missing Chain Data",
}


def _fmt_metric_value(metric: str, value: float | None) -> str:
    if value is None:
        return "—"
    if metric in ("supply", "liquidity_usd"):
        return _fmt_supply(value)
    if metric == "peg_deviation_bps":
        return f"{value:.1f} bps"
    return f"{value:.0f}"


def _fmt_change(change: dict) -> str:
    pct = change.get("percent_change")
    if pct is not None:
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.1f}%"
    abs_change = change.get("absolute_change") or 0
    sign = "+" if abs_change >= 0 else ""
    if change["metric"] == "peg_deviation_bps":
        return f"{sign}{abs_change:.1f} bps"
    return f"{sign}{abs_change:.0f} pts"


def render_market_changes(changes: list[dict]) -> None:
    _section_header(
        "Market Changes",
        "The biggest moves since the prior snapshot, so you don't have to read every chart. "
        "Ranked by severity — supply over 7 days, peg / liquidity / risk score over 24 hours.",
    )

    if not changes:
        _callout(
            "Not enough history yet to compute market changes. "
            "This populates once the pipelines have run on at least two snapshots.",
            "info",
        )
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        return

    for change in changes[:5]:
        color = SEVERITY_COLORS.get(change["severity"], C_BLUE)
        st.markdown(
            f"<div style='border-left:3px solid {color}; background:rgba(128,128,128,0.07); "
            f"padding:8px 14px; border-radius:0 6px 6px 0; font-size:13px; "
            f"line-height:1.5; margin-bottom:8px;'>"
            f"<span style='color:{color}; font-weight:700; text-transform:uppercase; "
            f"font-size:10px; letter-spacing:0.08em; margin-right:10px;'>{change['severity']}</span>"
            f"{change['summary']}</div>",
            unsafe_allow_html=True,
        )

    with st.expander("All movers"):
        table = pd.DataFrame([
            {
                "Asset":    c["asset"],
                "Metric":   METRIC_LABELS.get(c["metric"], c["metric"]),
                "Previous": _fmt_metric_value(c["metric"], c["previous_value"]),
                "Current":  _fmt_metric_value(c["metric"], c["current_value"]),
                "Change":   _fmt_change(c),
                "Window":   c["comparison_window"],
                "Severity": c["severity"].capitalize(),
            }
            for c in changes
        ])

        def _color_sev(val: str) -> str:
            c = SEVERITY_COLORS.get(val.lower(), "")
            return f"color:{c}; font-weight:700;" if c else ""

        st.dataframe(
            table.style.map(_color_sev, subset=["Severity"]),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)


# ── asset profile ───────────────────────────────────────────────────────────────

STATUS_COLORS = {
    "fresh":   C_GREEN,
    "delayed": C_AMBER,
    "stale":   C_RED,
    "missing": C_MUTED,
}
STATUS_LABELS = {
    "fresh":   "Fresh",
    "delayed": "Delayed",
    "stale":   "Stale",
    "missing": "No data",
}
FRESHNESS_SOURCES = [("price", "Price"), ("supply", "Supply"), ("scores", "Scores"), ("reserve", "Reserve")]

# Per-provider request health (distinct from the per-source freshness statuses).
PROVIDER_STATUS_COLORS = {"healthy": C_GREEN, "failing": C_RED, "missing": C_MUTED}
PROVIDER_STATUS_LABELS = {"healthy": "Healthy", "failing": "Failing", "missing": "No calls"}


def _fmt_age(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "—"
    mins = int(age_seconds / 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _freshness_badges(freshness: dict) -> None:
    """Render a row of per-source freshness pills (green/amber/red/grey)."""
    chips = []
    for key, label in FRESHNESS_SOURCES:
        info = freshness.get(key) or {}
        status = info.get("status", "missing")
        color = STATUS_COLORS.get(status, C_MUTED)
        status_txt = STATUS_LABELS.get(status, status)
        age_txt = "" if status == "missing" else f" · {_fmt_age(info.get('age_seconds'))}"
        chips.append(
            f"<span style='display:inline-flex; align-items:center; gap:7px; "
            f"padding:6px 12px; margin:0 8px 8px 0; border-radius:999px; "
            f"border:1px solid rgba(128,128,128,0.18); font-size:12px;'>"
            f"<span style='width:8px;height:8px;border-radius:50%;background:{color};'></span>"
            f"<span style='color:{C_MUTED};'>{label}</span>"
            f"<span style='font-weight:700;color:{color};'>{status_txt}{age_txt}</span>"
            f"</span>"
        )
    st.markdown("<div style='margin-bottom:10px;'>" + "".join(chips) + "</div>", unsafe_allow_html=True)


def _profile_history_charts(symbol: str) -> None:
    """Price (24h) and supply (30d) side by side, then score history (30d)."""
    left, right = st.columns(2, gap="large")

    with left:
        st.markdown("**Price — last 24h**")
        price_hist = load_price_history(symbol, hours=24)
        if not price_hist.empty and len(price_hist) > 1:
            fig = go.Figure()
            fig.add_hline(y=1.0, line_dash="dash", line_color="rgba(128,128,128,0.4)",
                          annotation_text="$1.00", annotation_font_size=10)
            fig.add_trace(go.Scatter(
                x=price_hist["recorded_at"], y=price_hist["price"], mode="lines",
                line=dict(color=C_PRIMARY, width=2),
                hovertemplate="<b>%{x}</b><br>$%{y:.4f}<extra></extra>",
            ))
            fig.update_layout(**_chart_layout(
                title="", height=260,
                yaxis=dict(gridcolor="rgba(128,128,128,0.12)", zeroline=False, tickformat="$.4f"),
            ))
            st.plotly_chart(fig, use_container_width=True)
        else:
            _callout("Price history appears once the pipeline has run a few times.", "info")

    with right:
        st.markdown("**Supply — last 30d**")
        supply_hist = load_supply_history(symbol, days=30)
        if not supply_hist.empty and len(supply_hist) > 1:
            # Collapse same-timestamp ticker collisions to the dominant value.
            ss = (supply_hist.sort_values("circulating_supply", ascending=False)
                  .drop_duplicates(subset=["recorded_at"]).sort_values("recorded_at"))
            fig = go.Figure(go.Scatter(
                x=ss["recorded_at"], y=ss["circulating_supply"], mode="lines",
                fill="tozeroy", line=dict(color=C_BLUE, width=2),
                fillcolor="rgba(59,130,246,0.08)",
                hovertemplate="<b>%{x}</b><br>$%{y:,.0f}<extra></extra>",
            ))
            fig.update_layout(**_chart_layout(
                title="", height=260,
                yaxis=dict(gridcolor="rgba(128,128,128,0.12)", zeroline=False, tickprefix="$", tickformat=",.0f"),
            ))
            st.plotly_chart(fig, use_container_width=True)
        else:
            _callout("Supply history appears after the pipeline has run on multiple days.", "info")

    st.markdown("**Risk scores — last 30d**")
    score_hist = load_score_history(symbol, days=30)
    if not score_hist.empty and len(score_hist) > 1:
        fig = go.Figure()
        line_styles = ["solid", "dash", "dot", "dashdot"]
        for col, label, color, dash in zip(SCORE_COLS, SCORE_LABELS, SCORE_COLORS, line_styles):
            fig.add_trace(go.Scatter(
                x=score_hist["scored_at"], y=score_hist[col], name=label, mode="lines",
                line=dict(color=color, dash=dash, width=2),
                hovertemplate=f"<b>%{{x}}</b><br>{label}: %{{y:.0f}}<extra></extra>",
            ))
        fig.update_layout(**_chart_layout(
            title="", height=280,
            yaxis=dict(range=[0, 100], title="Score", gridcolor="rgba(128,128,128,0.12)", zeroline=False),
            legend=dict(orientation="h", yanchor="bottom", y=1.01),
        ))
        st.plotly_chart(fig, use_container_width=True)
    else:
        _callout("Score history appears after the pipeline has run on multiple days.", "info")


def render_profile(symbol: str) -> None:
    """Full single-asset profile: metrics, scores, history, chains, reserves.

    Every section makes missing data explicit rather than guessing.
    """
    profile = load_profile(symbol)
    if profile is None:
        _callout(f"No data found for <strong>{symbol}</strong>.", "info")
        return

    name     = profile.get("name") or symbol
    issuer   = profile.get("issuer")
    peg_mech = profile.get("peg_mechanism")
    subtitle = " · ".join([p for p in (issuer, peg_mech) if p]) or "Issuer and peg mechanism not recorded"

    st.markdown(
        f"<h2 style='margin-bottom:2px;'>{name} "
        f"<span style='color:{C_MUTED}; font-weight:600; font-size:18px;'>{symbol}</span></h2>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<p style='color:{C_MUTED}; font-size:14px; margin-top:0; margin-bottom:16px;'>{subtitle}</p>",
        unsafe_allow_html=True,
    )
    if not profile.get("registered"):
        _callout(
            "This asset is not in the curated registry, so issuer and peg details are unavailable. "
            "Metrics below come from live snapshots.",
            "info",
        )

    _freshness_badges(profile.get("freshness", {}))

    # ── headline metrics ──────────────────────────────────────────────────────
    price  = profile.get("price")  or {}
    supply = profile.get("supply") or {}
    scores = profile.get("scores") or {}

    price_val  = price.get("price")
    bps        = price.get("peg_deviation_bps")
    supply_val = supply.get("circulating_supply")
    overall    = scores.get("overall_score")
    risk       = scores.get("risk_label", "—")

    peg_accent = C_GREEN
    if bps is not None:
        peg_accent = C_RED if bps > 50 else C_AMBER if bps > 10 else C_GREEN
    risk_accent = RISK_COLORS.get(risk, C_MUTED)

    c1, c2, c3, c4 = st.columns(4, gap="medium")
    c1.markdown(_stat_card(
        "Price", f"${price_val:.4f}" if price_val is not None else "—",
        f"Latest from {price.get('source', '—')}." if price_val is not None else "No price snapshot yet.",
        C_BLUE,
    ), unsafe_allow_html=True)
    c2.markdown(_stat_card(
        "Peg Deviation", f"{bps:.1f} bps" if bps is not None else "—",
        "Distance from $1.00. 1 bps = $0.0001.", peg_accent,
    ), unsafe_allow_html=True)
    c3.markdown(_stat_card(
        "Circulating Supply", _fmt_supply(supply_val),
        f"Top chain: {supply.get('top_chain')}" if supply.get("top_chain") else "Across all chains.",
        C_PRIMARY,
    ), unsafe_allow_html=True)
    c4.markdown(_stat_card(
        "Risk Score", f"{overall:.0f} / 100" if overall is not None else "—",
        risk if overall is not None else "Not scored yet.", risk_accent,
    ), unsafe_allow_html=True)

    st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)

    # ── score breakdown ───────────────────────────────────────────────────────
    if scores:
        _section_header(
            "Risk Score Breakdown",
            "How the overall score decomposes across four weighted dimensions: "
            "peg 35% · liquidity 25% · reserve 25% · adoption 15%.",
        )
        vals = [scores.get(c) for c in SCORE_COLS]
        fig = go.Figure(go.Bar(
            x=vals, y=SCORE_LABELS, orientation="h", marker_color=SCORE_COLORS,
            text=[f"{v:.0f}" if v is not None else "—" for v in vals], textposition="auto",
            hovertemplate="%{y}: %{x:.0f}<extra></extra>",
        ))
        fig.update_layout(**_chart_layout(
            title="", height=230,
            xaxis=dict(range=[0, 100], title="Score (higher = safer)", gridcolor="rgba(128,128,128,0.12)", zeroline=False),
            yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)", zeroline=False),
            margin={"t": 20, "r": 16, "b": 40, "l": 90},
        ))
        st.plotly_chart(fig, use_container_width=True)
        explanation = load_score_explanation(symbol)
        if explanation:
            if explanation.get("weakest_explanation"):
                _callout(
                    explanation["weakest_explanation"],
                    "warning" if explanation.get("weakest_component") else "info",
                )
            delta = explanation.get("delta") or {}
            if delta.get("summary") and delta.get("available"):
                change = delta.get("overall_change")
                _callout(
                    f"<strong>Since the last snapshot:</strong> {delta['summary']}",
                    "danger" if (change is not None and change < 0) else "info",
                )
        else:
            present = [(lbl, v) for lbl, v in zip(SCORE_LABELS, vals) if v is not None]
            if present:
                weakest = min(present, key=lambda t: t[1])
                _callout(f"Lowest dimension: <strong>{weakest[0]}</strong> at {weakest[1]:.0f} / 100 — "
                         "the biggest drag on the overall score.", "info")
    else:
        _section_header("Risk Score Breakdown", "Risk scores have not been computed for this asset yet.")
        _callout("No risk score available.", "info")

    st.divider()

    # ── history ───────────────────────────────────────────────────────────────
    _section_header("History", "How this asset's price, supply, and risk scores have moved over time.")
    _profile_history_charts(symbol)

    st.divider()

    # ── order book liquidity ──────────────────────────────────────────────────
    _section_header(
        "Order Book Liquidity",
        "How much depth sits on each side of the book on the primary exchange. "
        "Thin depth means the peg is easier to push off $1.00.",
    )
    lc1, lc2, lc3 = st.columns(3, gap="medium")
    lc1.markdown(_stat_card("Bid Depth", _fmt_supply(price.get("bid_depth_usd")),
                            "Buy-side support under the price.", C_GREEN), unsafe_allow_html=True)
    lc2.markdown(_stat_card("Ask Depth", _fmt_supply(price.get("ask_depth_usd")),
                            "Sell-side liquidity above the price.", C_ORANGE), unsafe_allow_html=True)
    lc3.markdown(_stat_card("Total Depth", _fmt_supply(price.get("total_depth_usd")),
                            "Combined bid + ask depth in USD.", C_BLUE), unsafe_allow_html=True)

    st.markdown("<div style='margin-top:24px;'></div>", unsafe_allow_html=True)
    st.divider()

    # ── chain distribution ────────────────────────────────────────────────────
    _section_header(
        "Chain Distribution",
        "Where this asset's supply lives. Heavy concentration on one chain is a platform risk.",
    )
    chains = supply.get("chains") or []
    if chains:
        top_chains = chains[:12]
        fig = go.Figure(go.Bar(
            x=[c["supply"] for c in top_chains], y=[c["chain"] for c in top_chains], orientation="h",
            marker=dict(color=[c["supply"] for c in top_chains], colorscale="Blues", showscale=False),
            hovertemplate="<b>%{y}</b><br>$%{x:,.0f}<extra></extra>",
        ))
        fig.update_layout(**_chart_layout(
            title="", height=max(220, len(top_chains) * 30),
            xaxis=dict(title="Supply (USD)", gridcolor="rgba(128,128,128,0.12)", zeroline=False),
            yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)", zeroline=False),
            margin={"t": 20, "r": 16, "b": 40, "l": 110},
        ))
        st.plotly_chart(fig, use_container_width=True)
        conc = supply.get("top_chain_pct")
        if conc is not None and conc >= 75:
            _callout(
                f"<strong>Concentration risk:</strong> {conc:.0f}% of supply is on {supply.get('top_chain')}. "
                "A single-chain outage or exploit would affect most of the supply.",
                "warning",
            )
    else:
        _callout("Chain distribution data is unavailable for this asset.", "info")

    st.divider()

    # ── reserve composition ───────────────────────────────────────────────────
    _section_header(
        "Reserve Composition",
        "What backs the token, per the issuer's latest attestation. Reserve data is curated, not live.",
    )
    reserve = profile.get("reserve")
    if reserve:
        meta_bits = []
        if reserve.get("auditor"):
            meta_bits.append(f"Auditor: <strong>{reserve['auditor']}</strong>")
        if reserve.get("report_date"):
            meta_bits.append(f"Report date: {reserve['report_date']}")
        if reserve.get("report_url"):
            meta_bits.append(f"<a href='{reserve['report_url']}' target='_blank'>Source</a>")
        if meta_bits:
            st.markdown(
                f"<p style='color:{C_MUTED}; font-size:13px; margin-bottom:12px;'>" + " · ".join(meta_bits) + "</p>",
                unsafe_allow_html=True,
            )
        if reserve.get("is_stale"):
            _callout(
                f"<strong>Stale attestation:</strong> the reserve report is {reserve.get('age_days')} days old "
                "(over 90 days). Treat composition with caution.",
                "warning",
            )
        elif reserve.get("report_date") is None:
            _callout("No attestation date is recorded for this asset's reserves.", "info")

        comp = reserve.get("composition") or {}
        if comp:
            palette = [C_BLUE, C_PRIMARY, C_GREEN, C_AMBER, C_ORANGE, C_RED, "#ec4899", "#14b8a6"]
            fig = go.Figure(go.Pie(
                labels=[k.replace("_", " ") for k in comp.keys()],
                values=list(comp.values()), hole=0.55,
                marker=dict(colors=palette[:len(comp)]),
                hovertemplate="%{label}: %{percent}<extra></extra>",
            ))
            fig.update_layout(**_chart_layout(title="", height=300, margin={"t": 20, "r": 16, "b": 16, "l": 16}))
            st.plotly_chart(fig, use_container_width=True)
        else:
            _callout("Reserve composition breakdown is unavailable.", "info")
    else:
        _callout("No reserve report is recorded for this asset.", "info")


def render_profile_tab(df: pd.DataFrame) -> None:
    _section_header(
        "Asset Profile",
        "A deep dive on one stablecoin — price, supply, chains, reserves, and risk in one place. "
        "Pick an asset below, or select a row in the Overview tab.",
    )

    if df.empty:
        _callout("No data yet. Run the ingestion pipelines first.", "info")
        return

    symbols = df["symbol"].tolist()

    # Default selection priority: a row picked in Overview, then a ?symbol= deep link.
    default = st.session_state.get("profile_symbol")
    if default not in symbols:
        qp = st.query_params.get("symbol")
        default = qp.upper() if qp else None
    index = symbols.index(default) if default in symbols else 0

    symbol = st.selectbox("Select asset", symbols, index=index, key="profile_select")
    st.session_state["profile_symbol"] = symbol
    st.query_params["symbol"] = symbol

    st.markdown("<div style='margin-top:8px;'></div>", unsafe_allow_html=True)
    render_profile(symbol)


def render_overview_tab(df: pd.DataFrame) -> None:
    _section_header(
        "Market Overview",
        "Every tracked stablecoin at a glance, sorted by circulating supply. "
        "Click any column header to sort. Risk Level is colour-coded — green is safest, red is most concerning.",
    )

    with st.expander("What does each column mean?"):
        st.markdown("""
| Column | What it measures |
|---|---|
| **Market Cap** | Total value of all tokens in circulation. For a $1.00-pegged coin this equals supply. |
| **Supply** | Number of tokens outstanding, converted to USD at the current price. |
| **7D / 30D Change** | Supply growth or contraction. Rapid growth suggests rising adoption; a sharp drop may signal redemptions or distress. |
| **Top Chain** | The blockchain holding the largest share of supply (e.g. Ethereum, Tron, BSC). |
| **Peg Deviation** | Distance from $1.00, in basis points. 1 bps = $0.0001. Healthy coins stay within 10 bps. |
| **Risk Score** | Composite score 0–100. Higher = safer. Weighted across peg, liquidity, reserves, and adoption. |
| **Risk Level** | Plain-English label: Low Risk (80+), Moderate (60–79), Elevated (40–59), High Risk (<40). |
| **Data Freshness** | How recently the risk score was calculated. |
        """)

    if df.empty:
        _callout("No data yet. Run the ingestion pipelines first.", "info")
        return

    # Filters row
    fc1, fc2 = st.columns([2, 1])
    risk_filter = fc1.selectbox(
        "Filter by risk level",
        ["All", "Low Risk", "Moderate", "Elevated", "High Risk"],
        key="overview_risk_filter",
    )
    search = fc2.text_input("Search symbol", placeholder="e.g. USDT", key="overview_search")

    filtered = df.copy()
    if risk_filter != "All":
        filtered = filtered[filtered["risk_label"] == risk_filter]
    if search:
        filtered = filtered[filtered["symbol"].str.upper().str.contains(search.upper())]

    st.caption(f"Showing {len(filtered)} of {len(df)} assets")

    display = filtered[[
        "symbol", "market_cap", "supply",
        "change_7d", "change_30d",
        "top_chain", "peg_deviation_bps",
        "overall_score", "risk_label", "scored_at",
    ]].copy()

    display["market_cap"]    = display["market_cap"].apply(_fmt_supply)
    display["supply"]        = display["supply"].apply(_fmt_supply)
    display["change_7d"]     = display["change_7d"].apply(_fmt_pct)
    display["change_30d"]    = display["change_30d"].apply(_fmt_pct)
    display["peg_deviation_bps"] = display["peg_deviation_bps"].apply(
        lambda v: f"{v:.1f} bps" if pd.notna(v) else "—"
    )
    display["overall_score"] = display["overall_score"].apply(lambda v: f"{v:.0f} / 100")
    display["scored_at"]     = display["scored_at"].apply(_fmt_freshness)

    display.columns = [
        "Symbol", "Market Cap", "Supply",
        "7D Change", "30D Change",
        "Top Chain", "Peg Deviation",
        "Risk Score", "Risk Level", "Data Freshness",
    ]

    def _color_risk(val: str) -> str:
        c = RISK_COLORS.get(val, "")
        return f"color:{c}; font-weight:700;" if c else ""

    st.caption("Select a row to open that asset's full profile below.")
    event = st.dataframe(
        display.style.map(_color_risk, subset=["Risk Level"]),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="overview_table",
    )

    try:
        selected_rows = event.selection["rows"]
    except (AttributeError, KeyError, TypeError):
        selected_rows = []

    if selected_rows:
        sel_symbol = filtered.iloc[selected_rows[0]]["symbol"]
        st.session_state["profile_symbol"] = sel_symbol
        st.query_params["symbol"] = sel_symbol
        st.divider()
        render_profile(sel_symbol)


def render_supply_tab(df: pd.DataFrame) -> None:
    _section_header(
        "Circulating Supply",
        "How much of each stablecoin exists, valued in USD. "
        "Because each token targets $1.00, supply is a direct measure of adoption and usage.",
    )

    with st.expander("Why does supply matter?"):
        st.markdown("""
**Large, growing supply** reflects strong adoption. Tether (USDT) and USD Coin (USDC) are the
two largest and serve as the backbone of crypto trading.

**Sudden supply drops** can signal a bank-run dynamic — holders redeeming tokens faster than
new ones are minted — which sometimes precedes a peg break.

**Supply concentration** on a single chain is a platform risk. If that chain has an outage or
exploit, the stablecoin becomes temporarily unusable.
        """)

    if df.empty:
        _callout("No supply data yet. Run <code>python -m pipelines.update_supply</code>.", "info")
        return

    sorted_df = df.sort_values("circulating_supply", ascending=False)

    # Controls
    cc1, cc2 = st.columns([4, 1])
    top_n     = cc1.slider("Assets to show", min_value=5, max_value=50, value=25, step=5, key="supply_top_n")
    log_scale = cc2.checkbox("Log scale", value=True, key="supply_log",
                             help="Log scale compresses the range so large and small assets are both visible.")

    chart_df = sorted_df.head(top_n)
    fig = go.Figure(go.Bar(
        x=chart_df["symbol"],
        y=chart_df["circulating_supply"],
        marker=dict(
            color=chart_df["circulating_supply"],
            colorscale="Blues",
            showscale=False,
        ),
        hovertemplate="<b>%{x}</b><br>Supply: $%{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(**_chart_layout(
        title=f"Top {top_n} Stablecoins by Circulating Supply",
        yaxis=dict(
            type="log" if log_scale else "linear",
            gridcolor="rgba(128,128,128,0.12)",
            zeroline=False,
            title="Supply (USD)",
        ),
    ))
    st.plotly_chart(fig, use_container_width=True)

    if log_scale:
        _callout(
            "Log scale is on — each step up the y-axis is a 10× increase. "
            "Uncheck to see absolute values.",
            "info",
        )

    st.divider()
    _section_header(
        "Supply History",
        "How one asset's supply has changed over the past 30 days. "
        "Steady growth is healthy; a cliff-edge drop warrants investigation.",
    )
    sym = st.selectbox("Select asset", sorted_df["symbol"].tolist(), key="supply_chain_sym")
    history = load_supply_history(sym, days=30)
    if not history.empty and len(history) > 1:
        fig2 = go.Figure(go.Scatter(
            x=history["recorded_at"],
            y=history["circulating_supply"],
            mode="lines",
            fill="tozeroy",
            line=dict(color=C_BLUE, width=2),
            fillcolor="rgba(59,130,246,0.08)",
            hovertemplate="<b>%{x}</b><br>Supply: $%{y:,.0f}<extra></extra>",
        ))
        fig2.update_layout(**_chart_layout(
            title=f"{sym} — 30-Day Supply",
            height=300,
            yaxis=dict(gridcolor="rgba(128,128,128,0.12)", zeroline=False, tickprefix="$", tickformat=",.0f"),
        ))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        _callout("Supply history will appear after the pipeline has run on multiple days.", "info")

    st.divider()
    table = sorted_df[["symbol", "circulating_supply"]].copy()
    table["circulating_supply"] = table["circulating_supply"].apply(_fmt_supply)
    table.columns = ["Symbol", "Circulating Supply"]
    st.dataframe(table, use_container_width=True, hide_index=True)


def render_peg_tab(df: pd.DataFrame) -> None:
    _section_header(
        "Peg Deviation",
        "How far each stablecoin's price has drifted from $1.00, measured in basis points (bps). "
        "1 bps = $0.0001. Healthy coins rarely exceed 10 bps.",
    )

    with st.expander("What is a basis point, and when should I be concerned?"):
        st.markdown("""
**1 basis point (bps) = 0.01% = $0.0001**

| Zone | Deviation | Dollar value | What it means |
|---|---|---|---|
| Normal | < 10 bps | < $0.0010 | Within expected market noise |
| Elevated | 10–50 bps | $0.0010–$0.0050 | Worth monitoring |
| Warning | 50–100 bps | $0.0050–$0.0100 | Liquidity or confidence stress |
| Critical | > 100 bps | > $0.0100 | Potential peg break |

**Why do stablecoins lose their peg?**
- **Fiat-backed (USDT, USDC):** Solvency concerns or high redemption demand.
- **Crypto-backed (DAI):** Collateral value drops faster than the protocol can liquidate.
- **Algorithmic:** Supply–demand imbalance. Terra/LUNA (2022) is the most prominent failure.
        """)

    if df.empty:
        _callout("No price data yet. Run <code>python -m pipelines.update_prices</code>.", "info")
        return

    peg_df = df.dropna(subset=["peg_deviation_bps"]).sort_values("peg_deviation_bps", ascending=False)

    # Alert if any asset is critically off-peg
    critical = peg_df[peg_df["peg_deviation_bps"] > 50]
    if not critical.empty:
        names = ", ".join(critical["symbol"].tolist())
        _callout(f"<strong>Warning:</strong> {names} exceed 50 bps deviation — potential peg stress.", "warning")

    # Colour bars by severity zone
    def _bar_color(bps: float) -> str:
        if bps > 50:
            return C_RED
        if bps > 10:
            return C_AMBER
        return C_GREEN

    colors = peg_df["peg_deviation_bps"].apply(_bar_color).tolist()

    fig = go.Figure(go.Bar(
        x=peg_df["symbol"],
        y=peg_df["peg_deviation_bps"],
        marker_color=colors,
        hovertemplate="<b>%{x}</b><br>Deviation: %{y:.1f} bps<extra></extra>",
    ))
    fig.add_hline(y=10, line_dash="dot", line_color=C_AMBER, line_width=1.5,
                  annotation_text="10 bps — elevated", annotation_font_size=11)
    fig.add_hline(y=50, line_dash="dot", line_color=C_RED, line_width=1.5,
                  annotation_text="50 bps — warning", annotation_font_size=11)
    fig.update_layout(**_chart_layout(
        title="Peg Deviation by Asset  (green < 10 bps · amber 10–50 · red > 50)",
        yaxis=dict(gridcolor="rgba(128,128,128,0.12)", zeroline=False, title="Deviation (bps)"),
    ))
    st.plotly_chart(fig, use_container_width=True)

    st.divider()
    _section_header(
        "Price History",
        "Price snapshots for a selected asset over the past 24 hours. "
        "The dashed line marks the $1.00 target.",
    )
    sym = st.selectbox("Select asset", peg_df["symbol"].tolist(), key="peg_sym")
    history = load_price_history(sym, hours=24)
    if not history.empty and len(history) > 1:
        fig2 = go.Figure()
        fig2.add_hline(y=1.0, line_dash="dash", line_color="rgba(128,128,128,0.4)",
                       annotation_text="$1.00 target", annotation_font_size=11)
        fig2.add_trace(go.Scatter(
            x=history["recorded_at"],
            y=history["price"],
            mode="lines+markers",
            line=dict(color=C_PRIMARY, width=2),
            marker=dict(size=5),
            hovertemplate="<b>%{x}</b><br>Price: $%{y:.4f}<extra></extra>",
        ))
        fig2.update_layout(**_chart_layout(
            title=f"{sym} — 24-Hour Price",
            height=300,
            yaxis=dict(gridcolor="rgba(128,128,128,0.12)", zeroline=False, tickformat="$.4f"),
        ))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        _callout("Price history will appear after the pipeline has run multiple times.", "info")

    st.divider()
    table = peg_df[["symbol", "price", "peg_deviation_bps", "bid_depth_usd", "ask_depth_usd"]].copy()
    table.columns = ["Symbol", "Price", "Deviation (bps)", "Bid Depth (USD)", "Ask Depth (USD)"]
    st.dataframe(table, use_container_width=True, hide_index=True)


def _score_accent(score: float | None) -> str:
    """Colour a 0–100 dimension score on the same bands as the risk label."""
    if score is None:
        return C_MUTED
    if score >= 80:
        return C_GREEN
    if score >= 60:
        return C_AMBER
    if score >= 40:
        return C_ORANGE
    return C_RED


def render_score_explanation(symbol: str) -> None:
    """Explain *why* one asset has its score: inputs, weights, drag, and delta.

    Shared between the Risk Scores tab and the Asset Profile page so the
    drilldown reads identically wherever a user lands.
    """
    explanation = load_score_explanation(symbol)
    if explanation is None:
        _callout(f"No risk score for <strong>{symbol}</strong> yet.", "info")
        return

    overall = explanation.get("overall_score")
    _callout(
        f"<strong>{symbol}</strong> overall score is "
        f"<strong>{overall:.0f} / 100</strong> ({explanation.get('risk_label', '—')}) — "
        "a weighted blend of the four dimensions below. Higher is safer.",
        "info",
    )

    # Per-dimension cards: score, weight, point contribution, and what drove it.
    components = explanation.get("components", [])
    weakest = explanation.get("weakest_component")
    cols = st.columns(len(components), gap="medium") if components else []
    for col, comp in zip(cols, components):
        flag = " ⚠️" if comp["key"] == weakest else ""
        desc = (
            f"{comp['weight_pct']:.0f}% weight · contributes "
            f"{comp['weighted_contribution']:.1f} pts to the overall.<br>"
            f"<span style='opacity:0.85;'>{comp['detail']}</span>"
        )
        col.markdown(
            _stat_card(
                comp["label"] + flag,
                f"{comp['score']:.0f}/100",
                desc,
                _score_accent(comp["score"]),
            ),
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin-top:18px;'></div>", unsafe_allow_html=True)

    if explanation.get("weakest_explanation"):
        _callout(explanation["weakest_explanation"], "warning" if weakest else "info")

    delta = explanation.get("delta") or {}
    if delta.get("summary"):
        change = delta.get("overall_change")
        kind = "info"
        if change is not None and delta.get("available"):
            kind = "danger" if change < 0 else "info"
        _callout(f"<strong>Since the last snapshot:</strong> {delta['summary']}", kind)


def render_risk_tab(df: pd.DataFrame) -> None:
    _section_header(
        "Risk Scores",
        "Each stablecoin is rated 0–100 across four dimensions. "
        "Higher scores mean lower risk. The overall score is a weighted average.",
    )

    with st.expander("How are scores calculated?"):
        st.markdown("""
| Dimension | Weight | How it is scored |
|---|---|---|
| **Peg** | 35% | 100 = perfect $1.00; drops to 0 at 100 bps deviation |
| **Liquidity** | 25% | 100 = $50M+ combined bid/ask order book depth |
| **Reserve** | 25% | Based on report age and whether an independent auditor signed off |
| **Adoption** | 15% | 100 = $5B+ circulating supply; scales linearly |

**Overall = Peg × 0.35 + Liquidity × 0.25 + Reserve × 0.25 + Adoption × 0.15**

Risk levels: **Low Risk** 80+  ·  **Moderate** 60–79  ·  **Elevated** 40–59  ·  **High Risk** < 40

*Scores are a quantitative starting point, not financial advice.*
        """)

    if df.empty:
        _callout("No scores yet. Run <code>python -m pipelines.score_stablecoins</code>.", "info")
        return

    sorted_df = df.sort_values("circulating_supply", ascending=False)

    # Risk distribution callout
    counts = sorted_df["risk_label"].value_counts()
    dist_parts = [f"<strong style='color:{RISK_COLORS[k]}'>{v} {k}</strong>"
                  for k, v in counts.items() if k in RISK_COLORS]
    if dist_parts:
        _callout("Distribution: " + " · ".join(dist_parts), "info")

    top_n    = st.slider("Assets to show (by supply)", min_value=5, max_value=50, value=15, step=5)
    chart_df = sorted_df.head(top_n).sort_values("overall_score", ascending=False)

    # Horizontal grouped bar — much easier to read with many assets
    fig = go.Figure()
    for col, label, color in zip(SCORE_COLS, SCORE_LABELS, SCORE_COLORS):
        fig.add_trace(go.Bar(
            name=label,
            y=chart_df["symbol"],
            x=chart_df[col],
            orientation="h",
            marker_color=color,
            hovertemplate=f"<b>%{{y}}</b><br>{label}: %{{x:.0f}}<extra></extra>",
        ))
    fig.update_layout(**_chart_layout(
        title=f"Top {top_n} Assets — Score Breakdown (higher = safer)",
        height=max(380, top_n * 28),
        barmode="group",
        xaxis=dict(range=[0, 100], title="Score", gridcolor="rgba(128,128,128,0.12)", zeroline=False),
        yaxis=dict(gridcolor="rgba(0,0,0,0)", zeroline=False, autorange="reversed"),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        margin={"t": 80, "r": 16, "b": 40, "l": 80},
    ))
    st.plotly_chart(fig, use_container_width=True)

    st.divider()
    _section_header(
        "Score History",
        "How an asset's scores have changed over the past 30 days. "
        "A declining peg or liquidity score can be an early warning sign.",
    )
    sym     = st.selectbox("Select asset", sorted_df["symbol"].tolist(), key="risk_history_sym")
    history = load_score_history(sym, days=30)
    if not history.empty and len(history) > 1:
        fig2 = go.Figure()
        line_styles = ["solid", "dash", "dot", "dashdot"]
        for col, label, color, dash in zip(SCORE_COLS, SCORE_LABELS, SCORE_COLORS, line_styles):
            fig2.add_trace(go.Scatter(
                x=history["scored_at"], y=history[col],
                name=label, mode="lines",
                line=dict(color=color, dash=dash, width=2),
                hovertemplate=f"<b>%{{x}}</b><br>{label}: %{{y:.0f}}<extra></extra>",
            ))
        fig2.update_layout(**_chart_layout(
            title=f"{sym} — Score History (30 days)",
            height=320,
            yaxis=dict(range=[0, 100], title="Score", gridcolor="rgba(128,128,128,0.12)", zeroline=False),
            legend=dict(orientation="h", yanchor="bottom", y=1.01),
        ))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        _callout("Score history will appear after the pipeline has run on multiple days.", "info")

    st.divider()
    _section_header(
        f"Why does {sym} score what it does?",
        "The inputs behind each dimension, how many points each contributes, "
        "what is dragging the score down most, and how it moved since the last snapshot.",
    )
    render_score_explanation(sym)

    st.divider()
    display = sorted_df.head(top_n).sort_values("overall_score", ascending=False)[[
        "symbol", "overall_score", "risk_label",
        "peg_score", "liquidity_score", "reserve_score", "adoption_score",
    ]].copy()
    display.columns = ["Symbol", "Overall", "Risk Level", "Peg", "Liquidity", "Reserve", "Adoption"]

    def _color_risk(val: str) -> str:
        c = RISK_COLORS.get(val, "")
        return f"color:{c}; font-weight:700;" if c else ""

    st.dataframe(
        display.style.map(_color_risk, subset=["Risk Level"]),
        use_container_width=True,
        hide_index=True,
    )


def _fmt_cadence(secs: int | None) -> str:
    if not secs:
        return "—"
    if secs % 3600 == 0:
        hours = secs // 3600
        return f"{hours}h" if hours > 1 else "1h"
    return f"{secs // 60} min"


def _age_from_iso(iso_ts: str | None) -> str:
    """Plain-language age from an ISO timestamp string (for provider rows)."""
    if not iso_ts:
        return "—"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "—"
    return _fmt_age((datetime.utcnow() - ts).total_seconds())


def render_data_freshness(freshness: dict) -> None:
    _section_header(
        "Data Freshness",
        "How current each data source is, measured against how often it is expected to refresh. "
        "Green is fresh, amber means one missed refresh, red means stale or unavailable.",
    )

    sources = freshness.get("sources", [])
    if not sources:
        _callout("No data sources are reporting yet. Run the ingestion pipelines first.", "info")
        return

    # Warn up front when anything is stale or has never reported.
    bad = [s for s in sources if s["status"] in ("stale", "missing")]
    if bad:
        names = ", ".join(s["label"] for s in bad)
        verb = "is" if len(bad) == 1 else "are"
        _callout(
            f"<strong>Heads up:</strong> {names} {verb} stale or unavailable — "
            "figures drawn from these sources may be out of date.",
            "warning",
        )

    # Per-source status pills.
    chips = []
    for s in sources:
        status = s["status"]
        color = STATUS_COLORS.get(status, C_MUTED)
        status_txt = STATUS_LABELS.get(status, status)
        age_txt = "" if status == "missing" else f" · {_fmt_age(s.get('age_seconds'))}"
        chips.append(
            f"<span style='display:inline-flex; align-items:center; gap:7px; "
            f"padding:6px 12px; margin:0 8px 8px 0; border-radius:999px; "
            f"border:1px solid rgba(128,128,128,0.18); font-size:12px;'>"
            f"<span style='width:8px;height:8px;border-radius:50%;background:{color};'></span>"
            f"<span style='color:{C_MUTED};'>{s['label']}</span>"
            f"<span style='font-weight:700;color:{color};'>{status_txt}{age_txt}</span>"
            f"</span>"
        )
    st.markdown("<div style='margin-bottom:14px;'>" + "".join(chips) + "</div>", unsafe_allow_html=True)

    source_table = pd.DataFrame([
        {
            "Source":       s["label"],
            "Provider":     s["provider"],
            "Tracks":       s["metric"],
            "Status":       STATUS_LABELS.get(s["status"], s["status"]),
            "Last Updated": _fmt_age(s.get("age_seconds")) if s["status"] != "missing" else "Never",
            "Expected Every": _fmt_cadence(s.get("expected_cadence_seconds")),
            "Assets":       s.get("assets_covered", 0),
        }
        for s in sources
    ])

    def _color_status(val: str) -> str:
        key = {v: k for k, v in STATUS_LABELS.items()}.get(val)
        c = STATUS_COLORS.get(key, "")
        return f"color:{c}; font-weight:700;" if c else ""

    st.dataframe(
        source_table.style.map(_color_status, subset=["Status"]),
        use_container_width=True,
        hide_index=True,
    )

    # Provider request health.
    providers = freshness.get("providers", [])
    if providers:
        st.markdown("<div style='margin-top:18px;'></div>", unsafe_allow_html=True)
        st.markdown("##### Provider request health")
        failing = [p for p in providers if p["status"] == "failing"]
        if failing:
            names = ", ".join(p["provider"] for p in failing)
            _callout(
                f"<strong>{names}</strong> last returned an error. The dashboard falls back to "
                "cached or alternate data, but live values from these providers may be missing.",
                "danger",
            )
        prov_table = pd.DataFrame([
            {
                "Provider":     p["provider"],
                "Status":       PROVIDER_STATUS_LABELS.get(p["status"], p["status"]),
                "Last Request": _age_from_iso(p.get("last_request_at")),
                "Last Status":  p.get("last_status_code") if p.get("last_status_code") is not None else "—",
                "Total Calls":  p.get("total_requests", 0),
                "Errors":       p.get("error_count", 0),
            }
            for p in providers
        ])

        def _color_prov(val: str) -> str:
            key = {v: k for k, v in PROVIDER_STATUS_LABELS.items()}.get(val)
            c = PROVIDER_STATUS_COLORS.get(key, "")
            return f"color:{c}; font-weight:700;" if c else ""

        st.dataframe(
            prov_table.style.map(_color_prov, subset=["Status"]),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()


def _fmt_event_time(ts) -> str:
    """Format a risk-event timestamp; accepts a datetime or an ISO string."""
    if ts is None:
        return "—"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return ts
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def render_risk_events_tab(events: list[dict]) -> None:
    _section_header(
        "Risk Events",
        "A chronological log of notable risk changes — peg breaks, liquidity "
        "drops, supply shocks, sharp score moves, stale reserves, and provider "
        "failures — detected automatically as the pipelines run.",
    )

    if not events:
        _callout(
            "No risk events recorded yet. Events are logged automatically once the "
            "pipelines have run on at least two snapshots and a metric moves past a "
            "stress threshold.",
            "info",
        )
        return

    # Client-side filters over already-loaded data — no extra queries or API cost.
    assets = sorted({e["symbol"] for e in events})
    types_present = [t for t in EVENT_TYPE_LABELS if any(e["event_type"] == t for e in events)]

    c1, c2, c3 = st.columns([1, 2, 2])
    with c1:
        asset = st.selectbox("Asset", ["All"] + assets, key="re_asset")
    with c2:
        type_sel = st.multiselect(
            "Event type", options=types_present, default=types_present,
            format_func=lambda t: EVENT_TYPE_LABELS.get(t, t), key="re_types",
        )
    with c3:
        sev_sel = st.multiselect(
            "Severity", options=["high", "medium", "low"], default=["high", "medium", "low"],
            format_func=str.capitalize, key="re_sev",
        )

    filtered = [
        e for e in events
        if (asset == "All" or e["symbol"] == asset)
        and (not type_sel or e["event_type"] in type_sel)
        and (not sev_sel or e["severity"] in sev_sel)
    ]

    if not filtered:
        _callout("No events match the current filters.", "info")
        return

    shown = filtered[:60]
    note = (
        f"Showing the {len(shown)} most recent of {len(filtered)} matching events."
        if len(filtered) > len(shown)
        else f"Showing {len(filtered)} of {len(events)} events."
    )
    st.markdown(
        f"<p style='font-size:12px; color:{C_MUTED}; margin-bottom:14px;'>{note}</p>",
        unsafe_allow_html=True,
    )

    for e in shown:
        color = SEVERITY_COLORS.get(e["severity"], C_BLUE)
        type_label = EVENT_TYPE_LABELS.get(e["event_type"], e["event_type"])
        desc = e.get("description") or ""
        st.markdown(
            f"<div style='border-left:3px solid {color}; background:rgba(128,128,128,0.07); "
            f"padding:10px 16px; border-radius:0 6px 6px 0; margin-bottom:10px;'>"
            f"<div style='display:flex; justify-content:space-between; align-items:center; "
            f"margin-bottom:4px;'>"
            f"<span>"
            f"<span style='color:{color}; font-weight:800; text-transform:uppercase; "
            f"font-size:10px; letter-spacing:0.08em; margin-right:10px;'>{e['severity']}</span>"
            f"<span style='font-size:10px; font-weight:700; text-transform:uppercase; "
            f"letter-spacing:0.06em; color:{C_MUTED}; border:1px solid rgba(128,128,128,0.22); "
            f"padding:2px 8px; border-radius:999px; margin-right:8px;'>{type_label}</span>"
            f"<span style='font-size:12px; font-weight:700;'>{e['symbol']}</span>"
            f"</span>"
            f"<span style='font-size:11px; color:{C_MUTED}; font-family:monospace;'>"
            f"{_fmt_event_time(e['triggered_at'])}</span>"
            f"</div>"
            f"<div style='font-size:14px; font-weight:700; margin-bottom:2px;'>{e['title']}</div>"
            f"<div style='font-size:13px; color:{C_MUTED}; line-height:1.5;'>{desc}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )


PIPELINE_LABELS = {
    "update_supply":     "Supply",
    "update_prices":     "Prices",
    "update_liquidity":  "Liquidity",
    "update_reserves":   "Reserves",
    "score_stablecoins": "Risk Scores",
}

# Pipeline run status → colour / label (distinct from the freshness statuses).
RUN_STATUS_COLORS = {"success": C_GREEN, "error": C_RED}
RUN_STATUS_LABELS = {"success": "Success", "error": "Failed"}


def _fmt_duration(secs: float | None) -> str:
    if secs is None:
        return "—"
    if secs < 1:
        return f"{secs * 1000:.0f} ms"
    if secs < 60:
        return f"{secs:.1f}s"
    return f"{int(secs // 60)}m {int(secs % 60)}s"


def render_pipeline_runs(data: dict) -> None:
    _section_header(
        "Pipeline Runs",
        "Every data job logs each run here — so you can confirm the pipelines are "
        "working and see when each last succeeded. Failed runs are flagged.",
    )

    summary = data.get("summary", [])
    runs = data.get("runs", [])

    if not summary:
        _callout(
            "No pipeline runs recorded yet. Runs are logged automatically each time "
            "a pipeline executes (on auto-refresh or manual refresh).",
            "info",
        )
        return

    # Callout for any pipeline whose most recent run failed.
    failed = [s for s in summary if s.get("last_status") == "error"]
    if failed:
        names = ", ".join(PIPELINE_LABELS.get(s["pipeline_name"], s["pipeline_name"]) for s in failed)
        verb = "pipeline's last run" if len(failed) == 1 else "pipelines' last runs"
        _callout(
            f"<strong>{names}</strong> — {verb} failed. The data behind these jobs may be "
            "stale until the next successful run. See the error in the table below.",
            "danger",
        )

    # Per-pipeline status pills.
    chips = []
    for s in summary:
        status = s.get("last_status") or "missing"
        color = RUN_STATUS_COLORS.get(status, C_MUTED)
        status_txt = RUN_STATUS_LABELS.get(status, "No runs")
        age_txt = f" · {_age_from_iso(s.get('last_run_at'))}" if s.get("last_run_at") else ""
        label = PIPELINE_LABELS.get(s["pipeline_name"], s["pipeline_name"])
        chips.append(
            f"<span style='display:inline-flex; align-items:center; gap:7px; "
            f"padding:6px 12px; margin:0 8px 8px 0; border-radius:999px; "
            f"border:1px solid rgba(128,128,128,0.18); font-size:12px;'>"
            f"<span style='width:8px;height:8px;border-radius:50%;background:{color};'></span>"
            f"<span style='color:{C_MUTED};'>{label}</span>"
            f"<span style='font-weight:700;color:{color};'>{status_txt}{age_txt}</span>"
            f"</span>"
        )
    st.markdown("<div style='margin-bottom:14px;'>" + "".join(chips) + "</div>", unsafe_allow_html=True)

    summary_table = pd.DataFrame([
        {
            "Pipeline":     PIPELINE_LABELS.get(s["pipeline_name"], s["pipeline_name"]),
            "Last Status":  RUN_STATUS_LABELS.get(s.get("last_status"), "No runs"),
            "Last Run":     _age_from_iso(s.get("last_run_at")),
            "Last Success": _age_from_iso(s.get("last_success_at")) if s.get("last_success_at") else "Never",
            "Rows":         s.get("last_rows_written") if s.get("last_rows_written") is not None else "—",
            "Duration":     _fmt_duration(s.get("last_duration_seconds")),
            "Failures (24h)": s.get("recent_failures", 0),
        }
        for s in summary
    ])

    def _color_run_status(val: str) -> str:
        key = {v: k for k, v in RUN_STATUS_LABELS.items()}.get(val)
        c = RUN_STATUS_COLORS.get(key, "")
        return f"color:{c}; font-weight:700;" if c else ""

    st.dataframe(
        summary_table.style.map(_color_run_status, subset=["Last Status"]),
        use_container_width=True,
        hide_index=True,
    )

    if runs:
        with st.expander(f"Recent runs ({len(runs)})"):
            runs_table = pd.DataFrame([
                {
                    "Pipeline": PIPELINE_LABELS.get(r["pipeline_name"], r["pipeline_name"]),
                    "Status":   RUN_STATUS_LABELS.get(r.get("status"), r.get("status")),
                    "Started":  _fmt_event_time(r.get("started_at")),
                    "Duration": _fmt_duration(r.get("duration_seconds")),
                    "Rows":     r.get("rows_written") if r.get("rows_written") is not None else "—",
                    "Error":    r.get("error_message") or "",
                }
                for r in runs
            ])
            st.dataframe(
                runs_table.style.map(_color_run_status, subset=["Status"]),
                use_container_width=True,
                hide_index=True,
            )

    st.divider()


def render_data_quality(data: dict) -> None:
    _section_header(
        "Data Quality",
        "Automated integrity checks on the stored data — impossible prices, "
        "non-positive supply, peg figures that disagree with price, implausible "
        "supply jumps, ticker-collision duplicates, and missing chain breakdowns. "
        "Active warnings clear automatically once the underlying data is fixed.",
    )

    summary = data.get("summary", {})
    warnings = data.get("warnings", [])
    total = summary.get("active_total", 0)

    if not warnings:
        _callout(
            "No active data-quality warnings — all stored metrics pass the "
            "validation rules. Checks run automatically each time the scoring "
            "pipeline executes.",
            "info",
        )
        st.divider()
        return

    by_sev = summary.get("by_severity", {})
    high, medium, low = by_sev.get("high", 0), by_sev.get("medium", 0), by_sev.get("low", 0)
    kind = "danger" if high else "warning"
    parts = [f"{n} {label}" for n, label in
             ((high, "high"), (medium, "medium"), (low, "low")) if n]
    breakdown = ", ".join(parts)
    noun = "warning" if total == 1 else "warnings"
    _callout(
        f"<strong>{total} active data-quality {noun}</strong>"
        + (f" ({breakdown}). " if breakdown else ". ")
        + "Figures derived from the affected metrics may be unreliable until resolved.",
        kind,
    )

    # Severity pills.
    chips = []
    for sev, n in (("high", high), ("medium", medium), ("low", low)):
        color = SEVERITY_COLORS.get(sev, C_MUTED)
        chips.append(
            f"<span style='display:inline-flex; align-items:center; gap:7px; "
            f"padding:6px 12px; margin:0 8px 8px 0; border-radius:999px; "
            f"border:1px solid rgba(128,128,128,0.18); font-size:12px;'>"
            f"<span style='width:8px;height:8px;border-radius:50%;background:{color};'></span>"
            f"<span style='color:{C_MUTED};'>{sev.capitalize()}</span>"
            f"<span style='font-weight:700;color:{color};'>{n}</span>"
            f"</span>"
        )
    st.markdown("<div style='margin-bottom:14px;'>" + "".join(chips) + "</div>", unsafe_allow_html=True)

    sev_rank = {"high": 0, "medium": 1, "low": 2}
    ordered = sorted(warnings, key=lambda w: sev_rank.get(w.get("severity"), 9))
    table = pd.DataFrame([
        {
            "Severity": w.get("severity", "").capitalize(),
            "Type":     WARNING_TYPE_LABELS.get(w.get("warning_type"), w.get("warning_type")),
            "Asset":    w.get("symbol") or "—",
            "Metric":   w.get("metric_name") or "—",
            "Detected": _fmt_event_time(w.get("detected_at")),
            "Detail":   w.get("message") or "",
        }
        for w in ordered
    ])

    def _color_sev(val: str) -> str:
        c = SEVERITY_COLORS.get(val.lower(), "")
        return f"color:{c}; font-weight:700;" if c else ""

    st.dataframe(
        table.style.map(_color_sev, subset=["Severity"]),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()


def render_api_tab() -> None:
    render_data_freshness(load_data_freshness())

    render_pipeline_runs(load_pipeline_runs())

    render_data_quality(load_data_quality())

    _section_header(
        "API Usage",
        "All data is sourced from free public APIs. No paid tier is used.",
    )
    st.dataframe(PROVIDER_COSTS, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("##### Live call log")
    usage_df = load_api_usage()
    if usage_df.empty:
        _callout("No API calls logged yet.", "info")
    else:
        st.dataframe(usage_df, use_container_width=True, hide_index=True)

        fig = go.Figure()
        for provider in usage_df["provider"].unique():
            sub = usage_df[usage_df["provider"] == provider]
            fig.add_trace(go.Bar(
                name=provider,
                x=sub["endpoint"],
                y=sub["calls"],
                hovertemplate="<b>%{x}</b><br>Calls: %{y}<extra></extra>",
            ))
        fig.update_layout(**_chart_layout(
            title="Total API Calls by Endpoint",
            height=300,
            barmode="group",
            yaxis=dict(title="Calls", gridcolor="rgba(128,128,128,0.12)", zeroline=False),
        ))
        st.plotly_chart(fig, use_container_width=True)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    init_db()
    _inject_styles()

    # ── auto-refresh (always on) ───────────────────────────────────────────────
    st_autorefresh(interval=PRICE_REFRESH_SECS * 1000, key="data_autorefresh")
    if _run_scheduled_pipelines():
        st.cache_data.clear()

    overview_df = load_overview()
    scores_df   = load_latest_scores()

    render_header(overview_df)
    render_market_changes(load_market_changes())

    tab_overview, tab_profile, tab_supply, tab_peg, tab_risk, tab_events, tab_api = st.tabs([
        "Overview",
        "Asset Profile",
        "Supply",
        "Peg Deviation",
        "Risk Scores",
        "Risk Events",
        "API Usage",
    ])

    with tab_overview:
        render_overview_tab(overview_df)
    with tab_profile:
        render_profile_tab(overview_df)
    with tab_supply:
        render_supply_tab(scores_df)
    with tab_peg:
        render_peg_tab(scores_df)
    with tab_risk:
        render_risk_tab(scores_df)
    with tab_events:
        render_risk_events_tab(load_risk_events())
    with tab_api:
        render_api_tab()

    st.sidebar.markdown("---")
    st.sidebar.caption(f"Data as of {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")

    # ── auto-refresh status ────────────────────────────────────────────────────
    st.sidebar.markdown("---")
    st.sidebar.subheader("Auto-Refresh")
    _price_rem  = max(0, int(PRICE_REFRESH_SECS  - (time.time() - _PIPELINE_LAST_RUN["prices"])))
    _supply_rem = max(0, int(SUPPLY_REFRESH_SECS - (time.time() - _PIPELINE_LAST_RUN["supply"])))
    with st.sidebar:
        components.html(
                f"""
                <style>
                    .cd-row {{
                        font-size: 12px;
                        color: rgba(160,160,160,0.85);
                        font-family: monospace;
                        padding: 2px 0;
                    }}
                    .cd-label {{ opacity: 0.7; }}
                </style>
                <div class="cd-row">
                    <span class="cd-label">Prices &amp; scores: </span>
                    <span id="cd-price">—</span>
                </div>
                <div class="cd-row">
                    <span class="cd-label">Supply &amp; reserves: </span>
                    <span id="cd-supply">—</span>
                </div>
                <script>
                    function makeTimer(id, startSecs) {{
                        var rem = startSecs;
                        (function tick() {{
                            var el = document.getElementById(id);
                            if (!el) return;
                            var m = Math.floor(rem / 60);
                            var s = rem % 60;
                            el.innerText = m + ':' + (s < 10 ? '0' : '') + s;
                            if (rem > 0) {{ rem--; setTimeout(tick, 1000); }}
                            else {{ el.innerText = 'refreshing…'; }}
                        }})();
                    }}
                    makeTimer('cd-price',  {_price_rem});
                    makeTimer('cd-supply', {_supply_rem});
                </script>
                """,
                height=52,
            )

    # ── manual refresh ─────────────────────────────────────────────────────────
    st.sidebar.markdown("---")
    st.sidebar.subheader("Manual Refresh")
    pwd = st.sidebar.text_input("Password", type="password", key="refresh_pwd")
    if st.sidebar.button("Refresh Data", use_container_width=True):
        if pwd == "2026":
            import core.cache as _api_cache
            import pipelines.update_supply   as _supply
            import pipelines.update_prices   as _prices
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

    st.sidebar.markdown(
        "<p style='font-size:11px; color:rgba(100,100,100,0.8); margin-top:32px;'>"
        "Data sourced from DefiLlama, Binance, and Coinbase public APIs. "
        "Not financial advice.</p>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
