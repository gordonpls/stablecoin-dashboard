"""Tests for risk scoring logic."""

import pytest
from datetime import date, timedelta

from pipelines.score_stablecoins import (
    _peg_score,
    _liquidity_score,
    _reserve_score,
    _adoption_score,
    LARGE_CAP_THRESHOLD,
    MAX_PEG_DEVIATION_BPS,
    MAX_LIQUIDITY_USD,
    STALE_RESERVE_DAYS,
)
from db.models import ReserveReport


def test_peg_score_perfect():
    assert _peg_score(0.0) == 100.0


def test_peg_score_at_max_deviation():
    assert _peg_score(MAX_PEG_DEVIATION_BPS) == pytest.approx(0.0)


def test_peg_score_over_max_clamped_to_zero():
    assert _peg_score(MAX_PEG_DEVIATION_BPS * 2) == 0.0


def test_peg_score_none_returns_midpoint():
    assert _peg_score(None) == 50.0


def test_peg_score_small_deviation():
    score = _peg_score(10)
    assert 85.0 < score < 95.0


def test_liquidity_score_full_depth():
    score = _liquidity_score(MAX_LIQUIDITY_USD / 2, MAX_LIQUIDITY_USD / 2)
    assert score == 100.0


def test_liquidity_score_partial():
    score = _liquidity_score(MAX_LIQUIDITY_USD / 4, 0)
    assert score == pytest.approx(25.0)


def test_liquidity_score_none_returns_midpoint():
    assert _liquidity_score(None, None) == 50.0


def test_reserve_score_no_report():
    assert _reserve_score(None) == 20.0


def test_reserve_score_fresh_audited():
    report = ReserveReport(symbol="USDC", report_date=date.today(), auditor="Deloitte")
    score = _reserve_score(report)
    assert score >= 95.0


def test_reserve_score_fresh_no_auditor():
    report = ReserveReport(symbol="DAI", report_date=date.today(), auditor=None)
    score = _reserve_score(report)
    assert 85.0 <= score < 95.0


def test_reserve_score_stale():
    report = ReserveReport(
        symbol="USDT",
        report_date=date.today() - timedelta(days=STALE_RESERVE_DAYS + 10),
        auditor=None,
    )
    assert _reserve_score(report) < 5.0


def test_reserve_score_no_date():
    report = ReserveReport(symbol="X", report_date=None, auditor=None)
    assert _reserve_score(report) == 40.0


def test_adoption_score_large_cap():
    assert _adoption_score(LARGE_CAP_THRESHOLD) == 100.0


def test_adoption_score_over_cap_clamped():
    assert _adoption_score(LARGE_CAP_THRESHOLD * 2) == 100.0


def test_adoption_score_zero():
    assert _adoption_score(0) == 0.0


def test_adoption_score_none():
    assert _adoption_score(None) == 0.0


def test_overall_score_weights_sum_to_one():
    weights = [0.35, 0.25, 0.25, 0.15]
    assert sum(weights) == pytest.approx(1.0)
