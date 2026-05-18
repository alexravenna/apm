"""Helper functions for ``apm pack`` and ``apm unpack``."""

import json as json_mod
from pathlib import Path

import click

from ..core.build_orchestrator import OutputKind
from ..utils.path_security import validate_path_segments


def _emit_json_error_or_raise(ctx, json_output: bool, code: str, message: str):
    """Emit a JSON error envelope to stdout or raise ClickException."""
    if json_output:
        from ..marketplace.builder import BuildReport

        click.echo(
            json_mod.dumps(
                BuildReport.failure_to_json_dict(errors=[{"code": code, "message": message}])
            )
        )
        ctx.exit(1)
    else:
        raise click.ClickException(message)


def _parse_marketplace_path_overrides(
    ctx,
    json_output: bool,
    marketplace_path_overrides,
) -> dict[str, str]:
    """Parse ``--marketplace-path`` overrides into a dict."""
    from ..marketplace.output_profiles import known_output_names

    path_overrides: dict[str, str] = {}
    for override in marketplace_path_overrides:
        if "=" not in override:
            msg = f"--marketplace-path must be FORMAT=PATH, got: {override!r}"
            _emit_json_error_or_raise(ctx, json_output, "cli_error", msg)
            return {}

        fmt_name, path_val = override.split("=", 1)
        fmt_name = fmt_name.strip()
        path_val = path_val.strip()
        if fmt_name not in known_output_names():
            msg = (
                f"Unknown marketplace format '{fmt_name}' in --marketplace-path. "
                f"Known formats: {', '.join(sorted(known_output_names()))}"
            )
            _emit_json_error_or_raise(ctx, json_output, "unknown_format", msg)
            return {}

        try:
            validate_path_segments(path_val, context="--marketplace-path", allow_current_dir=True)
        except Exception as exc:
            _emit_json_error_or_raise(ctx, json_output, "path_error", str(exc))
            return {}
        path_overrides[fmt_name] = path_val
    return path_overrides


def _parse_marketplace_filter(ctx, json_output: bool, marketplace_filter) -> tuple[str, ...] | None:
    """Parse the marketplace format filter from CLI input."""
    from ..marketplace.output_profiles import known_output_names

    if marketplace_filter is None:
        return None

    normalised = marketplace_filter.strip().lower()
    if normalised == "none":
        return ()
    if normalised == "all":
        return None

    requested = [item.strip() for item in marketplace_filter.split(",") if item.strip()]
    known = known_output_names()
    for requested_name in requested:
        if requested_name in known:
            continue
        msg = (
            f"Unknown marketplace format '{requested_name}' in --marketplace. "
            f"Known formats: {', '.join(sorted(known))}"
        )
        _emit_json_error_or_raise(ctx, json_output, "unknown_format", msg)
        return None
    return tuple(requested)


def _resolve_effective_target(project_root: Path, target, logger):
    """Resolve the informational target metadata recorded in packed bundles."""
    if target is not None:
        logger.warning(
            "--target is deprecated and will be removed in a future release. "
            "Bundles are target-agnostic; the value is recorded as informational "
            "pack.target metadata only and is ignored by 'apm install'."
        )
        return target

    from ..core.target_detection import detect_target

    try:
        detected, _reason = detect_target(project_root)
        return detected if detected else None
    except Exception:
        return None


def _emit_pack_json(result, dry_run: bool) -> None:
    """Emit the stable JSON envelope for ``apm pack --json``."""
    envelope = {
        "ok": True,
        "dry_run": dry_run,
        "warnings": [],
        "errors": [],
        "marketplace": {"outputs": []},
        "bundle": None,
    }
    for sub_result in result.producer_results:
        if sub_result.kind is not OutputKind.MARKETPLACE or sub_result.payload is None:
            continue
        payload = sub_result.payload.to_json_dict()
        envelope["warnings"] = payload.get("warnings", [])
        envelope["marketplace"] = payload.get("marketplace", {"outputs": []})
        break
    click.echo(json_mod.dumps(envelope, indent=2))


def _render_bundle_result(logger, pack_result, fmt, target, dry_run):
    """Mirror the legacy ``apm pack`` output for the bundle producer."""
    if pack_result is None:
        return

    mapping_summary = _mapping_summary(pack_result.path_mappings)
    if dry_run:
        if pack_result.mapped_count:
            logger.dry_run_notice(
                f"Would remap {pack_result.mapped_count} file(s){mapping_summary}"
            )
            for mapped, original in pack_result.path_mappings.items():
                logger.verbose_detail(f"    {original} -> {mapped}")
        if pack_result.files:
            logger.dry_run_notice(
                f"Would pack {len(pack_result.files)} file(s) -> {pack_result.bundle_path}"
            )
            for file_path in pack_result.files:
                logger.tree_item(f"  {file_path}")
        else:
            _warn_empty(logger, target, pack_result)
        return

    if pack_result.mapped_count:
        logger.progress(f"Mapped {pack_result.mapped_count} file(s){mapping_summary}")
        for mapped, original in pack_result.path_mappings.items():
            logger.verbose_detail(f"    {original} -> {mapped}")

    if not pack_result.files:
        _warn_empty(logger, target, pack_result)
        return

    logger.success(f"Packed {len(pack_result.files)} file(s) -> {pack_result.bundle_path}")
    for file_path in pack_result.files:
        logger.verbose_detail(f"    {file_path}")
    if fmt == "plugin":
        logger.progress(
            "Plugin bundle ready -- contains plugin.json plus "
            "plugin-native directories (agents/, skills/, commands/, ...) "
            "and an embedded apm.lock.yaml for install-time integrity "
            "verification."
        )
    if pack_result.bundle_path:
        logger.info(f"Share with: apm install {pack_result.bundle_path}")


def _render_marketplace_result(logger, report, dry_run, extra_warnings=None, outputs=None):
    """Render the marketplace producer's report (one-liner summary)."""
    seen_warnings = set()
    for warn_msg in extra_warnings or []:
        seen_warnings.add(warn_msg)
        logger.warning(warn_msg)
    for warn_msg in getattr(report, "warnings", ()) or ():
        if warn_msg in seen_warnings:
            continue
        seen_warnings.add(warn_msg)
        logger.warning(warn_msg)

    output_reports = tuple(getattr(report, "outputs", ()) or ())
    if not output_reports:
        package_count = len(getattr(report, "resolved", ()) or ()) if report is not None else None
        for output in outputs or []:
            message = f"marketplace.json -> {output}"
            if package_count is not None:
                message = f"marketplace.json ({package_count} package(s)) -> {output}"
            if dry_run:
                logger.dry_run_notice(f"Would write {message}")
            else:
                logger.success(f"Built {message}")
        return

    for output_report in output_reports:
        message = (
            f"marketplace.json [{output_report.profile}] "
            f"({len(output_report.resolved)} package(s)) -> {output_report.output_path}"
        )
        if dry_run or output_report.dry_run:
            logger.dry_run_notice(f"Would write {message}")
        else:
            logger.success(f"Built {message}")


def _log_unpack_file_list(result, logger):
    """Log unpacked files grouped by dependency, using tree-style output."""
    if result.dependency_files:
        for dep_name, dep_files in result.dependency_files.items():
            logger.progress(f"  {dep_name}")
            for file_path in dep_files:
                logger.tree_item(f"    - {file_path}")
        return

    for file_path in result.files:
        logger.tree_item(f"  - {file_path}")


def _mapping_summary(path_mappings):
    """Build a compact ': src/ -> dst/' suffix from path mappings, or empty string."""
    if not path_mappings:
        return ""
    src_sample = next(iter(path_mappings.values()))
    dst_sample = next(iter(path_mappings))
    src_root = src_sample.split("/")[0] + "/"
    dst_root = dst_sample.split("/")[0] + "/"
    return f": {src_root} -> {dst_root}"


def _warn_empty(logger, target, result):
    """Emit a contextual warning when the bundle has no files."""
    if not target:
        logger.warning("No deployed files found -- empty bundle created")
        return

    logger.warning(f"No files to pack for target '{target}'")
    if not (result.path_mappings or result.mapped_count):
        logger.verbose_detail(
            "    Hint: check that apm.lock.yaml has deployed_files entries (run apm install first)"
        )


def _log_bundle_meta(result, output_dir, logger):
    """Show bundle provenance and warn if target mismatches the project."""
    meta = result.pack_meta
    if not meta:
        return

    bundle_target = meta.get("target", "")
    dep_count = len(result.dependency_files) if result.dependency_files else 0
    file_count = len(result.files) if result.files else 0
    display_map = {"vscode": "copilot", "agents": "copilot"}
    display_bundle = display_map.get(bundle_target, bundle_target)
    logger.progress(f"Bundle target: {display_bundle} ({dep_count} dep(s), {file_count} file(s))")

    try:
        from ..core.target_detection import detect_target

        project_target, _reason = detect_target(output_dir.resolve())
    except Exception:
        return

    display_project = display_map.get(project_target, project_target)
    canonical_map = {"copilot": "vscode", "agents": "vscode"}
    norm_bundle = canonical_map.get(bundle_target, bundle_target)
    norm_project = canonical_map.get(project_target, project_target)
    if norm_bundle == "all" or norm_project in ("all", "minimal"):
        return
    if norm_bundle != norm_project:
        logger.warning(
            f"Bundle target '{display_bundle}' differs from project target '{display_project}'"
        )
        logger.verbose_detail(
            f"    To get a {display_project}-targeted bundle, "
            f"ask the publisher to run: apm pack --target {display_project}"
        )
