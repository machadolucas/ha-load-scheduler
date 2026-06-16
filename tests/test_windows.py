"""Unit tests for the DST-safe window resolver (pure, no Home Assistant)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

_W_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "load_scheduler"
    / "windows.py"
)
_spec = importlib.util.spec_from_file_location("ls_windows", _W_PATH)
windows = importlib.util.module_from_spec(_spec)
sys.modules["ls_windows"] = windows
_spec.loader.exec_module(windows)

resolve_window = windows.resolve_window

EET = timezone(timedelta(hours=2))  # fixed offset for the non-DST cases
HEL = ZoneInfo("Europe/Helsinki")


def test_daytime_window_not_yet_started():
    now = datetime(2026, 1, 15, 10, 0, tzinfo=EET)
    start, end = resolve_window(now, time(12, 0), time(18, 0))
    assert start == datetime(2026, 1, 15, 12, 0, tzinfo=EET)
    assert end == datetime(2026, 1, 15, 18, 0, tzinfo=EET)


def test_daytime_window_already_passed_rolls_to_tomorrow():
    now = datetime(2026, 1, 15, 20, 0, tzinfo=EET)
    start, end = resolve_window(now, time(12, 0), time(18, 0))
    assert start == datetime(2026, 1, 16, 12, 0, tzinfo=EET)
    assert end == datetime(2026, 1, 16, 18, 0, tzinfo=EET)


def test_overnight_window_from_evening():
    now = datetime(2026, 1, 15, 20, 0, tzinfo=EET)
    start, end = resolve_window(now, time(21, 0), time(7, 0))
    assert start == datetime(2026, 1, 15, 21, 0, tzinfo=EET)
    assert end == datetime(2026, 1, 16, 7, 0, tzinfo=EET)


def test_overnight_window_inside_clamps_start_to_now():
    now = datetime(2026, 1, 15, 2, 0, tzinfo=EET)
    start, end = resolve_window(now, time(21, 0), time(7, 0))
    assert start == now  # clamped: don't schedule the part already elapsed
    assert end == datetime(2026, 1, 15, 7, 0, tzinfo=EET)


def test_no_clamp_keeps_window_open_in_past():
    now = datetime(2026, 1, 15, 2, 0, tzinfo=EET)
    start, end = resolve_window(now, time(21, 0), time(7, 0), clamp_to_now=False)
    assert start == datetime(2026, 1, 14, 21, 0, tzinfo=EET)
    assert end == datetime(2026, 1, 15, 7, 0, tzinfo=EET)


def test_earliest_none_starts_now():
    now = datetime(2026, 1, 15, 10, 0, tzinfo=EET)
    start, end = resolve_window(now, None, time(18, 0))
    assert start == now
    assert end == datetime(2026, 1, 15, 18, 0, tzinfo=EET)


def test_deadline_none_is_24h_horizon():
    now = datetime(2026, 1, 15, 10, 0, tzinfo=EET)
    start, end = resolve_window(now, None, None)
    assert start == now
    assert end == now + timedelta(days=1)


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        resolve_window(datetime(2026, 1, 15, 10, 0), time(12, 0), time(18, 0))


# --------------------------------------------------------------------------- #
# DST: the overnight window's real duration reflects the skipped/repeated hour.
# --------------------------------------------------------------------------- #


# NB: subtracting two datetimes that share the *same* ZoneInfo does wall-clock
# arithmetic (always 10h here), so to assert *real* elapsed time we convert both
# to UTC first. The property that matters is: the window is anchored to the
# correct local wall-clock instants (21:00 → 07:00), which a naive
# `start + timedelta(hours=10)` would get wrong across a transition.


def _real_elapsed(start, end):
    return end.astimezone(UTC) - start.astimezone(UTC)


def test_dst_spring_forward_overnight_anchors_and_is_nine_hours_real():
    # Night of 2026-03-28→29: clocks skip 03:00→04:00, so 21:00→07:00 is 9h real.
    now = datetime(2026, 3, 28, 20, 0, tzinfo=HEL)
    start, end = resolve_window(now, time(21, 0), time(7, 0), clamp_to_now=False)
    assert start == datetime(2026, 3, 28, 21, 0, tzinfo=HEL)
    assert end == datetime(2026, 3, 29, 7, 0, tzinfo=HEL)
    assert start.utcoffset() == timedelta(hours=2)  # EET, before the jump
    assert end.utcoffset() == timedelta(hours=3)  # EEST, after the jump
    assert _real_elapsed(start, end) == timedelta(hours=9)


def test_dst_fall_back_overnight_anchors_and_is_eleven_hours_real():
    # Night of 2026-10-24→25: clocks repeat 03:00→04:00, so 21:00→07:00 is 11h.
    now = datetime(2026, 10, 24, 20, 0, tzinfo=HEL)
    start, end = resolve_window(now, time(21, 0), time(7, 0), clamp_to_now=False)
    assert start == datetime(2026, 10, 24, 21, 0, tzinfo=HEL)
    assert end == datetime(2026, 10, 25, 7, 0, tzinfo=HEL)
    assert _real_elapsed(start, end) == timedelta(hours=11)
