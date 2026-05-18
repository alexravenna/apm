"""Manifest field, scripts-policy, and raw-YAML loading helpers.

Private sibling module extracted from ``dependency_checks`` to keep that
module cohesive and under the 500-line limit.  All three public symbols are
re-exported by ``dependency_checks`` so the public surface is unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import CheckResult
from .class_ import ManifestPolicy

_logger = logging.getLogger(__name__)


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
    data = None
    load_succeeded = False
    try:
        with open(apm_yml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        load_succeeded = True
    except FileNotFoundError:
        # TOCTOU: file disappeared between exists() check and open(); normal condition.
        pass
    except yaml.YAMLError as exc:
        _logger.warning("Malformed YAML in %s: %s", apm_yml_path, exc)
    except OSError as exc:
        _logger.warning("Cannot read %s: %s", apm_yml_path, exc)
    except UnicodeDecodeError as exc:
        _logger.warning("Cannot decode %s as UTF-8: %s", apm_yml_path, exc)
    if not load_succeeded:
        return None
    if not isinstance(data, dict):
        _logger.warning(
            "apm.yml is not a YAML mapping (got %s) -- skipping raw-field checks",
            type(data).__name__,
        )
        return None
    return data


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


def _check_includes_explicit(
    manifest_includes,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check: manifest declares an explicit ``includes:`` list when policy requires it.

    ``manifest_includes`` is the parsed value of the manifest's ``includes:``
    field as exposed by :class:`APMPackage` -- one of ``None`` (field
    absent), the literal string ``"auto"``, or a list of repo-relative
    path strings.

    Violation when ``policy.require_explicit_includes`` is True and
    ``manifest_includes`` is ``None`` or ``"auto"``.
    """
    if not policy.require_explicit_includes:
        return CheckResult(
            name="explicit-includes",
            passed=True,
            message="Explicit includes not required by policy",
        )

    if manifest_includes is None:
        return CheckResult(
            name="explicit-includes",
            passed=False,
            message=(
                "Policy requires explicit 'includes:' paths but none are "
                "declared. Add 'includes: [<path>, ...]' to apm.yml with "
                "the paths you intend to publish."
            ),
            details=[
                "includes: <absent>, require_explicit_includes: true",
            ],
        )

    if manifest_includes == "auto":
        return CheckResult(
            name="explicit-includes",
            passed=False,
            message=(
                "Policy requires explicit 'includes:' paths but manifest "
                "uses 'includes: auto'. Replace with an explicit list of "
                "paths."
            ),
            details=[
                "includes: 'auto', require_explicit_includes: true",
            ],
        )

    return CheckResult(
        name="explicit-includes",
        passed=True,
        message="Manifest declares explicit includes paths",
    )
