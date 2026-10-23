"""Tests for ETA estimation."""

from __future__ import annotations

import pytest

from progress_and_claude.core.eta import estimate_eta


def test_needs_three_samples() -> None:
    assert estimate_eta([(0, 0.0), (5, 0.5)], now=5) is None


def test_steady_rate() -> None:
    samples = [(0.0, 0.0), (10.0, 0.25), (20.0, 0.5)]
    assert estimate_eta(samples, now=20.0) == pytest.approx(20.0)


def test_stalled_returns_none() -> None:
    samples = [(0.0, 0.0), (10.0, 0.25), (20.0, 0.5)]
    assert estimate_eta(samples, now=500.0) is None


def test_complete_or_flat_returns_none() -> None:
    assert estimate_eta([(0, 0.0), (5, 0.5), (10, 1.0)], now=10) is None
    assert estimate_eta([(0, 0.3), (5, 0.3), (10, 0.3)], now=10) is None
