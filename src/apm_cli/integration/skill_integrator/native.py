"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import shutil
from pathlib import Path

from .class_ import SkillIntegrationResult
from .naming import normalize_skill_name, validate_skill_name
from .typing_helpers import should_install_skill


def _integrate_native_skill(
    self,
    package_info,
    project_root: Path,
    source_skill_md: Path,
    diagnostics=None,
    managed_files=None,
    force: bool = False,
    logger=None,
    targets=None,
) -> SkillIntegrationResult:
    """Copy a native Skill (with existing SKILL.md) to all active targets.

    For packages that already have a SKILL.md at their root (like those from
    awesome-claude-skills), we copy the entire skill folder to every active
    target that supports skills (driven by ``active_targets()``).

    The skill folder name is the source folder name (e.g., ``mcp-builder``),
    validated and normalized per the agentskills.io spec.

    Source SKILL.md is copied verbatim -- no metadata injection. Orphan
    detection uses apm.lock via directory name matching instead.

    Copies:
    - SKILL.md (required)
    - scripts/ (optional)
    - references/ (optional)
    - assets/ (optional)
    - Any other subdirectories the package contains

    Args:
        package_info: PackageInfo object with package metadata
        project_root: Root directory of the project
        source_skill_md: Path to the source SKILL.md file

    Returns:
        SkillIntegrationResult: Results of the integration operation
    """
    package_path = package_info.install_path

    # Use the source folder name as the skill name
    # e.g., apm_modules/ComposioHQ/awesome-claude-skills/mcp-builder -> mcp-builder
    raw_skill_name = package_path.name

    # Validate skill name per agentskills.io spec
    is_valid, error_msg = validate_skill_name(raw_skill_name)
    if is_valid:
        skill_name = raw_skill_name
    else:
        # Normalize the name if validation fails
        skill_name = normalize_skill_name(raw_skill_name)
        if diagnostics is not None:
            diagnostics.warn(
                f"Skill name '{raw_skill_name}' normalized to '{skill_name}' ({error_msg})",
                package=raw_skill_name,
            )
        elif logger:
            logger.warning(
                f"Skill name '{raw_skill_name}' normalized to '{skill_name}' ({error_msg})"
            )
        else:
            try:
                from apm_cli.utils.console import _rich_warning

                _rich_warning(
                    f"Skill name '{raw_skill_name}' normalized to '{skill_name}' ({error_msg})"
                )
            except ImportError:
                pass  # CLI not available in tests

    # Deploy to all active targets that support skills.
    # When *targets* is provided (from --target), use it directly.
    # Otherwise auto-detect with copilot as the fallback.
    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(project_root)
    skill_created = False
    skill_updated = False
    files_copied = 0
    all_target_paths: list[Path] = []
    primary_skill_md: Path | None = None

    # Read lockfile once and derive both maps in a single pass.
    owned_by, lockfile_native_owners = self._build_ownership_maps(project_root)
    sub_skills_dir = package_path / ".apm" / "skills"

    # Full unique key of the package currently being installed.
    dep_ref = package_info.dependency_ref
    current_key: str | None = dep_ref.get_unique_key() if dep_ref is not None else None

    seen_skill_dirs: set[Path] = set()

    for idx, target in enumerate(targets):
        if not target.supports("skills"):
            continue

        is_primary = idx == 0  # first active target owns diagnostics
        skills_mapping = target.primitives["skills"]
        # Dynamic-root targets (cowork): use resolved_deploy_root.
        if target.resolved_deploy_root is not None:
            target_skill_dir = target.resolved_deploy_root / skill_name
        else:
            effective_root = skills_mapping.deploy_root or target.root_dir
            target_skill_dir = project_root / effective_root / "skills" / skill_name

        # Security: validate name + containment + symlink rejection.
        from apm_cli.utils.path_security import (
            PathTraversalError,
            ensure_path_within,
            validate_path_segments,
        )

        validate_path_segments(skill_name, context="skill name")
        if target_skill_dir.is_symlink():
            raise PathTraversalError(
                f"Skill destination {target_skill_dir} is a symlink -- refusing to deploy"
            )
        if target.resolved_deploy_root is None:
            ensure_path_within(target_skill_dir, project_root / effective_root / "skills")

        # Dedup: skip if same resolved path already deployed.
        resolved = target_skill_dir.resolve()
        if resolved in seen_skill_dirs:
            if logger:
                logger.progress(
                    f"{target_skill_dir} -- already deployed, skipping for {target.name}",
                    symbol="info",
                )
            continue
        seen_skill_dirs.add(resolved)

        if is_primary:
            skill_created = not target_skill_dir.exists()
            skill_updated = not skill_created
            primary_skill_md = target_skill_dir / "SKILL.md"

        if target_skill_dir.exists():
            if is_primary:
                # Check both the lockfile (previous runs) and the in-memory session
                # map (current run) so that same-manifest collisions are caught even
                # before the lockfile has been written for this run.
                prev_owner = lockfile_native_owners.get(
                    skill_name
                ) or self._native_skill_session_owners.get(skill_name)
                is_self_overwrite = prev_owner is not None and prev_owner == current_key
                if prev_owner is not None and not is_self_overwrite:
                    try:
                        rel_prefix = target_skill_dir.parent.relative_to(project_root).as_posix()
                    except ValueError:
                        # Dynamic-root targets (cowork): directory is
                        # outside the project tree.
                        rel_prefix = "skills"
                    rel_path = f"{rel_prefix}/{skill_name}"
                    # Issue 1: package= should identify the package causing the
                    # collision (current_key), not the skill name, so render_summary()
                    # groups diagnostics by the package responsible.
                    # Issue 2: message must tell the user what to do ("So What?" test).
                    detail = (
                        f"Skill '{skill_name}' from '{current_key}' replaced "
                        f"'{prev_owner}' -- remove one package to avoid this"
                    )
                    if diagnostics is not None:
                        diagnostics.overwrite(
                            path=rel_path,
                            package=current_key or skill_name,
                            detail=detail,
                        )
                    elif logger:
                        logger.warning(detail)
                    else:
                        # Reached when called without diagnostics or logger (e.g. uninstall sync).
                        from apm_cli.utils.console import _rich_warning

                        _rich_warning(detail)
            shutil.rmtree(target_skill_dir)

        target_skill_dir.parent.mkdir(parents=True, exist_ok=True)
        from apm_cli.security.gate import ignore_non_content

        _apm_filter = shutil.ignore_patterns(".apm")

        def _ignore_non_content_and_apm(directory, contents, apm_filter=_apm_filter):
            return list(
                set(ignore_non_content(directory, contents)) | set(apm_filter(directory, contents))
            )

        shutil.copytree(package_path, target_skill_dir, ignore=_ignore_non_content_and_apm)
        all_target_paths.append(target_skill_dir)

        if is_primary:
            files_copied = sum(1 for _ in target_skill_dir.rglob("*") if _.is_file())

        # Promote sub-skills for this target
        if target.resolved_deploy_root is not None:
            target_skills_root = target.resolved_deploy_root
        else:
            target_skills_root = project_root / effective_root / "skills"
        _, sub_deployed = self._promote_sub_skills(
            sub_skills_dir,
            target_skills_root,
            skill_name,
            warn=is_primary,
            owned_by=owned_by if is_primary else None,
            diagnostics=diagnostics if is_primary else None,
            managed_files=managed_files if is_primary else None,
            force=force,
            project_root=project_root,
            logger=logger if is_primary else None,
        )
        all_target_paths.extend(sub_deployed)

    # Record ownership in the session map so subsequent packages installed in
    # the same run can detect a collision even before the lockfile is written.
    if current_key is not None:
        self._native_skill_session_owners[skill_name] = current_key

    # Count unique sub-skills from primary target only
    primary_root = project_root / ".github" / "skills"
    sub_skills_count = sum(
        1 for p in all_target_paths if p.parent == primary_root and p.name != skill_name
    )

    return SkillIntegrationResult(
        skill_created=skill_created,
        skill_updated=skill_updated,
        skill_skipped=False,
        skill_path=primary_skill_md,
        references_copied=files_copied,
        links_resolved=0,
        sub_skills_promoted=sub_skills_count,
        target_paths=all_target_paths,
    )


def _integrate_skill_bundle(
    self,
    package_info,
    project_root: Path,
    skills_dir: Path,
    diagnostics=None,
    managed_files=None,
    force: bool = False,
    logger=None,
    targets=None,
    skill_subset=None,
) -> SkillIntegrationResult:
    """Promote every skill in a SKILL_BUNDLE's top-level skills/ directory.

    Reuses the same promotion logic as _promote_sub_skills but sources
    from package_root/skills/ instead of .apm/skills/.  Each nested
    skill directory becomes a top-level skill in every target.

    Args:
        package_info: PackageInfo with package metadata.
        project_root: Root directory of the project.
        skills_dir: The package's skills/ directory.
        diagnostics: Optional DiagnosticCollector.
        managed_files: Set of managed file paths.
        force: Whether to overwrite locally-authored files.
        logger: Optional InstallLogger.
        targets: Optional explicit list of TargetProfile objects.
        skill_subset: Optional tuple of skill names to install (None = all).

    Returns:
        SkillIntegrationResult with all promoted skills.
    """
    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(project_root)

    parent_name = package_info.install_path.name
    owned_by, lockfile_native_owners = self._build_ownership_maps(project_root)  # noqa: RUF059

    total_promoted = 0
    all_deployed: list[Path] = []
    any_created = False
    seen_skill_dirs: set[Path] = set()

    # Convert skill_subset tuple to a set for O(1) lookup
    _name_filter = set(skill_subset) if skill_subset else None

    for idx, target in enumerate(targets):
        if not target.supports("skills"):
            continue

        is_primary = idx == 0
        skills_mapping = target.primitives["skills"]
        effective_root = skills_mapping.deploy_root or target.root_dir
        target_skills_root = project_root / effective_root / "skills"

        # Dedup: skip if same resolved skills root already processed.
        resolved_root = target_skills_root.resolve()
        if resolved_root in seen_skill_dirs:
            if logger:
                logger.progress(
                    f"{target_skills_root} -- already deployed, skipping for {target.name}",
                    symbol="info",
                )
            continue
        seen_skill_dirs.add(resolved_root)

        target_skills_root.mkdir(parents=True, exist_ok=True)

        n, deployed = self._promote_sub_skills(
            skills_dir,
            target_skills_root,
            parent_name,
            warn=is_primary,
            owned_by=owned_by if is_primary else None,
            diagnostics=diagnostics if is_primary else None,
            managed_files=managed_files if is_primary else None,
            force=force,
            project_root=project_root,
            logger=logger if is_primary else None,
            name_filter=_name_filter,
        )
        if is_primary:
            total_promoted = n
            if n > 0:
                any_created = True
        all_deployed.extend(deployed)

    return SkillIntegrationResult(
        skill_created=any_created,
        skill_updated=False,
        skill_skipped=False,
        skill_path=None,
        references_copied=0,
        links_resolved=0,
        sub_skills_promoted=total_promoted,
        target_paths=all_deployed,
    )


def integrate_package_skill(
    self,
    package_info,
    project_root: Path,
    diagnostics=None,
    managed_files=None,
    force: bool = False,
    logger=None,
    targets=None,
    skill_subset=None,
) -> SkillIntegrationResult:
    """Integrate a package's skill into all active target directories.

    Copies native skills (packages with SKILL.md at root) to every active
    target that supports skills (e.g. .github/skills/, .claude/skills/,
    .opencode/skills/). Also promotes any sub-skills from .apm/skills/.

    When *targets* is provided (e.g. from ``--target cursor``), only those
    targets are considered.  Otherwise falls back to ``active_targets()``.

    Packages without SKILL.md at root are not installed as skills -- only their
    sub-skills (if any) are promoted.

    Args:
        package_info: PackageInfo object with package metadata
        project_root: Root directory of the project
        targets: Optional explicit list of TargetProfile objects.

    Returns:
        SkillIntegrationResult: Results of the integration operation
    """
    # Check if package type allows skill installation (T4 routing)
    # SKILL and HYBRID -> install as skill
    # INSTRUCTIONS and PROMPTS -> skip skill installation
    if not should_install_skill(package_info):
        # Even non-skill packages may ship sub-skills under .apm/skills/.
        # Promote them so Copilot can discover them independently.
        sub_skills_count, sub_deployed = self._promote_sub_skills_standalone(
            package_info,
            project_root,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
        )
        return SkillIntegrationResult(
            skill_created=False,
            skill_updated=False,
            skill_skipped=True,
            skill_path=None,
            references_copied=0,
            links_resolved=0,
            sub_skills_promoted=sub_skills_count,
            target_paths=sub_deployed,
        )

    # Skip virtual FILE packages - they're individual files, not full packages
    # Multiple virtual files from the same repo would collide on skill name
    # BUT: subdirectory packages (like Claude Skills) SHOULD generate skills
    if package_info.dependency_ref and package_info.dependency_ref.is_virtual:
        # Allow subdirectory packages through - they are complete skill packages
        if not package_info.dependency_ref.is_virtual_subdirectory():
            return SkillIntegrationResult(
                skill_created=False,
                skill_updated=False,
                skill_skipped=True,
                skill_path=None,
                references_copied=0,
                links_resolved=0,
            )

    package_path = package_info.install_path

    # Check if this is a native Skill (already has SKILL.md at root)
    source_skill_md = package_path / "SKILL.md"
    if source_skill_md.exists():
        if skill_subset:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(
                f"--skill filter ignored for '{package_info.install_path.name}': "
                "package is a single CLAUDE_SKILL, not a SKILL_BUNDLE."
            )
        return self._integrate_native_skill(
            package_info,
            project_root,
            source_skill_md,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
        )

    # SKILL_BUNDLE: promote skills from root-level skills/ directory.
    root_skills_dir = package_path / "skills"
    if root_skills_dir.is_dir() and any(
        (d / "SKILL.md").exists() for d in root_skills_dir.iterdir() if d.is_dir()
    ):
        return self._integrate_skill_bundle(
            package_info,
            project_root,
            root_skills_dir,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
            skill_subset=skill_subset,
        )

    # No SKILL.md at root  -- not a skill package.
    # Still promote any sub-skills shipped under .apm/skills/.
    sub_skills_count, sub_deployed = self._promote_sub_skills_standalone(
        package_info,
        project_root,
        diagnostics=diagnostics,
        managed_files=managed_files,
        force=force,
        logger=logger,
        targets=targets,
    )
    return SkillIntegrationResult(
        skill_created=False,
        skill_updated=False,
        skill_skipped=True,
        skill_path=None,
        references_copied=0,
        links_resolved=0,
        sub_skills_promoted=sub_skills_count,
        target_paths=sub_deployed,
    )
