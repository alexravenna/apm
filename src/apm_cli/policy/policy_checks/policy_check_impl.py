"""Policy checks for organisational governance enforcement.

These checks run WITH a policy file and validate that the project's manifest,
lockfile, and on-disk state comply with the organisation's declared policies.
They are always run in addition to the baseline checks in ``ci_checks``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from ..models import CheckResult, CIAuditResult
from .class_ import (
    ApmPolicy,
    LockFile,
    UnmanagedFilesPolicy,
)
from .dependency_checks import (
    _check_compilation_strategy,
    _check_compilation_target,
    _check_dependency_allowlist,
    _check_dependency_denylist,
    _check_includes_explicit,
    _check_mcp_allowlist,
    _check_mcp_denylist,
    _check_mcp_self_defined,
    _check_mcp_transport,
    _check_required_manifest_fields,
    _check_required_package_version,
    _check_required_packages,
    _check_required_packages_deployed,
    _check_scripts_policy,
    _check_source_attribution,
    _check_transitive_depth,
    _load_raw_apm_yml,
)

_logger = logging.getLogger(__name__)
_INCLUDES_NOT_PROVIDED = object()
_DEFAULT_GOVERNANCE_DIRS = [
    ".github/agents",
    ".github/instructions",
    ".github/hooks",
    ".cursor/rules",
    ".claude",
    ".opencode",
]
_MAX_UNMANAGED_SCAN_FILES = 10_000


def run_dependency_policy_checks(
    deps_to_install,
    *,
    lockfile=None,
    policy: ApmPolicy,
    mcp_deps=None,
    effective_target: str | None = None,
    fetch_outcome: str | None = None,
    fail_fast: bool = True,
    manifest_includes=_INCLUDES_NOT_PROVIDED,
) -> CIAuditResult:
    """Evaluate :class:`ApmPolicy` against an already-resolved dependency set.

    Used by both ``apm audit --ci`` (after resolving from disk) and the
    install pipeline ``policy_gate`` phase.  Reuses the private ``_check_*``
    helpers -- no logic duplication.

    Parameters
    ----------
    deps_to_install:
        Iterable of ``DependencyReference`` (the resolved set, including
        transitives).  This is what ``InstallContext.deps_to_install``
        contains after the resolve phase.
    lockfile:
        An ``ApmLockfile`` / ``LockFile`` instance, or ``None``.  Needed
        for deployed-files and version-pin checks.
    policy:
        The effective :class:`ApmPolicy` to enforce.
    mcp_deps:
        Iterable of ``MCPDependency`` objects, or ``None``.  When the
        resolved set includes MCP entries they are checked against
        ``policy.mcp``.
    effective_target:
        The post-targets-phase compilation target string, or ``None``.
        When ``None`` target/compilation checks are **skipped** (they
        belong to the separate W2-target-aware call).
    fetch_outcome:
        Human-readable label for diagnostic context (e.g.
        ``"cached"``, ``"fetched"``).  Currently informational only.
    fail_fast:
        Stop after the first failing check (default ``True``).
    manifest_includes:
        The parsed value of the manifest's ``includes:`` field
        (``None``, ``"auto"``, or a list of paths).  When omitted,
        the ``explicit-includes`` check is skipped -- callers that
        do not have manifest information available (e.g. dep-only
        seams) can leave it unset.

    Returns
    -------
    CIAuditResult
        Contains individual :class:`CheckResult` entries.  The caller
        decides how to map ``enforcement`` level (block vs warn) onto
        these results.

    Notes
    -----
    ``require_resolution: project-wins`` semantics (rubber-duck I7):
    version-pin mismatches are downgraded to warnings; missing required
    packages still block; inherited org deny still wins.  This is
    handled inside ``_check_required_package_version`` which already
    reads ``policy.dependencies.require_resolution``.

    Does **not** load ``apm.yml`` from disk -- the caller supplies the
    resolved dep set directly.
    """
    result = CIAuditResult()
    deps_list = list(deps_to_install)
    mcp_list = list(mcp_deps) if mcp_deps is not None else []

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # -- Dependency checks (1-6) -----------------------------------
    if _run(_check_dependency_allowlist(deps_list, policy.dependencies)):
        return result
    if _run(_check_dependency_denylist(deps_list, policy.dependencies)):
        return result
    if _run(_check_required_packages(deps_list, policy.dependencies)):
        return result
    if _run(_check_required_packages_deployed(deps_list, lockfile, policy.dependencies)):
        return result
    if _run(_check_required_package_version(deps_list, lockfile, policy.dependencies)):
        return result
    if _run(_check_transitive_depth(lockfile, policy.dependencies)):
        return result

    # -- MCP checks (7-10) ----------------------------------------
    # When mcp_deps is None (not provided), skip MCP checks entirely.
    # When mcp_deps is an empty list (provided but no MCP deps), still
    # run MCP checks so they report "no X configured" for completeness.
    if mcp_deps is not None:
        if _run(_check_mcp_allowlist(mcp_list, policy.mcp)):
            return result
        if _run(_check_mcp_denylist(mcp_list, policy.mcp)):
            return result
        if _run(_check_mcp_transport(mcp_list, policy.mcp)):
            return result
        if _run(_check_mcp_self_defined(mcp_list, policy.mcp)):
            return result

    # -- Target / compilation checks (11-13) -----------------------
    # Skipped when effective_target is None -- those run in a separate
    # post-targets call (W2-target-aware).
    if effective_target is not None:
        # Build a minimal raw_yml dict so _check_compilation_target
        # sees the effective (possibly CLI-overridden) target value
        # rather than what is literally on disk.
        synthetic_yml = {"target": effective_target}
        if _run(_check_compilation_target(synthetic_yml, policy.compilation)):
            return result

    # -- Manifest-level explicit-includes check --------------------
    # Only run when the caller supplied the manifest includes value.
    # Dep-only seams that lack manifest context (legacy callers) skip
    # this check; the install pipeline and ``apm audit`` wrappers both
    # supply it.
    if manifest_includes is not _INCLUDES_NOT_PROVIDED:
        if _run(_check_includes_explicit(manifest_includes, policy.manifest)):
            return result

    # NOTE: compilation strategy, source attribution, manifest fields,
    # scripts policy, and unmanaged files are disk-level / manifest-level
    # concerns.  They are NOT included in the resolved-dep seam because
    # the install pipeline does not have the raw manifest at this point
    # and they are already covered by the full ``run_policy_checks``
    # wrapper that ``apm audit --ci`` calls.

    return result


def run_policy_checks(
    project_root: Path,
    policy: ApmPolicy,
    *,
    fail_fast: bool = True,
) -> CIAuditResult:
    """Run the full set of policy checks against a project on disk.

    Thin wrapper: loads manifest + lockfile from *project_root*, resolves
    deps, and delegates dependency/MCP checks to
    :func:`run_dependency_policy_checks`.  Then appends the disk-level
    checks (compilation, manifest, unmanaged files) that require the raw
    ``apm.yml``.

    These checks are ADDED to baseline checks -- caller runs both.
    When *fail_fast* is ``True`` (default), stops after the first
    failing check.
    Returns :class:`CIAuditResult` with individual check results.
    """
    from ...deps.lockfile import LockFile, get_lockfile_path
    from ...models.apm_package import APMPackage, clear_apm_yml_cache

    result = CIAuditResult()

    # Load manifest
    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        return result

    import yaml

    try:
        clear_apm_yml_cache()
        manifest = APMPackage.from_apm_yml(apm_yml_path)
    except (ValueError, yaml.YAMLError, OSError) as exc:
        result.checks.append(
            CheckResult(
                name="manifest-parse",
                passed=False,
                message=f"Cannot parse apm.yml: {exc} -- fix the YAML syntax error in apm.yml and re-run.",
            )
        )
        return result

    # Load lockfile (optional -- some checks work without it)
    lockfile_path = get_lockfile_path(project_root)
    lock = LockFile.read(lockfile_path) if lockfile_path.exists() else None

    # Load raw YAML for field-level checks
    raw_yml = _load_raw_apm_yml(project_root)

    # Get dependencies from manifest (disk view)
    apm_deps = manifest.get_apm_dependencies()
    mcp_deps = manifest.get_mcp_dependencies()

    # Read effective target from raw manifest for the full-project path
    # NOTE: the wrapper does NOT pass effective_target to the dep seam.
    # Target checks run as disk-level checks below (reading raw_yml),
    # because the wrapper has the on-disk manifest.  The install pipeline
    # will pass effective_target directly (W2-target-aware).

    # -- Delegate dependency + MCP checks to shared seam ---------------
    dep_result = run_dependency_policy_checks(
        apm_deps,
        lockfile=lock,
        policy=policy,
        mcp_deps=mcp_deps,
        # effective_target=None: target checks handled below from raw_yml
        fail_fast=fail_fast,
        manifest_includes=manifest.includes,
    )
    result.checks.extend(dep_result.checks)

    # Early exit if dep checks already failed in fail-fast mode
    if fail_fast and not dep_result.passed:
        return result

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # -- Disk-level checks that only apply to full-project audits --

    # Compilation checks (11-13) -- all run from raw_yml in wrapper
    if _run(_check_compilation_target(raw_yml, policy.compilation)):
        return result
    if _run(_check_compilation_strategy(raw_yml, policy.compilation)):
        return result
    if _run(_check_source_attribution(raw_yml, policy.compilation)):
        return result

    # Manifest checks (14-15)
    if _run(_check_required_manifest_fields(raw_yml, policy.manifest)):
        return result
    if _run(_check_scripts_policy(raw_yml, policy.manifest)):
        return result

    # Unmanaged files check (16)
    _run(_check_unmanaged_files(project_root, lock, policy.unmanaged_files))

    return result


def _check_unmanaged_files(
    project_root: Path,
    lock: LockFile | None,
    policy: UnmanagedFilesPolicy,
) -> CheckResult:
    """Check 16: no untracked files in governance directories."""
    if policy.effective_action == "ignore":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="Unmanaged files check disabled (action: ignore)",
        )

    dirs = policy.directories if policy.directories else _DEFAULT_GOVERNANCE_DIRS

    # Build set of deployed files AND directory prefixes from lockfile
    deployed: set = set()
    deployed_dir_prefixes: list = []
    if lock:
        for _key, dep in lock.dependencies.items():
            for f in dep.deployed_files:
                cleaned = f.rstrip("/")
                deployed.add(cleaned)
                if f.endswith("/"):
                    deployed_dir_prefixes.append(cleaned + "/")

    dir_prefix_tuple = tuple(deployed_dir_prefixes)

    policy_checks_pkg = sys.modules.get("apm_cli.policy.policy_checks")
    max_scan_files = getattr(
        policy_checks_pkg, "_MAX_UNMANAGED_SCAN_FILES", _MAX_UNMANAGED_SCAN_FILES
    )

    unmanaged: list[str] = []
    files_scanned = 0
    cap_hit = False
    for gov_dir in dirs:
        dir_path = project_root / gov_dir
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        for file_path in dir_path.rglob("*"):
            if file_path.is_file():
                files_scanned += 1
                if files_scanned > max_scan_files:
                    cap_hit = True
                    break
                rel = file_path.relative_to(project_root).as_posix()
                if rel not in deployed and not (
                    dir_prefix_tuple and rel.startswith(dir_prefix_tuple)
                ):
                    unmanaged.append(rel)
        if cap_hit:
            break

    if cap_hit:
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=(f"Scan capped at {max_scan_files:,} files -- skipping unmanaged-files check"),
            details=[
                f"Governance directories contain > {max_scan_files:,} files; "
                "consider adding exclude patterns in a future policy version"
            ],
        )

    if not unmanaged:
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="No unmanaged files in governance directories",
        )

    if policy.effective_action == "warn":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=f"{len(unmanaged)} unmanaged file(s) found (warn)",
            details=unmanaged,
        )

    # action == "deny"
    return CheckResult(
        name="unmanaged-files",
        passed=False,
        message=f"{len(unmanaged)} unmanaged file(s) in governance directories",
        details=unmanaged,
    )
