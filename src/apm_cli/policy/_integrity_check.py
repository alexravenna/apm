"""Content-integrity check for the baseline CI audit.

Extracted from :mod:`apm_cli.policy.ci_checks` to keep that module within
the line-count budget.  All symbols are re-exported from
``apm_cli.policy.ci_checks``; existing callers need no changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..deps.lockfile import _SELF_KEY
from .models import CheckResult

if TYPE_CHECKING:
    from ..deps.lockfile import LockFile


def _check_content_integrity(
    project_root: Path,
    lock: LockFile,
) -> CheckResult:
    """Check deployed files for critical hidden Unicode and hash drift.

    Two signals are evaluated:
      * Critical hidden Unicode (steganographic markers) via the file
        scanner.
      * SHA-256 drift between the on-disk content and the hash recorded
        in ``deployed_file_hashes`` at install time.

    Missing files are deliberately skipped here -- ``_check_deployed_files_present``
    already reports those, and double-reporting muddies the audit output.
    Symlinks are skipped because they may legitimately point elsewhere,
    and lockfile entries without a recorded hash (e.g. directories) are
    skipped silently.
    """
    from ..security.file_scanner import scan_lockfile_packages
    from ..utils.content_hash import compute_file_hash

    findings_by_file, _files_scanned = scan_lockfile_packages(project_root)

    # Only critical findings fail this check
    critical_files: list[str] = []
    for rel_path, findings in findings_by_file.items():
        if any(f.severity == "critical" for f in findings):
            critical_files.append(rel_path)

    # Per-file hash verification across all dependencies (the synthesized
    # self-entry is included in ``lock.dependencies`` so local content is
    # covered through the same iteration).
    hash_mismatches: list[tuple] = []  # (dep_key, rel_path, expected, actual)
    # Local import: matches the scoping pattern used in
    # _check_deployed_files_present; avoids cycles.
    from ..integration.base_integrator import BaseIntegrator as _BaseIntegrator

    for dep_key, dep in lock.dependencies.items():
        if not dep.deployed_file_hashes:
            continue
        for rel_path, expected_hash in dep.deployed_file_hashes.items():
            # Path safety: silently skip any rel_path that escapes
            # project_root or targets a non-allowlisted prefix.  Mirrors
            # the guard in _check_deployed_files_present so a forged
            # lockfile cannot induce reads outside managed locations.
            safe_rel = rel_path.rstrip("/")
            if not _BaseIntegrator.validate_deploy_path(safe_rel, project_root):
                continue
            file_path = project_root / safe_rel
            if not file_path.exists():
                continue  # _check_deployed_files_present owns this signal
            if file_path.is_symlink():
                continue
            if not file_path.is_file():
                continue
            actual_hash = compute_file_hash(file_path)
            if actual_hash != expected_hash:
                hash_mismatches.append((dep_key, rel_path, expected_hash, actual_hash))

    if not critical_files and not hash_mismatches:
        return CheckResult(
            name="content-integrity",
            passed=True,
            message="No critical hidden Unicode or hash drift detected",
        )

    details: list[str] = []
    for rel_path in critical_files:
        details.append(f"unicode: {rel_path}")
    for dep_key, rel_path, expected, actual in hash_mismatches:
        # Truncate hashes for terminal width; full hashes available via JSON output.
        exp_short = expected.split(":", 1)[-1][:12] if ":" in expected else expected[:12]
        act_short = actual.split(":", 1)[-1][:12] if ":" in actual else actual[:12]
        # Render the synthesized self-entry with a friendly label rather
        # than the internal _SELF_KEY constant ("." is opaque to users).
        dep_label = "<self>" if dep_key == _SELF_KEY else dep_key
        details.append(
            f"hash-drift: {rel_path} (dep={dep_label}, expected={exp_short}..., actual={act_short}...)"
        )

    parts: list[str] = []
    remedies: list[str] = []
    if critical_files:
        parts.append(f"{len(critical_files)} file(s) with critical hidden Unicode")
        remedies.append("'apm audit --strip' to clean Unicode")
    if hash_mismatches:
        parts.append(f"{len(hash_mismatches)} file(s) with hash drift")
        remedies.append("'apm install' to restore drifted files")
    summary = "; ".join(parts)
    remedy = " and ".join(remedies)
    return CheckResult(
        name="content-integrity",
        passed=False,
        message=f"{summary} -- run {remedy}",
        details=details,
    )
