"""End-to-end tests for ``apm pack --check-versions --create-tag --push``.

Spawns a real git repository plus a local bare remote and exercises the
full pack-tagging flow. No network access, no GPG, fully hermetic.

Coverage map (user-visible promises -> test):

* Promise A (lockstep + create-tag + push)
  -> ``test_pack_check_versions_create_tag_push_end_to_end``
* Promise B (``tag_pattern`` strategy renders ``build.tagPattern``)
  -> ``test_pack_tag_pattern_strategy_creates_templated_tag_e2e``
* Promise E (push refuses when tag already on remote, fail-closed via
  ``ls-remote`` preflight)
  -> ``test_pack_push_refuses_when_tag_already_on_remote_e2e``
* Promise F (``--check-versions`` failure exits 3 AND leaves NO tag
  behind on disk -- side-effect-free guarantee)
  -> ``test_pack_version_mismatch_blocks_tag_no_side_effects_e2e``
* Promise G (idempotent re-run: second invocation refuses cleanly
  with ``tag_exists`` when the tag is already local)
  -> ``test_pack_rerun_refuses_when_tag_exists_locally_e2e``
* Promise J (``--json`` envelope keeps ``tag_creation`` /
  ``tag_push`` shape stable across success AND refusal)
  -> ``test_pack_json_envelope_stable_across_success_and_refusal_e2e``
"""

from __future__ import annotations

import json as _json
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


# ---------------------------------------------------------------------------
# Extra fixtures for promise B (tag_pattern) and re-use across new tests.
# ---------------------------------------------------------------------------


def _scaffold_tag_pattern(tmp_path: Path) -> tuple[Path, Path]:
    """Scaffold a repo using ``versioning.strategy: tag_pattern`` with a
    custom ``build.tagPattern`` of ``{name}--v{version}`` (the literal
    pattern from the original session brief). Two packages so we also
    confirm the renderer produces distinct tags per package.
    """
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "apm.yml").write_text(
        textwrap.dedent(
            """\
            name: e2e-tag-pattern
            description: tag_pattern e2e.
            version: 9.9.9
            marketplace:
              owner:
                name: ACME
              versioning:
                strategy: tag_pattern
              build:
                tagPattern: "{name}--v{version}"
              packages:
                - name: gamma
                  source: ./packages/gamma
                  description: G.
                  version: 3.1.4
                - name: delta
                  source: ./packages/delta
                  description: D.
                  version: 0.0.1
            """
        ),
        encoding="utf-8",
    )
    for name, ver in (("gamma", "3.1.4"), ("delta", "0.0.1")):
        pkg = repo / "packages" / name
        pkg.mkdir(parents=True)
        pkg.joinpath("apm.yml").write_text(
            f"name: {name}\ndescription: x.\nversion: {ver}\n", encoding="utf-8"
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


def _parse_json_envelope(output: str) -> dict:
    """Extract the JSON envelope from CliRunner stdout."""
    idx = output.find("{")
    assert idx >= 0, f"no JSON object in output: {output!r}"
    return _json.loads(output[idx:])


# ---------------------------------------------------------------------------
# Promise B: tag_pattern strategy materializes the configured template.
# ---------------------------------------------------------------------------


def test_pack_tag_pattern_strategy_creates_templated_tag_e2e(tmp_path, monkeypatch):
    """``strategy: tag_pattern`` + ``build.tagPattern: "{name}--v{version}"``
    in apm.yml must materialize one tag per local package using the
    rendered template, both locally and on origin.

    Mutation-break verified: substituting ``per_package`` for the
    ``tag_pattern`` branch in ``GitTagger.plan_tags`` makes both
    assertions on remote tag names fail (tags render as
    ``{name}-v{version}`` with one dash instead of two).
    """
    repo, bare = _scaffold_tag_pattern(tmp_path)
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
    assert "gamma--v3.1.4" in local, local
    assert "delta--v0.0.1" in local, local
    remote = _run_git(["ls-remote", "--tags", str(bare)], repo).stdout
    assert "refs/tags/gamma--v3.1.4" in remote, remote
    assert "refs/tags/delta--v0.0.1" in remote, remote


# ---------------------------------------------------------------------------
# Promise F: --check-versions failure exits 3 and creates NO tags.
# ---------------------------------------------------------------------------


def test_pack_version_mismatch_blocks_tag_no_side_effects_e2e(tmp_path, monkeypatch):
    """When the version gate fails (exit 3), ``--create-tag`` must be a
    strict no-op: no tag may appear in ``git tag --list`` after the run.

    The unit-tier counterpart only asserts the exit code; this test
    closes the silent-side-effect gap by inspecting the on-disk tag
    list with a REAL git repo.

    Mutation-break verified: deleting the
    ``if version_gate_failed or drift_gate_failed: return None, None, False``
    short-circuit in ``_run_tagging`` causes the tag ``v5.0.0`` to be
    created on disk, failing the post-condition assertion.
    """
    repo, _bare = _scaffold(tmp_path)
    # Bump marketplace top-level version above the package version so the
    # lockstep version gate fails (package 2.0.0 vs marketplace 5.0.0).
    apm_yml = repo / "apm.yml"
    apm_yml.write_text(apm_yml.read_text().replace("version: 2.0.0", "version: 5.0.0", 1))
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "bump"], repo).check_returncode()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(
        pack_cmd,
        [
            "--marketplace=none",
            "--offline",
            "--check-versions",
            "--create-tag",
        ],
    )
    # Exit 3 = version gate failure per docs/reference/cli/pack.md.
    assert result.exit_code == 3, (result.exit_code, result.output)
    # Critical post-condition: no tag was materialized.
    tags = _run_git(["tag", "--list"], repo).stdout.strip()
    assert "v5.0.0" not in tags, f"tag leaked despite failed gate: {tags!r}"
    assert "v2.0.0" not in tags, f"tag leaked despite failed gate: {tags!r}"


# ---------------------------------------------------------------------------
# Promise G: idempotent re-run when local tag exists -> refusal exit 1.
# ---------------------------------------------------------------------------


def test_pack_rerun_refuses_when_tag_exists_locally_e2e(tmp_path, monkeypatch):
    """A second ``apm pack --create-tag`` after a successful first run
    must refuse cleanly (``tag_exists`` / exit 1), not silently
    overwrite, and not produce a duplicate.

    Mutation-break verified: deleting the
    ``if existing_local:`` raise block in ``GitTagger.preflight``
    causes the re-run to attempt ``git tag -a`` again, which exits
    non-zero with ``tag already exists`` and surfaces as
    ``git_failure`` (not ``tag_exists``), failing the refusal-code
    assertion below.
    """
    repo, _bare = _scaffold(tmp_path)
    monkeypatch.chdir(repo)
    # First run: creates v2.0.0 locally (no --push to avoid remote noise).
    first = CliRunner().invoke(
        pack_cmd,
        ["--marketplace=none", "--offline", "--check-versions", "--create-tag"],
    )
    assert first.exit_code == 0, first.output
    assert "v2.0.0" in _run_git(["tag", "--list"], repo).stdout

    # Second run: same state. Must refuse with tag_exists / exit 1.
    second = CliRunner().invoke(
        pack_cmd,
        [
            "--marketplace=none",
            "--offline",
            "--check-versions",
            "--create-tag",
            "--json",
        ],
    )
    assert second.exit_code == 1, (second.exit_code, second.output)
    envelope = _parse_json_envelope(second.output)
    assert envelope["tag_creation"]["status"] == "refused"
    assert envelope["tag_creation"]["refusal_code"] == "tag_exists"
    assert envelope["ok"] is False
    # Exactly one tag still on disk: no duplication, no overwrite.
    tag_lines = [
        line for line in _run_git(["tag", "--list"], repo).stdout.splitlines() if line.strip()
    ]
    assert tag_lines == ["v2.0.0"], tag_lines


# ---------------------------------------------------------------------------
# Promise E: --push refuses fail-closed when tag already on remote.
# ---------------------------------------------------------------------------


def test_pack_push_refuses_when_tag_already_on_remote_e2e(tmp_path, monkeypatch):
    """If the target tag already exists on ``origin`` (e.g. somebody
    else pushed it from a sibling clone), the preflight ``ls-remote``
    check must refuse with ``tag_exists`` BEFORE any local tag is
    materialized -- so neither side drifts.

    Setup: scaffold a working repo + bare origin, push ``v2.0.0`` from
    a sibling clone, then run ``apm pack ... --push`` from the working
    repo. The working repo has no local ``v2.0.0`` yet, so the
    refusal proves the remote check fired.

    Mutation-break verified: deleting the
    ``existing_remote = self._existing_remote_tags(...)`` /
    ``if existing_remote:`` block in ``GitTagger.preflight`` lets the
    flow proceed to ``create`` (which succeeds locally) and ``push``
    (which then fails at the wire with ``git_failure``, not
    ``tag_exists``), failing the refusal-code assertion.
    """
    repo, bare = _scaffold(tmp_path)
    # Stage the tag from a sibling clone so the remote already has v2.0.0.
    sibling = tmp_path / "sibling"
    _run_git(["clone", "-q", str(bare), str(sibling)], tmp_path).check_returncode()
    _run_git(["config", "user.name", "Sibling"], sibling)
    _run_git(["config", "user.email", "sib@example.com"], sibling)
    _run_git(["config", "commit.gpgSign", "false"], sibling)
    _run_git(["config", "tag.gpgSign", "false"], sibling)
    _run_git(["tag", "-a", "-m", "Release v2.0.0", "v2.0.0"], sibling).check_returncode()
    _run_git(["push", "origin", "refs/tags/v2.0.0:refs/tags/v2.0.0"], sibling).check_returncode()
    # Sanity: tag is on the remote, but NOT on the working repo yet.
    assert "v2.0.0" not in _run_git(["tag", "--list"], repo).stdout
    assert "refs/tags/v2.0.0" in _run_git(["ls-remote", "--tags", str(bare)], repo).stdout

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(
        pack_cmd,
        [
            "--marketplace=none",
            "--offline",
            "--check-versions",
            "--create-tag",
            "--push",
            "--json",
        ],
    )
    assert result.exit_code == 1, (result.exit_code, result.output)
    envelope = _parse_json_envelope(result.output)
    # Preflight refusal: no local tag was created, payload is "refused".
    assert envelope["tag_creation"]["status"] == "refused"
    assert envelope["tag_creation"]["refusal_code"] == "tag_exists"
    assert envelope["tag_push"] is None, envelope["tag_push"]
    # Critical post-condition: working repo still has no local tag.
    assert "v2.0.0" not in _run_git(["tag", "--list"], repo).stdout
    # Actionable hint surfaced in stderr (logger.error path).
    assert "tag_exists" in {e["code"] for e in envelope["errors"]}


# ---------------------------------------------------------------------------
# Promise J: --json envelope shape stable across success AND refusal.
# ---------------------------------------------------------------------------


def test_pack_json_envelope_stable_across_success_and_refusal_e2e(tmp_path, monkeypatch):
    """The contract documented in pack.md:
    ``tag_creation.refusal_code`` and ``tag_push.refusal_code`` carry
    the stable code on failure, and the keys are present on success
    too (with ``refusal_code: null``). Downstream ``jq`` consumers
    must never see a missing key.

    This e2e variant runs the SAME repo through a success path and a
    failure path back-to-back (idempotency triggers the failure), so
    both branches of the envelope shape are observed with real
    fixtures in one test.
    """
    repo, bare = _scaffold(tmp_path)
    monkeypatch.chdir(repo)

    # Success path: full create + push.
    ok = CliRunner().invoke(
        pack_cmd,
        [
            "--marketplace=none",
            "--offline",
            "--check-versions",
            "--create-tag",
            "--push",
            "--json",
        ],
    )
    assert ok.exit_code == 0, ok.output
    env_ok = _parse_json_envelope(ok.output)
    for key in ("ok", "dry_run", "warnings", "errors", "tag_creation", "tag_push"):
        assert key in env_ok, f"missing envelope key on success: {key}"
    assert env_ok["tag_creation"]["status"] == "ok"
    assert env_ok["tag_creation"]["created"] == ["v2.0.0"]
    assert env_ok["tag_creation"]["refusal_code"] is None
    assert env_ok["tag_push"]["status"] == "ok"
    assert env_ok["tag_push"]["pushed"] == ["v2.0.0"]
    assert env_ok["tag_push"]["remote"] == "origin"
    assert env_ok["tag_push"]["refusal_code"] is None

    # Failure path: same flags, but the tag now exists locally and on
    # the remote -> preflight refusal. Both payload keys must still be
    # present, and tag_creation.refusal_code must carry the code.
    bad = CliRunner().invoke(
        pack_cmd,
        [
            "--marketplace=none",
            "--offline",
            "--check-versions",
            "--create-tag",
            "--push",
            "--json",
        ],
    )
    assert bad.exit_code == 1, bad.output
    env_bad = _parse_json_envelope(bad.output)
    for key in ("ok", "dry_run", "warnings", "errors", "tag_creation", "tag_push"):
        assert key in env_bad, f"missing envelope key on refusal: {key}"
    assert env_bad["ok"] is False
    assert env_bad["tag_creation"]["status"] == "refused"
    assert env_bad["tag_creation"]["refusal_code"] == "tag_exists"
    assert env_bad["tag_creation"]["created"] == []
    # Remote also pre-existed (from the prior success), so push is not
    # reached -- payload stays None per contract.
    assert env_bad["tag_push"] is None
    codes = {e["code"] for e in env_bad["errors"]}
    assert "tag_exists" in codes, codes
    # Use bare in an assertion so the fixture binding is not unused.
    assert "refs/tags/v2.0.0" in _run_git(["ls-remote", "--tags", str(bare)], repo).stdout
