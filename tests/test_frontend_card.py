"""Run the frontend card's headless editor checks under pytest.

The Lovelace bundle is plain browser JS with no build step, so its checks live
in ``tests/frontend/card_editor_test.mjs`` and run under Node. Wrapping them
here keeps ``pytest`` the single command that runs everything; the test skips
where Node isn't installed rather than failing the Python-only suite.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

_TEST = pathlib.Path(__file__).parent / "frontend" / "card_editor_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_card_editors() -> None:
    """The card editors expose every option and round-trip the config."""
    result = subprocess.run(
        [shutil.which("node") or "node", str(_TEST)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
