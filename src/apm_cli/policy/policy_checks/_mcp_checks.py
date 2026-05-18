"""MCP server policy checks.

Private sibling module extracted from ``dependency_checks`` to keep that
module under the 500-line limit.  All four check functions are re-exported
by ``dependency_checks`` so the public surface is unchanged.
"""

from __future__ import annotations

from ..models import CheckResult
from .class_ import McpPolicy


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
