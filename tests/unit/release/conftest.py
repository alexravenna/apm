"""Shared fixtures for release-engineering tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


def _git_available() -> bool:
    return shutil.which("git") is not None


requires_git = pytest.mark.skipif(
    not _git_available(),
    reason="git executable not on PATH",
)


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        # Disable any user-level gpg signing config that would prompt.
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
def git_repo_factory(tmp_path: Path) -> Callable[..., Path]:
    """Factory creating an initialised git repo for tagger tests.

    Returns a callable. Each call yields a fresh directory under tmp_path.

    Kwargs:
        subdir: subdirectory name (default: 'repo')
        dirty: if True, leave an unstaged file behind after the initial commit
        existing_tags: list of tag names to pre-create on HEAD
        with_remote: if True, attach a local bare repo as 'origin'
        remote_tags: list of tag names to pre-create on the remote
    """
    counter = {"n": 0}

    def _factory(
        *,
        subdir: str | None = None,
        dirty: bool = False,
        existing_tags: list[str] | None = None,
        with_remote: bool = False,
        remote_tags: list[str] | None = None,
    ) -> Path:
        counter["n"] += 1
        name = subdir or f"repo{counter['n']}"
        repo = tmp_path / name
        repo.mkdir(parents=True, exist_ok=True)
        assert _run(["init", "-q", "-b", "main"], repo).returncode == 0
        # Local repo identity (avoid relying on global config).
        _run(["config", "user.name", "Test"], repo)
        _run(["config", "user.email", "test@example.com"], repo)
        _run(["config", "commit.gpgSign", "false"], repo)
        _run(["config", "tag.gpgSign", "false"], repo)
        (repo / "README.md").write_text("init\n", encoding="utf-8")
        _run(["add", "README.md"], repo)
        assert _run(["commit", "-q", "-m", "init"], repo).returncode == 0

        if with_remote:
            bare = tmp_path / f"{name}-remote.git"
            assert _run(["init", "-q", "--bare", str(bare)], tmp_path).returncode == 0
            _run(["remote", "add", "origin", str(bare)], repo)
            # Push main so origin has a usable ref.
            _run(["push", "-q", "origin", "main"], repo)
            if remote_tags:
                for t in remote_tags:
                    _run(["tag", t], repo)
                    _run(["push", "-q", "origin", f"refs/tags/{t}:refs/tags/{t}"], repo)
                    _run(["tag", "-d", t], repo)

        if existing_tags:
            for t in existing_tags:
                _run(["tag", "-a", "-m", f"pre {t}", t], repo)

        if dirty:
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        return repo

    return _factory


@pytest.fixture
def run_git_cmd():
    """Wrapper to invoke git from tests without leaking env."""

    def _run_cmd(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return _run(args, cwd)

    return _run_cmd
