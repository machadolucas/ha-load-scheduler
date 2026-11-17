"""Fixtures for the Home-Assistant-backed tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the custom integration in every HA test."""
    yield


@pytest.fixture
def no_startup_grace(monkeypatch):
    """A dead price feed, not one still loading after a restart.

    A freshly started hub with no prices waits PRICE_DROPOUT_GRACE_S before
    treating the feed as dead (failsafe, repair, switching owned runs off).
    Tests of the dead-feed behaviour skip that wait.
    """
    from custom_components.load_scheduler.coordinator import LoadSchedulerCoordinator

    monkeypatch.setattr(LoadSchedulerCoordinator, "_awaiting_first_prices", lambda self, now: False)
