"""End-to-end test for ``apm pack --check-versions --create-tag --push``.

Spawns a real git repository plus a local bare remote and exercises the
full pack-tagging flow. No network access, no GPG, fully hermetic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git executable not on PATH",
)


def _run_git(args, cwd):
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=False
    )


def _scaffold(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "apm.yml").write_text(
        textwrap.dedent(
            """\
            name: e2e-project
            description: E2E project.
            version: 2.0.0
            marketplace:
              owner:
                name: ACME
              packages:
                - name: hello
                  source: ./packages/hello
                  description: Hello.
                  version: 2.0.0
            """
        ),
        encoding="utf-8",
    )
    pkg = repo / "packages" / "hello"
    pkg.mkdir(parents=True)
    pkg.joinpath("apm.yml").write_text(
        "name: hello\ndescription: Hello.\nversion: 2.0.0\n", encoding="utf-8"
    )
    _run_git(["init", "-q", "-b", "main"], repo).check_returncode()
    _run_git(["config", "user.name", "Test"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "commit.gpgSign", "false"], repo)
    _run_git(["config", "tag.gpgSign", "false"], repo)
    _run_git(["add", "-A"], repo).check_returncode()
    _run_git(["commit", "-q", "-m", "init"], repo).check_returncode()
    bare = tmp_path / "origin.git"
    _run_git(["init", "-q", "--bare", str(bare)], tmp_path).check_returncode()
    _run_git(["remote", "add", "origin", str(bare)], repo).check_returncode()
    _run_git(["push", "-q", "origin", "main"], repo).check_returncode()
    return repo, bare


def test_pack_check_versions_create_tag_push_end_to_end(tmp_path, monkeypatch):
    repo, bare = _scaffold(tmp_path)
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(
        pack_cmd,
        [
            "--marketplace=none",
            "--offline",
            "--check-versions",
            "--create-tag",
            "--push",
        ],
    )
    assert result.exit_code == 0, result.output
    local = _run_git(["tag", "--list"], repo).stdout
    assert "v2.0.0" in local
    remote = _run_git(["ls-remote", "--tags", str(bare)], repo).stdout
    assert "refs/tags/v2.0.0" in remote


def test_pack_dry_run_does_not_touch_repo_or_remote(tmp_path, monkeypatch):
    repo, bare = _scaffold(tmp_path)
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(
        pack_cmd,
        [
            "--marketplace=none",
            "--offline",
            "--check-versions",
            "--create-tag",
            "--push",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "v2.0.0" not in _run_git(["tag", "--list"], repo).stdout
    assert "refs/tags/v2.0.0" not in _run_git(["ls-remote", "--tags", str(bare)], repo).stdout
