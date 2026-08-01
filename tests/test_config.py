"""Configuration tests.

The line worth defending: the environment says who you are and where things
live; the plan says what the system does. A model id in `.env` would put a run's
cost profile outside the artifact that describes the run.
"""

from __future__ import annotations

import pytest

from orchestrator.config import MissingCredential, Settings, WorkerMode


def settings(**overrides) -> Settings:
    """Build settings without reading the developer's real .env."""
    return Settings(_env_file=None, **overrides)


def test_defaults_are_safe_without_any_environment():
    """A fresh clone runs the suite with no .env and no credential."""
    config = settings()
    assert config.worker is WorkerMode.STUB
    assert config.anthropic_api_key is None
    assert not config.is_live


def test_the_database_lives_inside_the_runs_directory(tmp_path):
    """Pointing runs_dir elsewhere should move the database with it."""
    config = settings(runs_dir=tmp_path / "elsewhere")
    assert config.database_url == f"sqlite:///{tmp_path / 'elsewhere'}/orchestrator.db"


def test_an_explicit_database_url_wins():
    config = settings(db_url="sqlite:///tmp/other.db")
    assert config.database_url == "sqlite:///tmp/other.db"


def test_a_live_run_without_a_credential_fails_early_and_legibly():
    """Better than a cryptic failure deep inside an SDK call."""
    config = settings(worker=WorkerMode.LIVE)
    with pytest.raises(MissingCredential) as excinfo:
        config.require_api_key()

    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "ORCHESTRATOR_WORKER=replay" in message  # names the way out


def test_stub_and_replay_runs_need_no_credential():
    """Which is why the whole test suite runs without one."""
    for mode in (WorkerMode.STUB, WorkerMode.REPLAY):
        assert not settings(worker=mode).is_live


def test_the_api_key_is_read_unprefixed(monkeypatch):
    """Every tool expects this exact name; a second spelling would be a trap."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert settings(worker=WorkerMode.LIVE).require_api_key() == "sk-ant-test"


def test_everything_else_is_prefixed(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_WORKER", "replay")
    monkeypatch.setenv("ORCHESTRATOR_MAX_WORKERS", "16")
    config = settings()
    assert config.worker is WorkerMode.REPLAY
    assert config.max_workers == 16


def test_an_unknown_variable_is_ignored(monkeypatch):
    """A typo in .env should not stop a run from starting."""
    monkeypatch.setenv("ORCHESTRATOR_NOT_A_SETTING", "x")
    assert settings().worker is WorkerMode.STUB


@pytest.mark.parametrize("field", ["model", "effort"])
def test_the_model_and_effort_are_not_environment_settings(field):
    """D16: behaviour is per-node data in the plan, not a flat env value.

    Putting them here would give per-node effort nowhere to live and move a
    run's cost profile out of the artifact that describes the run.
    """
    assert field not in Settings.model_fields


def test_the_example_file_documents_every_prefixed_setting():
    """`.env.example` is the only discovery surface a newcomer has."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    text = example.read_text()

    for name in Settings.model_fields:
        if name == "anthropic_api_key":
            assert "ANTHROPIC_API_KEY" in text
        else:
            assert f"ORCHESTRATOR_{name.upper()}" in text, f"{name} is undocumented"
