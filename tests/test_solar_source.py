"""Unit tests for the solar-forecast parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.load_scheduler import solar_source as ss
from custom_components.load_scheduler.engine import Slot


def _solcast(powers_kw: list[float], t0: datetime) -> dict:
    return {
        "detailedForecast": [
            {"period_start": (t0 + timedelta(minutes=30 * i)).isoformat(), "pv_estimate": p}
            for i, p in enumerate(powers_kw)
        ]
    }


def test_parse_solcast_treats_pv_estimate_as_kw_and_normalises_utc():
    t0 = datetime(2026, 6, 17, 9, 0, tzinfo=UTC)
    periods = ss.parse_solar(_solcast([4.0, 2.0], t0))
    assert len(periods) == 2
    assert periods[0].power_kw == 4.0
    assert periods[0].end - periods[0].start == timedelta(minutes=30)
    assert periods[0].start.utcoffset() == timedelta(0)


def test_available_kwh_integrates_power_over_slots():
    t0 = datetime(2026, 6, 17, 9, 0, tzinfo=UTC)
    periods = ss.parse_solar(_solcast([4.0], t0))  # one 30-min period at 4 kW
    slots = [
        Slot(t0, t0 + timedelta(minutes=15), buy=0.1),
        Slot(t0 + timedelta(minutes=15), t0 + timedelta(minutes=30), buy=0.1),
    ]
    kwh = ss.available_kwh_by_slot(periods, slots)
    # 4 kW for 15 min = 1 kWh in each of the two slots beneath the period.
    assert kwh[slots[0].start] == pytest.approx(1.0)
    assert kwh[slots[1].start] == pytest.approx(1.0)


def test_merge_dedups_overlapping_starts():
    t0 = datetime(2026, 6, 17, 9, 0, tzinfo=UTC)
    today = ss.parse_solar(_solcast([4.0, 2.0], t0))
    other = ss.parse_solar(_solcast([9.0, 3.0], t0))  # same period starts
    merged = ss.merge_solar(today, other)
    assert len(merged) == 2
    assert merged[0].power_kw == 4.0  # first one kept


def test_generic_watts_converted_to_kw():
    t0 = datetime(2026, 6, 17, 9, 0, tzinfo=UTC)
    periods = ss.parse_solar(
        {
            "forecast": [
                {"start": t0.isoformat(), "watts": 2000},
                {"start": (t0 + timedelta(minutes=30)).isoformat(), "watts": 1000},
            ]
        }
    )
    assert periods[0].power_kw == 2.0


def test_error_on_unknown_attributes():
    with pytest.raises(ss.SolarFormatError):
        ss.parse_solar({"foo": "bar"})
