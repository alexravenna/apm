"""Pure message-building helpers for command_logger.

All functions here are pure: no ``_rich_*`` calls, no I/O.  Keeping
message-building separate from dispatch reduces line count in ``__init__``
while leaving all ``_rich_*`` calls in the package namespace so existing
``@patch("apm_cli.core.command_logger._rich_*")`` test patches continue to
work.
"""

from __future__ import annotations

from ._types import _strip_source_prefix

# Templates for outcomes that always emit (non-verbose).
# Placeholders: {src} = source or "this project"; {source} = raw source; {err_text} = error text.
_POLICY_MISS_ALWAYS: dict[str, tuple[str, str]] = {
    "empty": (
        "warning",
        "Org policy at {src} is present but empty; no enforcement applied",
    ),
    "malformed": (
        "warning",
        "Policy at {source} is malformed: {err_text}. "
        "Contact your org admin to fix the policy file.",
    ),
    "cache_miss_fetch_fail": (
        "warning",
        "Could not fetch org policy from {source} ({err_text}); "
        "proceeding without policy enforcement. "
        "Retry, check connectivity, or use --no-policy to bypass.",
    ),
    "garbage_response": (
        "warning",
        "Policy response from {source} is not valid YAML "
        "({err_text}); proceeding without policy enforcement. "
        "Contact your org admin or use --no-policy.",
    ),
    "cached_stale": (
        "warning",
        "Using stale cached policy (refresh failed: {err_text}); "
        "enforcement still applies from cached policy.",
    ),
    "hash_mismatch": (
        "error",
        "Policy hash mismatch: pinned hash does not match fetched "
        "policy ({err_text}). Update apm.yml policy.hash or "
        "contact your org admin.",
    ),
}


def _verbose_only_spec(
    outcome: str,
    source: str,
    host_org: str | None,
    verbose: bool,
) -> tuple[str, str] | None:
    """Return spec for verbose-only outcomes (``absent``, ``no_git_remote``)."""
    if not verbose:
        return None
    if outcome == "absent":
        org = host_org or _strip_source_prefix(source) or "this project"
        return ("info", f"No org policy found for {org}")
    return (
        "info",
        "Could not determine org from git remote; policy auto-discovery skipped",
    )


def _always_emit_spec(
    outcome: str,
    source: str,
    err_text: str,
) -> tuple[str, str] | None:
    """Return spec for outcomes that always emit, or ``None`` for unknown."""
    spec = _POLICY_MISS_ALWAYS.get(outcome)
    if spec is not None:
        style, template = spec
        src = source or "this project"
        return (style, template.format(src=src, source=source, err_text=err_text))
    if err_text and err_text != "unknown":
        return ("warning", f"Policy discovery issue: {err_text}")
    return None


def _policy_resolved_msg(
    source: str,
    cached: bool,
    enforcement: str,
    age_seconds: int | None,
) -> str:
    """Build the policy-resolved log message string."""
    parts = [f"Policy: {source}"]
    if cached:
        cache_detail = "cached"
        if age_seconds is not None:
            if age_seconds < 60:
                cache_detail += f", fetched {age_seconds}s ago"
            else:
                minutes = age_seconds // 60
                unit = "m" if minutes < 60 else "h"
                value = minutes if minutes < 60 else minutes // 60
                cache_detail += f", fetched {value}{unit} ago"
        parts.append(f"({cache_detail})")
    parts.append(f"-- enforcement={enforcement}")
    return " ".join(parts)


def _policy_miss_spec(
    outcome: str,
    source: str,
    err_text: str,
    host_org: str | None,
    verbose: bool,
) -> tuple[str, str] | None:
    """Return ``(style, message)`` for policy_discovery_miss, or ``None`` for silent.

    ``style`` is one of ``"info"``, ``"warning"``, ``"error"``.
    """
    if outcome in ("absent", "no_git_remote"):
        return _verbose_only_spec(outcome, source, host_org, verbose)
    return _always_emit_spec(outcome, source, err_text)


def _download_complete_msg(
    dep_name: str,
    ref: str,
    sha: str,
    cached: bool,
    ref_suffix: str,
) -> str:
    """Build the download-complete log message string."""
    msg = f"  [+] {dep_name}"
    if ref_suffix:
        msg += f" ({ref_suffix})"
    else:
        if ref and sha:
            msg += f" #{ref} @{sha}"
        elif ref:
            msg += f" #{ref}"
        elif sha:
            msg += f" @{sha}"
        if cached:
            msg += " (cached)"
    return msg


def _install_summary_spec(
    apm_count: int,
    mcp_count: int,
    errors: int,
    stale_cleaned: int,
    elapsed_seconds: float | None,
) -> tuple[str, str] | None:
    """Build install-summary ``(style, message)`` or ``None`` if nothing to emit.

    ``style`` is ``"success"``, ``"warning"``, or ``"error"``.
    """
    parts = []
    if apm_count > 0:
        noun = "dependency" if apm_count == 1 else "dependencies"
        parts.append(f"{apm_count} APM {noun}")
    if mcp_count > 0:
        noun = "server" if mcp_count == 1 else "servers"
        parts.append(f"{mcp_count} MCP {noun}")

    cleanup_suffix = ""
    if stale_cleaned > 0:
        file_noun = "file" if stale_cleaned == 1 else "files"
        cleanup_suffix = f" ({stale_cleaned} stale {file_noun} cleaned)"

    timing_suffix = ""
    if elapsed_seconds is not None:
        timing_suffix = f" in {elapsed_seconds:.1f}s"

    if parts:
        summary = " and ".join(parts)
        if errors > 0:
            return (
                "warning",
                f"Installed {summary}{cleanup_suffix}{timing_suffix} with {errors} error(s).",
            )
        return ("success", f"Installed {summary}{cleanup_suffix}{timing_suffix}.")
    if errors > 0:
        return ("error", f"Installation failed with {errors} error(s){timing_suffix}.")
    return None
