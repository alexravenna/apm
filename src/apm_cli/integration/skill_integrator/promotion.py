"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import shutil
from pathlib import Path

from .class_ import SkillIntegrator
from .naming import normalize_skill_name, validate_skill_name
from .opts import SkillPromoteOpts


def _promote_sub_skills(
    sub_skills_dir: Path,
    target_skills_root: Path,
    parent_name: str,
    opts: SkillPromoteOpts | None = None,
) -> tuple[int, list[Path]]:
    """Promote sub-skills from .apm/skills/ to top-level skill entries.

    Args:
        sub_skills_dir: Path to the .apm/skills/ directory in the source package.
        target_skills_root: Root skills directory (e.g. .github/skills/ or .claude/skills/).
        parent_name: Name of the parent skill (used in warning messages).
        opts: Optional :class:`SkillPromoteOpts` controlling warn, force, diagnostics, etc.

    Returns:
        tuple[int, list[Path]]: (count of promoted sub-skills, list of deployed dir paths)
    """
    _opts = opts or SkillPromoteOpts()
    warn = _opts.warn
    owned_by = _opts.owned_by
    diagnostics = _opts.diagnostics
    managed_files = _opts.managed_files
    force = _opts.force
    project_root = _opts.project_root
    logger = _opts.logger
    name_filter = _opts.name_filter
    promoted = 0
    deployed = []
    if not sub_skills_dir.is_dir():
        return promoted, deployed

    # Compute project-relative prefix for consistent path reporting
    if project_root is not None:
        try:
            rel_prefix = target_skills_root.relative_to(project_root).as_posix()
        except ValueError:
            # Dynamic-root targets (cowork): use synthetic prefix
            # when the skills root lives outside the project tree.
            rel_prefix = target_skills_root.name
    else:
        rel_prefix = target_skills_root.name

    for sub_skill_path in sub_skills_dir.iterdir():
        if not sub_skill_path.is_dir():
            continue
        if not (sub_skill_path / "SKILL.md").exists():
            continue
        raw_sub_name = sub_skill_path.name
        # --skill filter: skip skills not in the requested subset
        if name_filter is not None and raw_sub_name not in name_filter:
            continue
        is_valid, _ = validate_skill_name(raw_sub_name)
        sub_name = raw_sub_name if is_valid else normalize_skill_name(raw_sub_name)
        target = target_skills_root / sub_name
        rel_path = f"{rel_prefix}/{sub_name}"
        if target.exists():
            # Content-identical → skip entirely (no copy, no warning)
            if SkillIntegrator._dirs_equal(sub_skill_path, target):
                promoted += 1
                deployed.append(target)
                continue

            # Check if this is a user-authored skill (not managed by APM)
            is_managed = managed_files is not None and rel_path.replace("\\", "/") in managed_files
            prev_owner = (owned_by or {}).get(sub_name)
            is_self_overwrite = prev_owner is not None and prev_owner == parent_name

            if managed_files is not None and not is_managed and not is_self_overwrite:
                # User-authored skill — respect force flag
                if not force:
                    if diagnostics is not None:
                        diagnostics.skip(rel_path, package=parent_name)
                    elif logger:
                        logger.warning(
                            f"Skipping skill '{sub_name}' -- local skill exists (not managed by APM). "
                            f"Use 'apm install --force' to overwrite."
                        )
                    else:
                        try:
                            from apm_cli.utils.console import _rich_warning

                            _rich_warning(
                                f"Skipping skill '{sub_name}' -- local skill exists (not managed by APM). "
                                f"Use 'apm install --force' to overwrite."
                            )
                        except ImportError:
                            pass
                    continue  # SKIP — protect user content

            if warn and not is_self_overwrite:
                if diagnostics is not None:
                    diagnostics.overwrite(
                        path=rel_path,
                        package=parent_name,
                        detail=f"Skill '{sub_name}' replaced -- previously from another package",
                    )
                elif logger:
                    logger.warning(
                        f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {rel_path}"
                    )
                else:
                    try:
                        from apm_cli.utils.console import _rich_warning

                        _rich_warning(
                            f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {rel_path}"
                        )
                    except ImportError:
                        pass
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        from apm_cli.security.gate import ignore_non_content

        shutil.copytree(sub_skill_path, target, dirs_exist_ok=True, ignore=ignore_non_content)
        promoted += 1
        deployed.append(target)
    return promoted, deployed


def _promote_sub_skills_standalone(
    self,
    package_info,
    project_root: Path,
    diagnostics=None,
    managed_files=None,
    force: bool = False,
    logger=None,
    targets=None,
) -> tuple[int, list[Path]]:
    """Promote sub-skills from a package that is NOT itself a skill.

    Packages typed as INSTRUCTIONS may still ship sub-skills under
    ``.apm/skills/``.  This method promotes them to all active targets
    that support skills, without creating a top-level skill entry for
    the parent package.

    Args:
        package_info: PackageInfo object with package metadata.
        project_root: Root directory of the project.
        targets: Optional explicit list of TargetProfile objects.

    Returns:
        tuple[int, list[Path]]: (count of promoted sub-skills, list of deployed dirs)
    """
    package_path = package_info.install_path
    sub_skills_dir = package_path / ".apm" / "skills"
    if not sub_skills_dir.is_dir():
        return 0, []

    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(project_root)

    parent_name = package_path.name
    owned_by = self._build_skill_ownership_map(project_root)
    count = 0
    all_deployed: list[Path] = []
    seen_skill_dirs: set[Path] = set()

    for idx, target in enumerate(targets):
        if not target.supports("skills"):
            continue

        is_primary = idx == 0  # first active target owns diagnostics
        skills_mapping = target.primitives["skills"]
        # Dynamic-root targets (cowork): use resolved_deploy_root.
        if target.resolved_deploy_root is not None:
            target_skills_root = target.resolved_deploy_root
        else:
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
            sub_skills_dir,
            target_skills_root,
            parent_name,
            SkillPromoteOpts(
                warn=is_primary,
                owned_by=owned_by if is_primary else None,
                diagnostics=diagnostics if is_primary else None,
                managed_files=managed_files if is_primary else None,
                force=force,
                project_root=project_root,
            ),
        )
        if is_primary:
            count = n
        all_deployed.extend(deployed)

    return count, all_deployed
