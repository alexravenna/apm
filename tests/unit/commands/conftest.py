"""Shared fixtures for ``tests/unit/commands/``.

The ``marketplace_authoring`` experimental flag was removed when marketplace
authoring went GA -- this conftest no longer patches the flag.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def run_git_cmd():
    """Run a git subcommand with hermetic identity/config for tests."""

    return _run_git
