"""Shared pytest fixtures for ccpermissions tests.

Two responsibilities:

1. ``_isolate_env`` (autouse) pins ``HOME``, ``CLAUDE_CONFIG_DIR`` and
   ``CLAUDE_PROJECT_DIR`` into the per-test ``tmp_path`` so the suite never
   reads the developer's real config.
2. ``write_user_config`` / ``write_project_config`` fixture-factories build
   the YAML files tests want to operate on.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path) -> None:
    """Pin config-related env vars to a clean per-test sandbox."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()


@pytest.fixture
def write_user_config(tmp_path, monkeypatch) -> Callable[[str], Path]:
    """Return a factory that writes a user-scoped ``ccpermissions.yaml``.

    The returned callable accepts the YAML content (auto-dedented) and points
    ``CLAUDE_CONFIG_DIR`` at the new directory.

    Returns:
        A function ``(content: str) -> Path`` that writes the YAML and
        returns the directory containing it.
    """

    def _write(content: str) -> Path:
        d: Path = tmp_path / "user-config"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ccpermissions.yaml").write_text(textwrap.dedent(content))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
        return d

    return _write


@pytest.fixture
def write_project_config(tmp_path, monkeypatch) -> Callable[[str], Path]:
    """Return a factory that writes a project-scoped ``ccpermissions.yaml``.

    Returns:
        A function ``(content: str) -> Path`` that writes the YAML and
        returns the project root containing it.
    """

    def _write(content: str) -> Path:
        proj: Path = tmp_path / "project"
        (proj / ".claude").mkdir(parents=True, exist_ok=True)
        (proj / ".claude" / "ccpermissions.yaml").write_text(textwrap.dedent(content))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
        return proj

    return _write


