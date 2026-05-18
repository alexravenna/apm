"""Root-project primitive integration helper.

Extracted from ``integrate.py`` to keep that module under 500 lines.
All names are re-exported via ``integrate.py`` so existing import paths
remain unchanged.
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

from apm_cli.install.services import integrate_local_content

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _integrate_root_project(
    ctx: InstallContext,
) -> dict[str, int] | None:
    """Integrate root project's own .apm/ primitives (#714).

    Users should not need a dummy "./agent/apm.yml" stub to get their
    root-level .apm/ rules deployed alongside external dependencies.
    Treat the project root as an implicit local package: any primitives
    found in <project_root>/.apm/ are integrated after all declared
    dependency packages have been processed.

    Delegates to ``integrate_local_content`` which creates a
    synthetic ``_local`` APMPackage with ``PackageType.APM_PACKAGE`` so that
    a root-level ``SKILL.md`` is NOT deployed as a skill.  Deployed files
    are tracked on ``ctx.local_deployed_files`` for the downstream
    post-deps-local phase (stale cleanup + lockfile persistence).

    Returns a counter-delta dict, or ``None`` if root integration is
    not applicable or failed.
    """
    if not ctx.root_has_local_primitives or not ctx.targets:
        return None

    from apm_cli.integration.base_integrator import BaseIntegrator

    logger = ctx.logger
    diagnostics = ctx.diagnostics

    # Track error count before local integration so the post-deps-local
    # phase can decide whether stale cleanup is safe.
    ctx.local_content_errors_before = diagnostics.error_count if diagnostics else 0

    # Build managed_files that includes old local deployed files AND
    # freshly-deployed dep files so local content wins collisions with
    # both.  This matches the pre-refactor Click handler behavior where
    # managed_files was rebuilt from the post-install lockfile.
    _local_managed = builtins.set(ctx.managed_files)
    _local_managed.update(ctx.old_local_deployed)
    for _dep_files in ctx.package_deployed_files.values():
        _local_managed.update(_dep_files)
    _local_managed = BaseIntegrator.normalize_managed_files(_local_managed)

    if logger:
        logger.download_complete("<project root>", ref_suffix="local")
        logger.verbose_detail("Integrating local .apm/ content...")
    try:
        _root_result = integrate_local_content(
            ctx.project_root,
            targets=ctx.targets,
            prompt_integrator=ctx.integrators["prompt"],
            agent_integrator=ctx.integrators["agent"],
            skill_integrator=ctx.integrators["skill"],
            instruction_integrator=ctx.integrators["instruction"],
            command_integrator=ctx.integrators["command"],
            hook_integrator=ctx.integrators["hook"],
            force=ctx.force,
            managed_files=_local_managed,
            diagnostics=diagnostics,
            logger=logger,
            scope=ctx.scope,
            ctx=ctx,
        )

        # Track deployed files for the post-deps-local phase (stale
        # cleanup + lockfile persistence of local_deployed_files).
        ctx.local_deployed_files = _root_result.get("deployed_files", [])

        _local_total = sum(
            _root_result.get(k, 0)
            for k in (
                "prompts",
                "agents",
                "skills",
                "sub_skills",
                "instructions",
                "commands",
                "hooks",
            )
        )
        if _local_total > 0 and logger:
            logger.verbose_detail(f"Deployed {_local_total} local primitive(s) from .apm/")

        return {
            "installed": 1,
            "prompts": _root_result["prompts"],
            "agents": _root_result["agents"],
            "skills": _root_result.get("skills", 0),
            "sub_skills": _root_result.get("sub_skills", 0),
            "instructions": _root_result["instructions"],
            "commands": _root_result["commands"],
            "hooks": _root_result["hooks"],
            "links_resolved": _root_result["links_resolved"],
        }
    except Exception as e:
        import traceback as _tb

        diagnostics.error(
            f"Failed to integrate root project primitives: {e}",
            package="<root>",
            detail=_tb.format_exc(),
        )
        # When root integration is the *only* action (no external deps),
        # a failure means nothing was deployed -- surface it clearly.
        if not ctx.all_apm_deps and logger:
            logger.error(f"Root project primitives could not be integrated: {e}")
        return None
