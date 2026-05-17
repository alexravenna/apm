"""APM audit command -- content integrity scanning for prompt files.

Scans installed APM packages (or arbitrary files) for hidden Unicode
characters that could embed invisible instructions.  This is the first
pillar of ``apm audit``; lock-file consistency (``--ci``) and drift
detection (``--drift``) are planned as future modes.

Exit codes:
    0 -- clean (no findings, or info-only)
    1 -- critical findings detected
    2 -- warnings only (no critical)
"""

import sys
from pathlib import Path

import click

from ..core.command_logger import CommandLogger
from ..deps.lockfile import LockFile, get_lockfile_path
from ..security.content_scanner import ContentScanner
from ..security.file_scanner import scan_lockfile_packages
from ..utils.console import (
    STATUS_SYMBOLS,
)
from .audit import _audit_outcome_cause, _AuditConfig, _has_actionable_findings, _scan_single_file
from .audit_sections import (
    _apply_strip,
    _preview_strip,
    _render_ci_results,
    _render_findings_table,
    _render_summary,
)


def _audit_ci_gate(
    cfg: _AuditConfig,
    policy_source: str | None,
    no_cache: bool,
    no_policy: bool,
    no_fail_fast: bool,
    no_drift: bool = False,
) -> None:
    """Handle ``apm audit --ci`` -- lockfile consistency gate.

    Runs baseline lockfile checks, drift detection (unless ``--no-drift``),
    and (optionally) org-policy checks, then emits a structured report
    and exits with 0 (clean) or 1 (violations).
    """
    logger = cfg.logger

    from ..policy.ci_checks import _check_drift, run_baseline_checks
    from ..policy.policy_checks import run_policy_checks

    fail_fast = not no_fail_fast

    # Always run baseline checks
    ci_result = run_baseline_checks(cfg.project_root, fail_fast=fail_fast, ci_mode=True)

    # Resolve policy source: explicit --policy wins; otherwise mirror
    # install's auto-discovery (closes #827) so CI catches sideloaded
    # files via unmanaged-files checks. --no-policy skips discovery.
    from ..policy import discovery as policy_discovery
    from ..policy.project_config import (
        read_project_fetch_failure_default,
    )

    fetch_result = None
    auto_discovered = False
    if policy_source and (not fail_fast or ci_result.passed):
        fetch_result = policy_discovery.discover_policy(
            cfg.project_root,
            policy_override=policy_source,
            no_cache=no_cache,
        )
    elif not policy_source and not no_policy and (not fail_fast or ci_result.passed):
        # Auto-discovery (mirror install path)
        fetch_result = policy_discovery.discover_policy_with_chain(cfg.project_root)
        auto_discovered = True

    if fetch_result is not None:
        # Honour project-side fetch_failure_default for outcomes that
        # mean "no enforcement applied".  Pre-#1159, auto-discovery
        # silently swallowed `absent` / `no_git_remote` / `empty` /
        # `disabled` -- a fail-open governance bypass.  Now those
        # outcomes are surfaced explicitly:
        #
        #   * malformed / cache_miss_fetch_fail / garbage_response
        #     -> existing fetch-failure handling (warn unless block);
        #     applies to BOTH explicit --policy and auto-discovery.
        #   * absent / no_git_remote / empty   (auto-discovery only)
        #     -> were silently dropped pre-#1159; now surfaced as
        #        explicit warnings, and honour `block` for parity with
        #        install.  Explicit --policy keeps the legacy fall-
        #        through so an opt-in pointer at a baseline file does
        #        not regress.
        #   * disabled   (auto-discovery only)
        #     -> emit a forensic `[i]` breadcrumb in --ci mode so
        #        audit logs explain WHY no policy ran.
        fetch_failure_outcomes = (
            "malformed",
            "cache_miss_fetch_fail",
            "garbage_response",
        )
        no_policy_outcomes = ("absent", "no_git_remote", "empty")

        if auto_discovered and fetch_result.outcome == "disabled":
            click.echo(
                "[i] Org-policy auto-discovery disabled by project apm.yml "
                "(policy.discovery_enabled=false); no enforcement applied",
                err=True,
            )
            fetch_result = None
        elif (
            fetch_result.outcome in fetch_failure_outcomes
            or fetch_result.error
            or (auto_discovered and fetch_result.outcome in no_policy_outcomes)
        ):
            project_default = read_project_fetch_failure_default(cfg.project_root)
            source = fetch_result.source
            err_text = fetch_result.error or fetch_result.fetch_error or fetch_result.outcome
            cause = _audit_outcome_cause(fetch_result.outcome, source, err_text)
            if project_default == "block":
                click.echo(
                    f"[x] {cause} (policy.fetch_failure_default=block)",
                    err=True,
                )
                sys.exit(1)
            else:
                click.echo(
                    f"[!] {cause}; enforcement skipped "
                    "(set policy.fetch_failure_default=block in apm.yml to fail closed)",
                    err=True,
                )
                fetch_result = None

    if fetch_result is not None and fetch_result.found:
        policy_obj = fetch_result.policy

        # Respect enforcement level
        if policy_obj.enforcement == "off":
            pass  # Policy checks disabled
        else:
            from ..policy.models import CheckResult

            policy_result = run_policy_checks(cfg.project_root, policy_obj, fail_fast=fail_fast)
            if policy_obj.enforcement == "block":
                ci_result.checks.extend(policy_result.checks)
            else:
                # enforcement == "warn": include results but don't fail
                for check in policy_result.checks:
                    ci_result.checks.append(
                        CheckResult(
                            name=check.name,
                            passed=True,  # downgrade to pass
                            message=check.message
                            + (" (enforcement: warn)" if not check.passed else ""),
                            details=check.details,
                        )
                    )

    # -- Drift detection (default-on per ADR-02) --------------------
    drift_findings: list = []
    if not no_drift and (cfg.project_root / "apm.yml").exists():
        from ..deps.lockfile import LockFile, get_lockfile_path

        lockfile_path = get_lockfile_path(cfg.project_root)
        if lockfile_path.exists():
            lockfile = LockFile.read(lockfile_path)
            if lockfile is not None:
                drift_check, drift_findings = _check_drift(
                    cfg.project_root,
                    lockfile,
                    cache_only=True,
                    verbose=cfg.verbose,
                )
                ci_result.checks.append(drift_check)
    elif no_drift and cfg.output_format == "text":
        # In structured output (json/sarif), --no-drift is implicit from
        # the absence of the drift check entry; no need to pollute output.
        click.echo(
            f"{STATUS_SYMBOLS['warning']} drift detection skipped (--no-drift); "
            "coverage reduced -- hand-edits and missing integrations will not be caught",
            err=True,
        )

    # Resolve effective format
    effective_format = cfg.output_format
    if cfg.output_path and effective_format == "text":
        from ..security.audit_report import detect_format_from_extension

        effective_format = detect_format_from_extension(Path(cfg.output_path))

    if effective_format in ("json", "sarif"):
        import json as _json

        from ..install.drift import render_drift_json, render_drift_sarif

        if effective_format == "sarif":
            payload = ci_result.to_sarif()
            if drift_findings:
                payload["runs"][0]["results"].extend(render_drift_sarif(drift_findings))
        else:
            payload = ci_result.to_json()
            if drift_findings or not no_drift:
                payload["drift"] = render_drift_json(drift_findings)

        output = _json.dumps(payload, indent=2)
        if cfg.output_path:
            Path(cfg.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg.output_path).write_text(output, encoding="utf-8")
            logger.success(f"CI audit report written to {cfg.output_path}")
        else:
            click.echo(output)
    else:
        _render_ci_results(ci_result)
        if drift_findings:
            from ..install.drift import render_drift_text

            click.echo("")
            click.echo(render_drift_text(drift_findings, verbose=cfg.verbose))

    sys.exit(0 if ci_result.passed else 1)


def _audit_content_scan(
    cfg: _AuditConfig,
    package: str | None,
    file_path: str | None,
    strip: bool,
    dry_run: bool,
    no_drift: bool = False,
) -> None:
    """Handle default ``apm audit`` -- content integrity scanning.

    Scans deployed prompt files (or a single file via ``--file``) for
    hidden Unicode characters, optionally stripping them.
    """
    logger = cfg.logger
    project_root = cfg.project_root

    # Resolve effective format (auto-detect from extension when needed)
    effective_format = cfg.output_format
    if cfg.output_path and effective_format == "text":
        from ..security.audit_report import detect_format_from_extension

        effective_format = detect_format_from_extension(Path(cfg.output_path))

    # --format json/sarif/markdown is incompatible with --strip / --dry-run
    if effective_format != "text" and (strip or dry_run):
        logger.error(f"--format {effective_format} cannot be combined with --strip or --dry-run")
        sys.exit(1)

    if file_path:
        # -- File mode: scan a single arbitrary file --
        findings_by_file, files_scanned = _scan_single_file(Path(file_path), logger)
    else:
        # -- Package mode: scan from lockfile --
        lockfile_path = get_lockfile_path(project_root)
        if not lockfile_path.exists():
            logger.progress(
                "No apm.lock.yaml found -- nothing to scan. Use --file to scan a specific file."
            )
            sys.exit(0)

        if package:
            logger.progress(f"Scanning package: {package}")
        else:
            logger.start("Scanning all installed packages...")

        findings_by_file, files_scanned = scan_lockfile_packages(
            project_root,
            package_filter=package,
        )

        if files_scanned == 0:
            if package:
                logger.warning(
                    f"Package '{package}' not found in apm.lock.yaml or has no deployed files"
                )
            else:
                logger.progress("No deployed files found in apm.lock.yaml")
            sys.exit(0)

    # -- Warn if --dry-run used without --strip --
    if dry_run and not strip:
        logger.progress("--dry-run only works with --strip (e.g. apm audit --strip --dry-run)")

    # -- Strip mode --
    if strip:
        if not findings_by_file:
            logger.progress("Nothing to clean -- no hidden characters found")
            sys.exit(0)
        if dry_run:
            _preview_strip(findings_by_file, logger)
            sys.exit(0)
        modified = _apply_strip(findings_by_file, project_root, logger)
        if modified > 0:
            logger.success(f"Cleaned {modified} file(s)")
        else:
            logger.progress("Nothing to clean -- no strippable characters found")
        sys.exit(0)

    # -- Drift detection (default-on per ADR-02) --------------------
    # Drift only applies to whole-project audit (not --file or --strip
    # modes; not single-package scoped).  Mutex on no_drift+strip/file
    # is enforced earlier via UsageError.
    drift_findings: list = []
    drift_failed = False
    if (
        not no_drift
        and not strip
        and not file_path
        and not package
        and (project_root / "apm.yml").exists()
    ):
        from ..policy.ci_checks import DRIFT_SKIP_PREFIX, _check_drift

        lockfile_path = get_lockfile_path(project_root)
        if lockfile_path.exists():
            lockfile = LockFile.read(lockfile_path)
            if lockfile is not None:
                drift_check, drift_findings = _check_drift(
                    project_root,
                    lockfile,
                    cache_only=True,
                    verbose=cfg.verbose,
                )
                drift_failed = not drift_check.passed
                # Bare `apm audit` is advisory: drift_failed does not gate
                # the exit code (that lives in --ci). But silence on a
                # cache-pin / cache-miss skip or failure is a UX trap: the
                # user cannot tell whether drift was clean or whether it was
                # never attempted. Surface the reason on stderr whenever the
                # drift check produced no findings.
                if drift_failed and not drift_findings:
                    click.echo(
                        f"{STATUS_SYMBOLS['warning']} drift check could not run: "
                        f"{drift_check.message}",
                        err=True,
                    )
                elif (
                    drift_check.passed
                    and not drift_findings
                    and drift_check.message.startswith(DRIFT_SKIP_PREFIX)
                ):
                    click.echo(
                        f"{STATUS_SYMBOLS['warning']} {drift_check.message}",
                        err=True,
                    )
    elif no_drift and cfg.output_format == "text":
        # In structured output (json/sarif), --no-drift is implicit from
        # the absence of the drift check entry; no need to pollute output.
        click.echo(
            f"{STATUS_SYMBOLS['warning']} drift detection skipped (--no-drift); "
            "coverage reduced -- hand-edits and missing integrations will not be caught",
            err=True,
        )

    # -- Display findings --
    # Determine exit code first (shared by all formats)
    if not findings_by_file or not _has_actionable_findings(findings_by_file):
        exit_code = 0
    else:
        all_findings = [f for ff in findings_by_file.values() for f in ff]
        exit_code = 1 if ContentScanner.has_critical(all_findings) else 2

    # Note: bare `apm audit` is advisory for drift; drift findings are
    # rendered (text/json/sarif) but DO NOT escalate the exit code. Use
    # `apm audit --ci` (handled in _audit_ci_gate) to gate on drift.
    _ = drift_failed  # retained for symmetry; gate path lives in --ci.

    if effective_format == "text":
        if cfg.output_path:
            logger.error(
                "Text format does not support --output. "
                "Use --format json, sarif, or markdown to write to a file."
            )
            sys.exit(1)
        if findings_by_file:
            _render_findings_table(findings_by_file, verbose=cfg.verbose)
        _render_summary(findings_by_file, files_scanned, logger)
        if drift_findings:
            from ..install.drift import render_drift_text

            click.echo("")
            click.echo(render_drift_text(drift_findings, verbose=cfg.verbose))
    elif effective_format == "markdown":
        from ..security.audit_report import findings_to_markdown

        md_report = findings_to_markdown(findings_by_file, files_scanned=files_scanned)
        if cfg.output_path:
            Path(cfg.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg.output_path).write_text(md_report, encoding="utf-8")
            logger.success(f"Audit report written to {cfg.output_path}")
        else:
            click.echo(md_report)
    else:
        from ..security.audit_report import (
            findings_to_json,
            findings_to_sarif,
            serialize_report,
            write_report,
        )

        if effective_format == "sarif":
            report = findings_to_sarif(findings_by_file, files_scanned=files_scanned)
        else:
            report = findings_to_json(
                findings_by_file,
                files_scanned=files_scanned,
                exit_code=exit_code,
            )

        if cfg.output_path:
            write_report(report, Path(cfg.output_path))
            logger.success(f"Audit report written to {cfg.output_path}")
        else:
            click.echo(serialize_report(report))

    # -- Exit code --
    sys.exit(exit_code)


def audit(
    ctx,
    package,
    file_path,
    strip,
    verbose,
    dry_run,
    output_format,
    output_path,
    ci,
    policy_source,
    no_cache,
    no_policy,
    no_fail_fast,
    no_drift,
):
    """Scan deployed prompt files for hidden Unicode characters.

    Detects invisible characters that could embed hidden instructions in
    prompt, instruction, and rules files. Dangerous and suspicious
    characters can be removed with --strip.

    By default, also runs install-replay drift detection: catches
    hand-edits to deployed files, missing integrations, and orphaned
    files vs the lockfile.  Use --no-drift to skip (reduces coverage).

    With --ci, runs lockfile consistency checks AND drift in machine-
    readable format, suitable for CI/CD pipeline gates.

    \b
    Exit codes:
        0  Clean, info-only findings, or drift-only (advisory) in bare
           audit, or successful strip
        1  Critical findings detected, or --ci with violations
           (including drift in --ci mode)
        2  Warning-only findings (suspicious but not critical), or
           usage error (mutually exclusive flags)

    \b
    Examples:
        apm audit                      # Scan + drift (all checks)
        apm audit my-package           # Scan a specific package
        apm audit --file .cursorrules  # Scan any file (no drift)
        apm audit --strip              # Remove dangerous/suspicious chars
        apm audit --no-drift           # Skip drift only (escape hatch)
        apm audit --ci                 # CI gate (lockfile + drift)
        apm audit --ci --no-drift      # CI gate without drift (rare)
        apm audit --ci --policy org    # CI gate with org policy checks
        apm audit --ci -f json         # JSON CI report
        apm audit --ci -f sarif        # SARIF for GitHub Code Scanning
        apm audit -o report.sarif      # Write SARIF to file
    """
    project_root = Path.cwd()
    logger = CommandLogger("audit", verbose=verbose)

    cfg = _AuditConfig(
        project_root=project_root,
        logger=logger,
        verbose=verbose,
        output_format=output_format,
        output_path=output_path,
    )

    # --no-drift is a different audit mode from --strip / --file (those
    # are content-scanning operations unrelated to integration drift).
    # Click-native UsageError gives exit code 2 with "Usage:" prefix.
    if no_drift and (strip or file_path):
        raise click.UsageError(
            "--no-drift cannot be combined with --strip or --file "
            "(those modes do not run drift detection)"
        )

    # -- CI mode: lockfile consistency gate -------------------------
    if ci:
        if verbose:
            logger.warning("--verbose has no effect in --ci mode (output is structured)")
        if strip or dry_run or file_path or package:
            logger.error("--ci cannot be combined with --strip, --dry-run, --file, or PACKAGE")
            sys.exit(1)
        if output_format == "markdown":
            logger.error("--ci does not support --format markdown. Use json or sarif.")
            sys.exit(1)

        _audit_ci_gate(cfg, policy_source, no_cache, no_policy, no_fail_fast, no_drift)
        return  # _audit_ci_gate calls sys.exit; return guards against fall-through

    # -- Content scan mode ------------------------------------------
    if policy_source:
        logger.warning(
            "--policy requires --ci mode. "
            "Use 'apm audit --ci --policy <source>' to run policy checks."
        )

    _audit_content_scan(cfg, package, file_path, strip, dry_run, no_drift)
