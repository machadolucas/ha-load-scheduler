"""Unit tests for the hour-of-day baseline profile (pure)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "load_scheduler"
    / "baseline.py"
)
_spec = importlib.util.spec_from_file_location("ls_baseline", _PATH)
baseline = importlib.util.module_from_spec(_spec)
sys.modules["ls_baseline"] = baseline
_spec.loader.exec_module(baseline)


def test_build_hourly_profile_averages_and_converts_to_kw():
    samples = [(0, 400.0), (0, 600.0), (12, 2000.0)]
    profile = baseline.build_hourly_profile(samples)
    assert profile[0] == 0.5  # (400+600)/2 = 500 W -> 0.5 kW
    assert profile[12] == 2.0  # 2000 W -> 2 kW
    assert 13 not in profile  # hours without samples are absent


def test_build_hourly_profile_empty():
    assert baseline.build_hourly_profile([]) == {}
