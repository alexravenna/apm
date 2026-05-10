"""End-to-end integration tests for `apm update` and `apm install --frozen`.

Issue: https://github.com/microsoft/apm/issues/1203 (P0).

Validates the full pipeline against a real GitHub package:

* `apm update --dry-run` resolves, renders a plan, and writes nothing.
* `apm update --yes` after install with no manifest changes is a no-op.
* `apm install --frozen` succeeds against an in-sync lockfile.
* `apm install --frozen` exits non-zero when lockfile is missing.
* `apm install --frozen` exits non-zero when manifest declares a dep
  not present in the lockfile.
* `apm install` (no flags, no manifest changes) emits the
  "Run 'apm update' to check for newer versions." hint.
* `apm install --frozen --update` is rejected as a usage error.

Uses the real `microsoft/apm-sample-package`. Requires GITHUB_APM_PAT
or GITHUB_TOKEN.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def apm_command():
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    project_dir = tmp_path / "update-test"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    # Per #1154, vscode/copilot target detection requires this signal file.
    (project_dir / ".github" / "copilot-instructions.md").write_text("# test\n")
    return project_dir


def _run_apm(apm_command, args, cwd, timeout=180, stdin_input=None):
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin_input,
    )


def _write_apm_yml(project_dir: Path, apm_packages: list[str]) -> None:
    config = {
        "name": "update-test",
        "version": "1.0.0",
        "dependencies": {"apm": apm_packages, "mcp": []},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


class TestUpdateE2E:
    def test_update_dry_run_writes_nothing(self, temp_project, apm_command):
        """`apm update --dry-run` prints a plan and writes no artifacts."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result = _run_apm(apm_command, ["update", "--dry-run"], temp_project)

        assert result.returncode == 0, (
            f"Dry-run failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        assert "Dry run" in result.stdout or "plan" in result.stdout.lower()
        assert not (temp_project / "apm.lock.yaml").exists()

    def test_update_after_install_no_changes_short_circuits(self, temp_project, apm_command):
        """After `apm install`, a follow-up `apm update --yes` with no
        manifest changes should report all-up-to-date and not fail."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        first = _run_apm(apm_command, ["install"], temp_project)
        assert first.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {first.stdout}\nSTDERR: {first.stderr}"
        )
        assert (temp_project / "apm.lock.yaml").exists()

        result = _run_apm(apm_command, ["update", "--yes"], temp_project)

        assert result.returncode == 0, (
            f"Update failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )


class TestFrozenE2E:
    def test_frozen_succeeds_against_in_sync_lockfile(self, temp_project, apm_command):
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        first = _run_apm(apm_command, ["install"], temp_project)
        assert first.returncode == 0, first.stderr

        # Re-run with --frozen on the same manifest+lockfile.
        result = _run_apm(apm_command, ["install", "--frozen"], temp_project)

        assert result.returncode == 0, (
            f"Frozen install failed on in-sync project:\n{result.stdout}\n{result.stderr}"
        )

    def test_frozen_fails_when_lockfile_missing(self, temp_project, apm_command):
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result = _run_apm(apm_command, ["install", "--frozen"], temp_project)

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "frozen" in combined.lower() or "lock" in combined.lower()

    def test_frozen_fails_when_manifest_adds_undeclared_dep(self, temp_project, apm_command):
        """Lockfile present but manifest gained a dep that isn't in lock -> fail."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        first = _run_apm(apm_command, ["install"], temp_project)
        assert first.returncode == 0, first.stderr

        _write_apm_yml(
            temp_project,
            ["microsoft/apm-sample-package", "microsoft/some-other-not-in-lock"],
        )

        result = _run_apm(apm_command, ["install", "--frozen"], temp_project)

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "out of sync" in combined.lower() or "missing" in combined.lower()

    def test_frozen_with_update_is_rejected(self, temp_project, apm_command):
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result = _run_apm(apm_command, ["install", "--frozen", "--update"], temp_project)

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "frozen" in combined.lower() and "update" in combined.lower()


class TestNoOpHintE2E:
    def test_install_emits_update_hint_when_lockfile_present_and_no_changes(
        self, temp_project, apm_command
    ):
        """`apm install` with no manifest changes and a present lockfile
        emits the "Run 'apm update' to check for newer versions." hint."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        first = _run_apm(apm_command, ["install"], temp_project)
        assert first.returncode == 0, first.stderr

        result = _run_apm(apm_command, ["install"], temp_project)

        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "apm update" in combined, f"Missing nudge in:\n{combined}"
