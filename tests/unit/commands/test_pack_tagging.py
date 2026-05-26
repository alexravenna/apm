"""Unit tests for ``apm pack --create-tag`` / ``--push`` wiring."""

from __future__ import annotations

import json as _json
import textwrap as _tw
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("git") is None,
    reason="git executable not on PATH",
)


_APM_YAML = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: Tool.
      version: 1.0.0
"""


def _scaffold_repo(tmp_path: Path, run_git_cmd, *, pkg_version: str = "1.0.0") -> Path:
    """Create a fresh git repo under tmp_path/project with one local package."""
    repo = tmp_path / "project"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "apm.yml").write_text(_tw.dedent(_APM_YAML), encoding="utf-8")
    pkg = repo / "packages" / "local-tool"
    pkg.mkdir(parents=True)
    pkg.joinpath("apm.yml").write_text(
        f"name: local-tool\ndescription: Tool.\nversion: {pkg_version}\n",
        encoding="utf-8",
    )
    assert run_git_cmd(["init", "-q", "-b", "main"], repo).returncode == 0
    run_git_cmd(["config", "user.name", "Test"], repo)
    run_git_cmd(["config", "user.email", "test@example.com"], repo)
    run_git_cmd(["config", "commit.gpgSign", "false"], repo)
    run_git_cmd(["config", "tag.gpgSign", "false"], repo)
    run_git_cmd(["add", "-A"], repo)
    assert run_git_cmd(["commit", "-q", "-m", "init"], repo).returncode == 0
    return repo


def _scaffold_per_package(tmp_path: Path, run_git_cmd) -> Path:
    """Create a repo using per_package versioning with two local packages."""
    repo = tmp_path / "project"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "apm.yml").write_text(
        _tw.dedent(
            """\
            name: my-project
            description: A project.
            version: 1.0.0
            marketplace:
              owner:
                name: ACME
              versioning:
                strategy: per_package
              packages:
                - name: alpha
                  source: ./packages/alpha
                  description: A.
                  version: 1.0.0
                - name: beta
                  source: ./packages/beta
                  description: B.
                  version: 2.0.0
            """
        ),
        encoding="utf-8",
    )
    for name, ver in (("alpha", "1.0.0"), ("beta", "2.0.0")):
        pkg = repo / "packages" / name
        pkg.mkdir(parents=True)
        pkg.joinpath("apm.yml").write_text(
            f"name: {name}\ndescription: x.\nversion: {ver}\n", encoding="utf-8"
        )
    assert run_git_cmd(["init", "-q", "-b", "main"], repo).returncode == 0
    run_git_cmd(["config", "user.name", "Test"], repo)
    run_git_cmd(["config", "user.email", "test@example.com"], repo)
    run_git_cmd(["config", "commit.gpgSign", "false"], repo)
    run_git_cmd(["config", "tag.gpgSign", "false"], repo)
    run_git_cmd(["add", "-A"], repo)
    assert run_git_cmd(["commit", "-q", "-m", "init"], repo).returncode == 0
    return repo


@pytest.fixture(autouse=True)
def _reset_console_state():
    """Reset the global console singleton so --json doesn't bleed across tests."""
    from apm_cli.utils.console import _reset_console

    yield
    _reset_console()


# Default flag chain: skip marketplace artifact writes so the orchestrator
# does not dirty the tree mid-flight. The release-gate config still loads.
# Real usage assumes the producer has already committed marketplace.json.
def _parse_json(output: str) -> dict:
    """Extract the JSON envelope from stdout, tolerating any leading log lines.

    set_console_stderr() should route logger output to stderr, but Rich's
    capture semantics under CliRunner can leak; tests assert against the
    actual JSON document either way.
    """
    idx = output.find("{")
    return _json.loads(output[idx:])


def _invoke(*extra: str, mix_stderr: bool = True):
    # Click 8.2+ already separates stderr from stdout; mix_stderr kwarg is gone.
    # `result.output` returns stdout only — exactly what we want for --json tests.
    return CliRunner().invoke(pack_cmd, ["--marketplace=none", "--offline", *extra])


class TestFlagGuards:
    def test_pack_create_tag_without_check_versions_errors(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        monkeypatch.chdir(repo)
        result = _invoke("--create-tag", "--dry-run")
        assert result.exit_code == 1
        assert "--check-versions" in result.output

    def test_pack_push_without_create_tag_errors(self, tmp_path, monkeypatch, run_git_cmd):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        monkeypatch.chdir(repo)
        result = _invoke("--check-versions", "--push", "--dry-run")
        assert result.exit_code == 1
        assert "--create-tag" in result.output


class TestDryRun:
    def test_pack_create_tag_dry_run_makes_no_git_calls(self, tmp_path, monkeypatch, run_git_cmd):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        monkeypatch.chdir(repo)
        result = _invoke("--check-versions", "--create-tag", "--dry-run")
        assert result.exit_code == 0, result.output
        tags = run_git_cmd(["tag", "--list"], repo).stdout
        assert "v1.0.0" not in tags
        assert "Would create tag" in result.output

    def test_pack_create_tag_push_dry_run_makes_no_remote_calls(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        bare = tmp_path / "origin.git"
        run_git_cmd(["init", "-q", "--bare", str(bare)], tmp_path)
        run_git_cmd(["remote", "add", "origin", str(bare)], repo)
        monkeypatch.chdir(repo)
        result = _invoke("--check-versions", "--create-tag", "--push", "--dry-run")
        assert result.exit_code == 0, result.output
        remote_refs = run_git_cmd(["ls-remote", "--tags", str(bare)], repo).stdout
        assert "v1.0.0" not in remote_refs


class TestHappyPath:
    def test_pack_create_tag_and_push_happy_path_lockstep(self, tmp_path, monkeypatch, run_git_cmd):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        bare = tmp_path / "origin.git"
        run_git_cmd(["init", "-q", "--bare", str(bare)], tmp_path)
        run_git_cmd(["remote", "add", "origin", str(bare)], repo)
        run_git_cmd(["push", "-q", "origin", "main"], repo)
        monkeypatch.chdir(repo)
        result = _invoke("--check-versions", "--create-tag", "--push")
        assert result.exit_code == 0, result.output
        local_tags = run_git_cmd(["tag", "--list"], repo).stdout
        assert "v1.0.0" in local_tags
        remote_tags = run_git_cmd(["ls-remote", "--tags", str(bare)], repo).stdout
        assert "refs/tags/v1.0.0" in remote_tags

    def test_pack_create_tag_and_push_happy_path_per_package(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        repo = _scaffold_per_package(tmp_path, run_git_cmd)
        bare = tmp_path / "origin.git"
        run_git_cmd(["init", "-q", "--bare", str(bare)], tmp_path)
        run_git_cmd(["remote", "add", "origin", str(bare)], repo)
        run_git_cmd(["push", "-q", "origin", "main"], repo)
        monkeypatch.chdir(repo)
        result = _invoke("--check-versions", "--create-tag", "--push")
        assert result.exit_code == 0, result.output
        local_tags = run_git_cmd(["tag", "--list"], repo).stdout
        assert "alpha-v1.0.0" in local_tags
        assert "beta-v2.0.0" in local_tags
        remote_tags = run_git_cmd(["ls-remote", "--tags", str(bare)], repo).stdout
        assert "refs/tags/alpha-v1.0.0" in remote_tags
        assert "refs/tags/beta-v2.0.0" in remote_tags


class TestRefusalSemantics:
    def test_pack_refusal_on_dirty_tree_exits_1_not_3_or_4(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        """Dirty-tree refusal must not collide with gate exit codes (3, 4)."""
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        monkeypatch.chdir(repo)
        result = _invoke("--check-versions", "--create-tag")
        assert result.exit_code == 1
        assert result.exit_code != 3
        assert result.exit_code != 4
        assert "uncommitted changes" in result.output

    def test_pack_release_gates_still_exit_3_and_4_when_their_checks_fail(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        """Tagging block must not change existing gate exit codes."""
        repo = _scaffold_repo(tmp_path, run_git_cmd, pkg_version="0.5.0")
        monkeypatch.chdir(repo)
        # check-versions fails -> exit 3, even with --create-tag set.
        result = _invoke("--check-versions", "--create-tag", "--dry-run")
        assert result.exit_code == 3

    def test_pack_create_tag_refuses_on_missing_remote_for_push(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        monkeypatch.chdir(repo)
        result = _invoke("--check-versions", "--create-tag", "--push")
        assert result.exit_code == 1
        assert "no 'origin' remote" in result.output


class TestJsonEnvelope:
    def test_pack_json_envelope_contains_tag_creation_block_on_success(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        monkeypatch.chdir(repo)
        result = _invoke(
            "--check-versions", "--create-tag", "--dry-run", "--json", mix_stderr=False
        )
        assert result.exit_code == 0, result.output
        data = _parse_json(result.output)
        assert data["tag_creation"] is not None
        assert data["tag_creation"]["status"] == "ok"
        assert data["tag_creation"]["created"] == ["v1.0.0"]
        assert data["tag_creation"]["refusal_code"] is None
        # No push requested -> tag_push key present but null.
        assert data["tag_push"] is None

    def test_pack_json_envelope_contains_refusal_code_on_failure(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        monkeypatch.chdir(repo)
        result = _invoke("--check-versions", "--create-tag", "--json", mix_stderr=False)
        assert result.exit_code == 1, result.output
        data = _parse_json(result.output)
        assert data["tag_creation"]["status"] == "refused"
        assert data["tag_creation"]["refusal_code"] == "dirty_tree"
        assert data["ok"] is False
        codes = {e["code"] for e in data["errors"]}
        assert "dirty_tree" in codes

    def test_pack_json_envelope_no_check_versions_refusal_includes_code(
        self, tmp_path, monkeypatch, run_git_cmd
    ):
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        monkeypatch.chdir(repo)
        result = _invoke("--create-tag", "--dry-run", "--json", mix_stderr=False)
        assert result.exit_code == 1
        data = _parse_json(result.output)
        assert data["tag_creation"]["refusal_code"] == "no_check_versions"

    def test_pack_json_envelope_keys_always_present(self, tmp_path, monkeypatch, run_git_cmd):
        """Even without tagging flags, the envelope keys must appear (as null)."""
        repo = _scaffold_repo(tmp_path, run_git_cmd)
        monkeypatch.chdir(repo)
        result = _invoke("--dry-run", "--json", mix_stderr=False)
        data = _parse_json(result.output)
        assert "tag_creation" in data
        assert "tag_push" in data
        assert data["tag_creation"] is None
        assert data["tag_push"] is None


class TestHelp:
    def test_help_mentions_create_tag_and_push(self):
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0
        assert "--create-tag" in result.output
        assert "--push" in result.output
