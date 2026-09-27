"""Unit tests for the pure unit helpers (``units.power_to_watts``).

Loaded directly from its file via importlib, like the engine tests, so it runs
with nothing but stdlib + pytest.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "custom_components/load_scheduler/units.py"
_spec = importlib.util.spec_from_file_location("ls_units", _PATH)
units = importlib.util.module_from_spec(_spec)
sys.modules["ls_units"] = units
_spec.loader.exec_module(units)


@pytest.mark.parametrize(
    ("value", "unit", "watts"),
    [
        (1500.0, "W", 1500.0),
        (1.5, "kW", 1500.0),
        (0.002, "MW", 2000.0),
        (500.0, "mW", 0.5),  # milli, not mega: case matters
        (42.0, None, 42.0),  # no unit: assume watts
        (42.0, "BTU/h", 42.0),  # unknown unit: assume watts
    ],
)
def test_power_to_watts(value: float, unit: str | None, watts: float) -> None:
    assert units.power_to_watts(value, unit) == pytest.approx(watts)
