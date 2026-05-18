"""Legacy multi-target agent integration helper.

Extracted from :meth:`AgentIntegrator.integrate_package_agents` to keep
``agent_integrator.py`` under 500 lines.  Not part of the public API.

The function here reproduces the deprecated multi-target auto-copy behaviour
(copilot + claude + cursor simultaneously) that predates scope-aware
target-driven dispatch.  New code should call
:meth:`AgentIntegrator.integrate_agents_for_target` directly.
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.integration.base_integrator import IntegrationResult
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath


def run_legacy_multi_target_integration(
    integrator,  # AgentIntegrator instance -- not typed to avoid circular import
    package_info,
    project_root: Path,
    force: bool,
    managed_files: set | None,
    diagnostics,
) -> IntegrationResult:
    """Execute the deprecated multi-target auto-copy logic.

    Reproduces the original ``integrate_package_agents`` behaviour that
    simultaneously deployed to copilot + claude + cursor without going
    through scope-aware target resolution.  New code should call
    ``integrate_agents_for_target`` directly.

    The ``continue`` statements inside the claude/cursor secondary-target
    blocks intentionally skip both the claude *and* cursor integrations for
    a given source file when a path-traversal guard fires.  This matches the
    original logic exactly and is preserved here.

    Args:
        integrator: The calling :class:`AgentIntegrator` instance; used to
            invoke ``find_agent_files``, ``get_target_filename_for_target``,
            ``is_content_identical_to_source``, ``check_collision``, and
            ``copy_agent``.
        package_info: Package whose agents are being integrated.
        project_root: Absolute path to the project root.
        force: When ``True``, overwrite user-authored collisions.
        managed_files: Set of APM-managed relative paths from the lockfile.
        diagnostics: Optional diagnostics collector.

    Returns:
        :class:`IntegrationResult` summarising the operation.
    """
    from apm_cli.integration.targets import KNOWN_TARGETS

    copilot = KNOWN_TARGETS["copilot"]

    agents_dir = project_root / ".github" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    claude_agents_dir = None
    claude_dir = project_root / ".claude"
    if claude_dir.exists() and claude_dir.is_dir():
        claude_agents_dir = claude_dir / "agents"
        claude_agents_dir.mkdir(parents=True, exist_ok=True)

    cursor_agents_dir = None
    cursor_dir = project_root / ".cursor"
    if cursor_dir.exists() and cursor_dir.is_dir():
        cursor_agents_dir = cursor_dir / "agents"
        cursor_agents_dir.mkdir(parents=True, exist_ok=True)

    files_integrated = 0
    files_skipped = 0
    files_adopted = 0
    target_paths: list[Path] = []
    total_links_resolved = 0

    for source_file in integrator.find_agent_files(package_info.install_path):
        target_filename = integrator.get_target_filename_for_target(
            source_file,
            package_info.package.name,
            copilot,
        )
        target_path = agents_dir / target_filename
        try:
            ensure_path_within(target_path, agents_dir)
        except PathTraversalError as exc:
            if diagnostics is not None:
                diagnostics.warn(
                    message=f"Rejected agent target path: {exc}",
                    package=package_info.package.name,
                )
            files_skipped += 1
            continue
        rel_path = portable_relpath(target_path, project_root)

        if integrator.is_content_identical_to_source(target_path, source_file):
            target_paths.append(target_path)
            files_adopted += 1
        else:
            if integrator.check_collision(
                target_path, rel_path, managed_files, force, diagnostics=diagnostics
            ):
                files_skipped += 1
                continue
            links_resolved = integrator.copy_agent(source_file, target_path)
            total_links_resolved += links_resolved
            files_integrated += 1
            target_paths.append(target_path)

        if claude_agents_dir:
            claude_target = KNOWN_TARGETS["claude"]
            claude_filename = integrator.get_target_filename_for_target(
                source_file,
                package_info.package.name,
                claude_target,
            )
            claude_path = claude_agents_dir / claude_filename
            try:
                ensure_path_within(claude_path, claude_agents_dir)
            except PathTraversalError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Rejected claude agent target path: {exc}",
                        package=package_info.package.name,
                    )
                continue
            claude_rel = portable_relpath(claude_path, project_root)
            if integrator.is_content_identical_to_source(claude_path, source_file):
                target_paths.append(claude_path)
                files_adopted += 1
            elif not integrator.check_collision(
                claude_path, claude_rel, managed_files, force, diagnostics=diagnostics
            ):
                integrator.copy_agent(source_file, claude_path)
                target_paths.append(claude_path)

        if cursor_agents_dir:
            cursor_target = KNOWN_TARGETS["cursor"]
            cursor_filename = integrator.get_target_filename_for_target(
                source_file,
                package_info.package.name,
                cursor_target,
            )
            cursor_path = cursor_agents_dir / cursor_filename
            try:
                ensure_path_within(cursor_path, cursor_agents_dir)
            except PathTraversalError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Rejected cursor agent target path: {exc}",
                        package=package_info.package.name,
                    )
                continue
            cursor_rel = portable_relpath(cursor_path, project_root)
            if integrator.is_content_identical_to_source(cursor_path, source_file):
                target_paths.append(cursor_path)
                files_adopted += 1
            elif not integrator.check_collision(
                cursor_path, cursor_rel, managed_files, force, diagnostics=diagnostics
            ):
                integrator.copy_agent(source_file, cursor_path)
                target_paths.append(cursor_path)

    return IntegrationResult(
        files_integrated=files_integrated,
        files_updated=0,
        files_skipped=files_skipped,
        target_paths=target_paths,
        links_resolved=total_links_resolved,
        files_adopted=files_adopted,
    )
