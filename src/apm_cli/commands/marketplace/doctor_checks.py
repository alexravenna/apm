"""Diagnostic helpers for ``apm marketplace doctor``."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...marketplace.errors import MarketplaceYmlError
from ...marketplace.git_stderr import translate_git_stderr
from ...marketplace.migration import ConfigSource, detect_config_source
from ...marketplace.yml_schema import load_marketplace_from_apm_yml, load_marketplace_yml
from ._check import _find_duplicate_names
from ._doctor import _DoctorCheck


def _run_version_check(command: list[str], *, timeout: int, missing_hint: str) -> tuple[bool, str]:
    """Run a simple ``--version``-style command and summarise the outcome."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            line = result.stdout.strip().split("\n")[0]
            return True, line or "Command succeeded"
        return False, f"{' '.join(command)} returned non-zero exit code"
    except FileNotFoundError:
        return False, missing_hint
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(command)} timed out"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)[:60]


def _check_git() -> _DoctorCheck:
    passed, detail = _run_version_check(
        ["git", "--version"], timeout=5, missing_hint="git not found on PATH"
    )
    return _DoctorCheck(name="git", passed=passed, detail=detail)


def _check_network() -> _DoctorCheck:
    """Check basic GitHub network reachability via ``git ls-remote``."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "https://github.com/git/git.git", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return _DoctorCheck(name="network", passed=True, detail="github.com reachable")
        translated = translate_git_stderr(
            result.stderr,
            exit_code=result.returncode,
            operation="ls-remote",
            remote="github.com",
        )
        return _DoctorCheck(name="network", passed=False, detail=translated.hint[:80])
    except subprocess.TimeoutExpired:
        detail = "Network check timed out (5s)"
    except FileNotFoundError:
        detail = "git not found; cannot test network"
    except (subprocess.SubprocessError, OSError) as exc:
        detail = str(exc)[:60]
    return _DoctorCheck(name="network", passed=False, detail=detail)


def _check_auth() -> _DoctorCheck:
    """Check whether AuthResolver can find a GitHub token."""
    try:
        from ...core.auth import AuthResolver

        has_token = bool(AuthResolver().resolve("github.com").token)
    except Exception:
        has_token = False
    detail = "Token detected" if has_token else "No token; unauthenticated rate limits apply"
    return _DoctorCheck(name="auth", passed=True, detail=detail, informational=True)


def _check_gh_cli() -> _DoctorCheck:
    """Check whether the GitHub CLI is available."""
    passed, detail = _run_version_check(
        ["gh", "--version"],
        timeout=10,
        missing_hint="gh CLI not found (install: https://cli.github.com/)",
    )
    return _DoctorCheck(name="gh CLI", passed=passed, detail=detail, informational=True)


def _check_marketplace_config(project_root: Path) -> tuple[_DoctorCheck, object | None]:
    """Check marketplace config presence and parseability."""
    apm_path = project_root / "apm.yml"
    legacy_path = project_root / "marketplace.yml"
    yml_obj = None
    config_passed = True
    config_detail = ""
    try:
        source = detect_config_source(project_root)
        if source == ConfigSource.APM_YML:
            yml_obj = load_marketplace_from_apm_yml(apm_path)
            config_detail = "apm.yml 'marketplace:' block found and valid"
        elif source == ConfigSource.LEGACY_YML:
            yml_obj = load_marketplace_yml(legacy_path)
            config_detail = "marketplace.yml found (legacy). Run 'apm marketplace migrate' to fold it into apm.yml."
        else:
            config_detail = "No marketplace authoring config in current directory"
    except MarketplaceYmlError as exc:
        config_passed = False
        config_detail = f"Error: {str(exc)[:113]}"
    return (
        _DoctorCheck(
            name="marketplace config",
            passed=config_passed,
            detail=config_detail,
            informational=True,
        ),
        yml_obj,
    )


def _check_duplicate_names(yml_obj) -> _DoctorCheck | None:
    """Check whether the config defines duplicate package names."""
    if yml_obj is None:
        return None
    dup_detail = _find_duplicate_names(yml_obj)
    if dup_detail:
        return _DoctorCheck(
            name="duplicate names",
            passed=False,
            detail=dup_detail,
            informational=True,
        )
    return _DoctorCheck(
        name="duplicate names",
        passed=True,
        detail="No duplicate package names",
        informational=True,
    )


def _critical_checks_passed(checks) -> bool:
    """Return whether all non-informational checks passed."""
    return all(check.passed for check in checks if not check.informational)
