"""Suite-wide hermeticity.

`Settings` reads `.env`, which is exactly right for an operator and exactly
wrong for a test: whoever has a real key in theirs would see a different suite
than whoever does not. That is not hypothetical — a `.env` with
`ORCHESTRATOR_WORKER=live` turns a passing suite red, on a machine where nothing
is broken.

So the suite reads no `.env` and carries no credential. Anything a test needs
about the environment, it sets itself.
"""

from __future__ import annotations

import pytest

from orchestrator.config import Settings, reset_settings


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reset_settings()
    yield
    reset_settings()
