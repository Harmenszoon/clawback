"""Tests for data-home resolution: where tools.json and logs/ live."""

from __future__ import annotations

from clawback.config import _resolve_data_home


def test_clawback_home_env_wins_and_is_created(tmp_path, monkeypatch):
    target = tmp_path / "custom-home"
    monkeypatch.setenv("CLAWBACK_HOME", str(target))
    assert _resolve_data_home() == target
    assert target.is_dir()  # created on resolution


def test_checkout_layout_uses_repo_root(monkeypatch):
    """Running from a git checkout (as the tests do) must keep the historical
    layout: tools.json and logs/ next to pyproject.toml, not in ~/.clawback."""
    monkeypatch.delenv("CLAWBACK_HOME", raising=False)
    root = _resolve_data_home()
    assert (root / "pyproject.toml").exists()
