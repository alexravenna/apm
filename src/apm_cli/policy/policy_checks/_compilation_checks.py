"""Compilation strategy and source-attribution policy checks.

Private sibling module extracted from ``dependency_checks`` to keep that
module cohesive and under the 500-line limit.  Both check functions are
re-exported by ``dependency_checks`` so the public surface is unchanged.
"""

from __future__ import annotations

from ..models import CheckResult
from .class_ import CompilationPolicy


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


def _check_compilation_target(
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 11: compilation target matches policy."""
    enforce = policy.target.enforce
    allow = policy.target.allow

    if not enforce and allow is None:
        return CheckResult(
            name="compilation-target",
            passed=True,
            message="No compilation target restrictions configured",
        )

    target = (raw_yml or {}).get("target")
    if not target:
        return CheckResult(
            name="compilation-target",
            passed=True,
            message="No compilation target set in manifest",
        )

    # Normalize target to a list for uniform checking
    target_list = target if isinstance(target, list) else [target]

    if enforce:
        if enforce not in target_list:
            return CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Enforced target '{enforce}' not present in {target_list}",
                details=[f"target: {target}, enforced: {enforce}"],
            )
    elif allow is not None:
        allow_set = set(allow) if isinstance(allow, (list, tuple)) else {allow}
        disallowed = [t for t in target_list if t not in allow_set]
        if disallowed:
            return CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Target(s) {disallowed} not in allowed list {sorted(allow_set)}",
                details=[f"target: {target}, allowed: {sorted(allow_set)}"],
            )

    return CheckResult(
        name="compilation-target",
        passed=True,
        message="Compilation target compliant",
    )
