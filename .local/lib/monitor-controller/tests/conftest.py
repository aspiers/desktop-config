"""Keep every test away from the live desktop's controller state."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Workers read displays switched off by hand from here (dc-8xns).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
