"""Dependency allow/deny/require and transitive-depth policy checks.

Private sibling module extracted from ``dependency_checks`` to keep that
module cohesive and under the 500-line limit.  All five check functions are
re-exported by ``dependency_checks`` so the public surface is unchanged.
"""

from __future__ import annotations

from ..models import CheckResult
from .class_ import DependencyPolicy, DependencyReference, LockFile


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


def _check_required_package_version(
    deps: list[DependencyReference],
    lock: LockFile | None,
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 5: required packages with version pins match per resolution strategy."""
    pinned = [(r, r.split("#", 1)) for r in policy.effective_require if "#" in r]
    if not pinned or lock is None:
        return CheckResult(
            name="required-package-version",
            passed=True,
            message="No version-pinned required packages",
        )

    resolution = policy.require_resolution
    violations: list[str] = []
    warnings: list[str] = []

    lock_by_name = {locked.get_unique_key(): locked for _key, locked in lock.dependencies.items()}

    for _req, parts in pinned:
        pkg_name, expected_ref = parts[0], parts[1]

        locked = lock_by_name.get(pkg_name)
        if locked is not None:
            actual_ref = locked.resolved_ref or ""
            if actual_ref != expected_ref:
                detail = f"{pkg_name}: expected ref '{expected_ref}', got '{actual_ref}'"
                if resolution in {"block", "policy-wins"}:  # noqa: PLR1714
                    violations.append(detail)
                else:  # project-wins
                    warnings.append(detail)

    if not violations:
        return CheckResult(
            name="required-package-version",
            passed=True,
            message="Required package versions match"
            + (f" (warnings: {len(warnings)})" if warnings else ""),
            details=warnings,
        )
    return CheckResult(
        name="required-package-version",
        passed=False,
        message=f"{len(violations)} version mismatch(es)",
        details=violations,
    )
