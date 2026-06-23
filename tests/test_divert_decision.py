"""Unit tests for the pure real-time divert decision (``divert.decide_divert``).

Like the engine tests, the module is loaded directly from its file via importlib
so it runs with nothing but stdlib + pytest (importing the package would pull in
Home Assistant).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_DIVERT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "load_scheduler"
    / "divert.py"
)
_spec = importlib.util.spec_from_file_location("ls_divert", _DIVERT_PATH)
divert = importlib.util.module_from_spec(_spec)
sys.modules["ls_divert"] = divert
_spec.loader.exec_module(divert)

DivertCandidate = divert.DivertCandidate
decide_divert = divert.decide_divert

# Defaults mirroring the actuator's wiring.
_BUFFER = 0.1
_SHED = 0.0


def _decide(**kw):
    base = dict(
        predicted_net=0.0,
        diverted=[],
        candidates=[],
        engage_buffer=_BUFFER,
        shed_margin=_SHED,
        sell_ok=True,
        can_engage=True,
        can_shed=True,
    )
    base.update(kw)
    return decide_divert(**base)


def test_engages_highest_priority_when_it_fits() -> None:
    # Projected to export 0.5 kWh; a load drawing 0.2 kWh keeps it in export.
    decision = _decide(
        predicted_net=-0.5,
        candidates=[
            DivertCandidate("low", priority=1, projected_energy=0.2),
            DivertCandidate("high", priority=5, projected_energy=0.2),
        ],
    )
    assert decision.add == "high"
    assert decision.remove is None


def test_refuses_candidate_that_would_tip_interval_to_import() -> None:
    # Projecting only -0.3 export, but the load would draw 0.5 kWh: engaging it
    # tips the interval to +0.2 net import, so it must be refused.
    decision = _decide(
        predicted_net=-0.3,
        candidates=[DivertCandidate("big", priority=5, projected_energy=0.5)],
    )
    assert decision.add is None
    assert decision.remove is None


def test_engage_needs_buffer_headroom() -> None:
    # Adding the load lands exactly at break-even (0.0); the buffer requires it to
    # stay <= -0.1, so it is refused.
    decision = _decide(
        predicted_net=-0.4,
        candidates=[DivertCandidate("x", priority=1, projected_energy=0.4)],
    )
    assert decision.add is None


def test_sheds_lowest_priority_on_projected_import() -> None:
    decision = _decide(
        predicted_net=0.2,  # projected to close in net import
        diverted=[("water", 5), ("floor", 1)],
    )
    assert decision.remove == "floor"
    assert decision.add is None


def test_sheds_when_selling_beats_self_consumption() -> None:
    # Even though strongly exporting, a high sell price (sell_ok False) means we
    # should be exporting, not self-consuming: shed.
    decision = _decide(
        predicted_net=-0.9,
        diverted=[("water", 5), ("floor", 1)],
        candidates=[DivertCandidate("ev", priority=9, projected_energy=0.1)],
        sell_ok=False,
    )
    assert decision.remove == "floor"
    assert decision.add is None


def test_holds_inside_hysteresis_band() -> None:
    # Between -engage_buffer (-0.1) and +shed_margin (0.0): neither engage nor shed.
    decision = _decide(
        predicted_net=-0.05,
        diverted=[("water", 5)],
        candidates=[DivertCandidate("floor", priority=1, projected_energy=0.0)],
    )
    assert decision == divert.DivertDecision()


def test_dwell_blocks_engage_but_allows_shed() -> None:
    # Asymmetric dwell: shed is permitted while engage is still held off.
    shed = _decide(
        predicted_net=0.2,
        diverted=[("floor", 1)],
        can_engage=False,
        can_shed=True,
    )
    assert shed.remove == "floor"

    held = _decide(
        predicted_net=-0.5,
        candidates=[DivertCandidate("x", priority=1, projected_energy=0.1)],
        can_engage=False,
        can_shed=True,
    )
    assert held.add is None


def test_no_action_when_nothing_eligible() -> None:
    assert _decide(predicted_net=-0.9) == divert.DivertDecision()
    assert _decide(predicted_net=0.5) == divert.DivertDecision()
