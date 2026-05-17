"""Policy checks for organisational governance enforcement.

These checks run WITH a policy file and validate that the project's manifest,
lockfile, and on-disk state comply with the organisation's declared policies.
They are always run in addition to the baseline checks in ``ci_checks``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import CheckResult, CIAuditResult
from .class_ import (
    ApmPolicy,
    CompilationPolicy,
    DependencyPolicy,
    DependencyReference,
    LockFile,
    ManifestPolicy,
    McpPolicy,
    UnmanagedFilesPolicy,
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
    kwargs = {
        "lockfile": lockfile,
        "policy": policy,
        "mcp_deps": mcp_deps,
        "effective_target": effective_target,
        "fetch_outcome": fetch_outcome,
        "fail_fast": fail_fast,
    }
    if manifest_includes is not _INCLUDES_NOT_PROVIDED:
        kwargs["manifest_includes"] = manifest_includes
    return _policy_check_impl.run_dependency_policy_checks(deps_to_install, **kwargs)


def run_policy_checks(
    project_root: Path, policy: ApmPolicy, *, fail_fast: bool = True
) -> CIAuditResult:
    return _policy_check_impl.run_policy_checks(project_root, policy, fail_fast=fail_fast)


def _load_raw_apm_yml(project_root: Path) -> dict | None:
    """Load raw apm.yml as a dict for policy checks that inspect raw fields.

    This helper is called **after** :pymethod:`APMPackage.from_apm_yml` has
    already succeeded in :func:`run_policy_checks`.  The primary security
    gate is ``from_apm_yml()`` -- if it fails, the audit aborts with a
    ``manifest-parse`` check result and this function is never reached.

    Returning ``None`` here is therefore **defence-in-depth**: it covers
    edge cases (TOCTOU race, transient I/O error) where the file becomes
    unreadable between the two calls.  Callers that receive ``None``
    gracefully skip supplementary raw-field checks (e.g.
    ``compilation-target``, ``extensions-present``) rather than hard-failing.

    Returns ``None`` when the file is absent, unreadable, malformed YAML,
    or not a mapping -- but logs a warning so the failure is visible
    rather than silently swallowed.
    """
    import yaml

    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        return None
    try:
        with open(apm_yml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        # TOCTOU: file disappeared between exists() check and open(); normal condition.
        return None
    except yaml.YAMLError as exc:
        _logger.warning("Malformed YAML in %s: %s", apm_yml_path, exc)
        return None
    except OSError as exc:
        _logger.warning("Cannot read %s: %s", apm_yml_path, exc)
        return None
    except UnicodeDecodeError as exc:
        _logger.warning("Cannot decode %s as UTF-8: %s", apm_yml_path, exc)
        return None
    if not isinstance(data, dict):
        _logger.warning(
            "apm.yml is not a YAML mapping (got %s) -- skipping raw-field checks",
            type(data).__name__,
        )
        return None
    return data


def _check_dependency_allowlist(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 1: every dependency matches policy allow list."""
    from ..matcher import check_dependency_allowed

    if policy.allow is None:
        return CheckResult(
            name="dependency-allowlist",
            passed=True,
            message="No dependency allow list configured",
        )

    violations: list[str] = []
    for dep in deps:
        ref = dep.get_canonical_dependency_string()
        allowed, reason = check_dependency_allowed(ref, policy)
        if not allowed and "not in allowed" in reason:
            violations.append(f"{ref}: {reason}")

    if not violations:
        return CheckResult(
            name="dependency-allowlist",
            passed=True,
            message="All dependencies match allow list",
        )
    return CheckResult(
        name="dependency-allowlist",
        passed=False,
        message=f"{len(violations)} dependency(ies) not in allow list",
        details=violations,
    )


def _check_dependency_denylist(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 2: no dependency matches policy deny list."""
    from ..matcher import check_dependency_allowed

    if not policy.effective_deny:
        return CheckResult(
            name="dependency-denylist",
            passed=True,
            message="No dependency deny list configured",
        )

    violations: list[str] = []
    for dep in deps:
        ref = dep.get_canonical_dependency_string()
        allowed, reason = check_dependency_allowed(ref, policy)
        if not allowed and "denied by pattern" in reason:
            violations.append(f"{ref}: {reason}")

    if not violations:
        return CheckResult(
            name="dependency-denylist",
            passed=True,
            message="No dependencies match deny list",
        )
    return CheckResult(
        name="dependency-denylist",
        passed=False,
        message=f"{len(violations)} dependency(ies) match deny list",
        details=violations,
    )


def _check_required_packages(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 3: every required package is in manifest deps."""
    if not policy.effective_require:
        return CheckResult(
            name="required-packages",
            passed=True,
            message="No required packages configured",
        )

    dep_names = {dep.get_canonical_dependency_string().split("#")[0] for dep in deps}
    missing: list[str] = []
    for req in policy.effective_require:
        pkg_name = req.split("#")[0]
        if pkg_name not in dep_names:
            missing.append(pkg_name)

    if not missing:
        return CheckResult(
            name="required-packages",
            passed=True,
            message="All required packages present in manifest",
        )
    return CheckResult(
        name="required-packages",
        passed=False,
        message=f"{len(missing)} required package(s) missing from manifest",
        details=missing,
    )


def _check_required_packages_deployed(
    deps: list[DependencyReference],
    lock: LockFile | None,
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 4: required packages appear in lockfile with deployed files."""
    if not policy.effective_require or lock is None:
        return CheckResult(
            name="required-packages-deployed",
            passed=True,
            message="No required packages to verify deployment",
        )

    dep_names = {dep.get_canonical_dependency_string().split("#")[0] for dep in deps}
    lock_by_name = {locked.get_unique_key(): locked for _key, locked in lock.dependencies.items()}
    not_deployed: list[str] = []
    for req in policy.effective_require:
        pkg_name = req.split("#")[0]
        if pkg_name not in dep_names:
            continue  # not in manifest -- check 3 handles this

        # Find in lockfile by exact key match
        locked = lock_by_name.get(pkg_name)
        if not locked or not locked.deployed_files:
            not_deployed.append(pkg_name)

    if not not_deployed:
        return CheckResult(
            name="required-packages-deployed",
            passed=True,
            message="All required packages deployed",
        )
    return CheckResult(
        name="required-packages-deployed",
        passed=False,
        message=f"{len(not_deployed)} required package(s) not deployed",
        details=not_deployed,
    )


def _check_required_package_version(
    deps: list[DependencyReference], lock: LockFile | None, policy: DependencyPolicy
) -> CheckResult:
    return _policy_check_impl._check_required_package_version(deps, lock, policy)


def _check_transitive_depth(
    lock: LockFile | None,
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 6: no lockfile dep exceeds max_depth."""
    if lock is None or policy.max_depth >= 50:
        return CheckResult(
            name="transitive-depth",
            passed=True,
            message="No transitive depth limit configured"
            if policy.max_depth >= 50
            else "No lockfile to check",
        )

    violations: list[str] = []
    for key, dep in lock.dependencies.items():
        if dep.depth > policy.max_depth:
            violations.append(f"{key}: depth {dep.depth} exceeds limit {policy.max_depth}")

    if not violations:
        return CheckResult(
            name="transitive-depth",
            passed=True,
            message=f"All dependencies within depth limit ({policy.max_depth})",
        )
    return CheckResult(
        name="transitive-depth",
        passed=False,
        message=f"{len(violations)} dependency(ies) exceed max depth {policy.max_depth}",
        details=violations,
    )


def _check_mcp_allowlist(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 7: MCP server names match allow list."""
    from ..matcher import check_mcp_allowed

    if policy.allow is None:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="No MCP allow list configured",
        )

    violations: list[str] = []
    for mcp in mcp_deps:
        allowed, reason = check_mcp_allowed(mcp.name, policy)
        if not allowed and "not in allowed" in reason:
            violations.append(f"{mcp.name}: {reason}")

    if not violations:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="All MCP servers match allow list",
        )
    return CheckResult(
        name="mcp-allowlist",
        passed=False,
        message=f"{len(violations)} MCP server(s) not in allow list",
        details=violations,
    )


def _check_mcp_denylist(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 8: no MCP server matches deny list."""
    from ..matcher import check_mcp_allowed

    if not policy.deny:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP deny list configured",
        )

    violations: list[str] = []
    for mcp in mcp_deps:
        allowed, reason = check_mcp_allowed(mcp.name, policy)
        if not allowed and "denied by pattern" in reason:
            violations.append(f"{mcp.name}: {reason}")

    if not violations:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP servers match deny list",
        )
    return CheckResult(
        name="mcp-denylist",
        passed=False,
        message=f"{len(violations)} MCP server(s) match deny list",
        details=violations,
    )


def _check_mcp_transport(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 9: MCP transport values match policy allow list."""
    allowed_transports = policy.transport.allow
    if allowed_transports is None:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="No MCP transport restrictions configured",
        )

    violations: list[str] = []
    for mcp in mcp_deps:
        if mcp.transport and mcp.transport not in allowed_transports:
            violations.append(
                f"{mcp.name}: transport '{mcp.transport}' not in allowed {allowed_transports}"
            )

    if not violations:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="All MCP transports comply with policy",
        )
    return CheckResult(
        name="mcp-transport",
        passed=False,
        message=f"{len(violations)} MCP transport violation(s)",
        details=violations,
    )


def _check_mcp_self_defined(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 10: self-defined MCP servers comply with policy."""
    self_defined_policy = policy.self_defined
    if self_defined_policy == "allow":
        return CheckResult(
            name="mcp-self-defined",
            passed=True,
            message="Self-defined MCP servers allowed",
        )

    self_defined = [m for m in mcp_deps if m.registry is False]
    if not self_defined:
        return CheckResult(
            name="mcp-self-defined",
            passed=True,
            message="No self-defined MCP servers found",
        )

    details = [f"{m.name}: self-defined server" for m in self_defined]
    if self_defined_policy == "deny":
        return CheckResult(
            name="mcp-self-defined",
            passed=False,
            message=f"{len(self_defined)} self-defined MCP server(s) denied by policy",
            details=details,
        )
    # warn -- pass but with details
    return CheckResult(
        name="mcp-self-defined",
        passed=True,
        message=f"{len(self_defined)} self-defined MCP server(s) (warn)",
        details=details,
    )


def _check_compilation_target(raw_yml: dict | None, policy: CompilationPolicy) -> CheckResult:
    return _policy_check_impl._check_compilation_target(raw_yml, policy)


def _check_compilation_strategy(
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 12: compilation strategy matches policy."""
    enforce = policy.strategy.enforce
    if not enforce:
        return CheckResult(
            name="compilation-strategy",
            passed=True,
            message="No compilation strategy enforced",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    strategy = compilation.get("strategy") if isinstance(compilation, dict) else None
    if not strategy:
        return CheckResult(
            name="compilation-strategy",
            passed=True,
            message="No compilation strategy set in manifest",
        )

    if strategy != enforce:
        return CheckResult(
            name="compilation-strategy",
            passed=False,
            message=f"Strategy '{strategy}' does not match enforced '{enforce}'",
            details=[f"strategy: {strategy}, enforced: {enforce}"],
        )
    return CheckResult(
        name="compilation-strategy",
        passed=True,
        message="Compilation strategy compliant",
    )


def _check_source_attribution(
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 13: source attribution enabled if policy requires."""
    if not policy.source_attribution:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution not required by policy",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    attribution = compilation.get("source_attribution") if isinstance(compilation, dict) else None
    if attribution is True:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution enabled",
        )
    return CheckResult(
        name="source-attribution",
        passed=False,
        message="Source attribution required by policy but not enabled in manifest",
        details=["Set compilation.source_attribution: true in apm.yml"],
    )


def _check_required_manifest_fields(
    raw_yml: dict | None,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check 14: all required fields are present with non-empty values."""
    if not policy.required_fields:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="No required manifest fields configured",
        )

    data = raw_yml or {}
    missing: list[str] = []
    for field_name in policy.required_fields:
        value = data.get(field_name)
        if not value:  # None, empty string, missing
            missing.append(field_name)

    if not missing:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="All required manifest fields present",
        )
    return CheckResult(
        name="required-manifest-fields",
        passed=False,
        message=f"{len(missing)} required manifest field(s) missing",
        details=missing,
    )


def _check_includes_explicit(manifest_includes, policy: ManifestPolicy) -> CheckResult:
    return _policy_check_impl._check_includes_explicit(manifest_includes, policy)


def _check_scripts_policy(
    raw_yml: dict | None,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check 15: scripts section absent if policy denies it."""
    if policy.scripts != "deny":
        return CheckResult(
            name="scripts-policy",
            passed=True,
            message="Scripts allowed by policy",
        )

    scripts = (raw_yml or {}).get("scripts")
    if scripts:
        return CheckResult(
            name="scripts-policy",
            passed=False,
            message="Scripts section present but denied by policy",
            details=list(scripts.keys()) if isinstance(scripts, dict) else ["scripts"],
        )
    return CheckResult(
        name="scripts-policy",
        passed=True,
        message="No scripts section (compliant with deny policy)",
    )


def _check_unmanaged_files(
    project_root: Path, lock: LockFile | None, policy: UnmanagedFilesPolicy
) -> CheckResult:
    return _policy_check_impl._check_unmanaged_files(project_root, lock, policy)


from . import policy_check_impl as _policy_check_impl
