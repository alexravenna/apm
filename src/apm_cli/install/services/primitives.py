"""Primitive dispatch pipeline for a single package.

Provides ``integrate_package_primitives`` and its private helpers.
The public symbol is re-exported from ``apm_cli.install.services`` so
all existing import paths and ``mock.patch`` seams continue to work.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.integration.skill_integrator.opts import SkillOpts as _SkillOpts

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.context import InstallContext
    from apm_cli.utils.diagnostics import DiagnosticCollector

from .deployed_path import _deployed_path_entry

# Shadow builtins defensively: see ``__init__`` module-level comment.
set = builtins.set
list = builtins.list
dict = builtins.dict


# ---------------------------------------------------------------------------
# Module-level helpers (no closure dependencies)
# ---------------------------------------------------------------------------


def _format_target_collapse(paths: list[str], verbose: bool) -> tuple[str, list[str]]:
    """Apply the 1/2/3+ multi-target collapse rule.

    Returns a tuple ``(suffix, expansion_lines)``:

    * ``suffix`` -- text appended after ``-> `` on the aggregate line.
    * ``expansion_lines`` -- extra ``  |     -> <path>`` lines emitted
      AFTER the aggregate line when ``verbose`` is True; empty otherwise.

    Rule:
      1 target  -> ``<path1>``
      2 targets -> ``<path1>, <path2>``
      3+        -> ``N targets`` (verbose forces full enumeration)
    """
    deduped: list[str] = []
    seen: set = builtins.set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    if verbose and len(deduped) >= 2:
        return "", [f"  |     -> {p}" for p in deduped]
    if len(deduped) == 0:
        return "", []
    if len(deduped) == 1:
        return deduped[0], []
    if len(deduped) == 2:
        return f"{deduped[0]}, {deduped[1]}", []
    return f"{len(deduped)} targets", []


def _maybe_emit_cowork_warning(
    package_info: Any,
    package_name: str,
    targets: Any,
    ctx: InstallContext | None,
    logger: InstallLogger | None,
    diagnostics: DiagnosticCollector,
) -> None:
    """Emit the cowork non-skill primitive warning once per run (Amendment 6).

    No-ops when the cowork target is not active, *ctx* is absent, or
    the warning has already been emitted for this install session.
    """
    _cowork_active = any(t.name == "copilot-cowork" for t in targets)
    if not (_cowork_active and ctx is not None and not ctx.cowork_nonsupported_warned):
        return

    _apm_dir = Path(package_info.install_path) / ".apm"
    _NON_SKILL_DIRS = {
        "agents": "agents",
        "prompts": "prompts",
        "instructions": "instructions",
        "hooks": "hooks",
    }
    _found_types = [
        ptype
        for ptype, subdir in _NON_SKILL_DIRS.items()
        if (_apm_dir / subdir).is_dir() and any((_apm_dir / subdir).iterdir())
    ]
    if not _found_types:
        return

    _pkg_label = package_name or getattr(package_info, "name", "unknown")
    _types_str = ", ".join(sorted(builtins.set(_found_types)))
    _warn_msg = (
        f"copilot-cowork target only supports skills; "
        f"non-skill primitives in {_pkg_label} "
        f"({_types_str}) will not deploy to cowork"
    )
    if logger:
        logger.warning(_warn_msg, symbol="warning")
    diagnostics.warn(_warn_msg)
    ctx.cowork_nonsupported_warned = True


@dataclass
class _DispatchCtx:
    """Bundled arguments for :func:`_dispatch_non_skill_primitives`."""

    dispatch: Any
    integrator_kwargs: dict
    targets: Any
    package_info: Any
    project_root: Path
    force: bool
    managed_files: Any
    diagnostics: Any
    deployed: list
    result: dict
    log_fn: Any
    verbose: bool


@dataclass
class _SkillLogCtx:
    """Bundled arguments for :func:`_collect_and_log_skills`."""

    skill_integrator: Any
    package_info: Any
    project_root: Path
    diagnostics: Any
    managed_files: Any
    force: bool
    targets: Any
    skill_subset: Any
    result: dict
    deployed: list
    log_fn: Any
    verbose: bool


def _dispatch_non_skill_primitives(ctx: _DispatchCtx) -> None:
    """Dispatch all non-skill primitives across targets; emit aggregate log lines.

    Mutates *result* counters and *deployed* paths in-place.
    Skills are skipped here (``entry.multi_target`` flag) and handled by
    ``_collect_and_log_skills``.
    """
    dispatch = ctx.dispatch
    integrator_kwargs = ctx.integrator_kwargs
    targets = ctx.targets
    package_info = ctx.package_info
    project_root = ctx.project_root
    force = ctx.force
    managed_files = ctx.managed_files
    diagnostics = ctx.diagnostics
    deployed = ctx.deployed
    result = ctx.result
    log_fn = ctx.log_fn
    verbose = ctx.verbose
    _per_kind: dict[str, dict[str, Any]] = {}

    for _prim_name, _entry in dispatch.items():
        if _entry.multi_target:
            continue  # skills handled separately

        _integrator = integrator_kwargs[_prim_name]
        _agg_files = 0
        _agg_adopted = 0
        _agg_paths: list[str] = []
        _label = _prim_name

        for _target in targets:
            _mapping = _target.primitives.get(_prim_name)
            if _mapping is None:
                continue
            _int_result = getattr(_integrator, _entry.integrate_method)(
                _target,
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            )
            result["links_resolved"] += _int_result.links_resolved
            for tp in _int_result.target_paths:
                deployed.append(_deployed_path_entry(tp, project_root, targets))

            _adopted_attr = getattr(_int_result, "files_adopted", 0)
            # Coerce defensively: MagicMock auto-attributes may not be ints.
            _adopted = _adopted_attr if isinstance(_adopted_attr, int) else 0

            if _int_result.files_integrated <= 0 and _adopted <= 0:
                continue

            _agg_files += _int_result.files_integrated
            _agg_adopted += _adopted
            result[_entry.counter_key] += _int_result.files_integrated
            _effective_root = _mapping.deploy_root or _target.root_dir
            _deploy_dir = (
                f"{_effective_root}/{_mapping.subdir}/"
                if _mapping.subdir
                else f"{_effective_root}/"
            )
            if _prim_name == "instructions" and _mapping.format_id in (
                "cursor_rules",
                "claude_rules",
            ):
                _label = "rule(s)"
            elif _prim_name == "instructions":
                _label = "instruction(s)"
            elif _prim_name == "hooks":
                if _target.hooks_config_display:
                    _deploy_dir = _target.hooks_config_display
                _label = "hook(s)"
            else:
                _label = _prim_name
            _agg_paths.append(_deploy_dir)

        if _agg_files > 0 or _agg_adopted > 0:
            _per_kind[_prim_name] = {
                "files": _agg_files,
                "adopted": _agg_adopted,
                "label": _label,
                "paths": _agg_paths,
            }

    # Emit aggregated per-kind lines in dispatch order so output is stable.
    for _prim_name in dispatch:
        if _prim_name not in _per_kind:
            continue
        _info = _per_kind[_prim_name]
        _suffix, _expansion = _format_target_collapse(_info["paths"], verbose)
        _files = _info["files"]
        _adopted = _info["adopted"]
        if _files > 0:
            _verb_phrase = f"{_files} {_info['label']} integrated"
            if _adopted > 0:
                _verb_phrase = f"{_verb_phrase} ({_adopted} adopted)"
        else:
            _verb_phrase = f"{_adopted} {_info['label']} adopted"
        if _expansion:
            log_fn(f"  |-- {_verb_phrase}:")
            for line in _expansion:
                log_fn(line)
        else:
            log_fn(f"  |-- {_verb_phrase} -> {_suffix}")


def _collect_and_log_skills(ctx: _SkillLogCtx) -> None:
    """Run skill integration, update result/deployed, and emit log lines.

    Mutates *result* and *deployed* in-place.
    """
    skill_integrator = ctx.skill_integrator
    package_info = ctx.package_info
    project_root = ctx.project_root
    diagnostics = ctx.diagnostics
    managed_files = ctx.managed_files
    force = ctx.force
    targets = ctx.targets
    skill_subset = ctx.skill_subset
    result = ctx.result
    deployed = ctx.deployed
    log_fn = ctx.log_fn
    verbose = ctx.verbose
    skill_result = skill_integrator.integrate_package_skill(
        package_info,
        project_root,
        diagnostics=diagnostics,
        managed_files=managed_files,
        force=force,
        targets=targets,
        skill_subset=skill_subset,
    )
    _skill_target_dirs: set = builtins.set()
    for tp in skill_result.target_paths:
        try:
            rel = tp.relative_to(project_root)
            if rel.parts:
                _skill_target_dirs.add(rel.parts[0])
        except ValueError:
            # Dynamic-root target (copilot-cowork) -- path outside project tree.
            _skill_target_dirs.add("copilot-cowork")

    _skill_target_paths = [f"{d}/skills/" for d in sorted(_skill_target_dirs)]
    if not _skill_target_paths:
        _skill_target_paths = ["skills/"]
    _skill_suffix, _skill_expansion = _format_target_collapse(_skill_target_paths, verbose)

    if skill_result.skill_created:
        result["skills"] += 1
        if _skill_expansion:
            log_fn("  |-- Skill integrated:")
            for line in _skill_expansion:
                log_fn(line)
        else:
            log_fn(f"  |-- Skill integrated -> {_skill_suffix}")

    if skill_result.sub_skills_promoted > 0:
        result["sub_skills"] += skill_result.sub_skills_promoted
        if _skill_expansion:
            log_fn(f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated:")
            for line in _skill_expansion:
                log_fn(line)
        else:
            log_fn(
                f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated -> {_skill_suffix}"
            )

    for tp in skill_result.target_paths:
        deployed.append(_deployed_path_entry(tp, project_root, targets))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def integrate_package_primitives(
    package_info: Any,
    project_root: Path,
    *,
    targets: Any,
    prompt_integrator: Any,
    agent_integrator: Any,
    skill_integrator: Any,
    instruction_integrator: Any,
    **kwargs,
) -> dict:
    """Run the full integration pipeline for a single package.

    Iterates over *targets* (``TargetProfile`` list) and dispatches each
    primitive to the appropriate integrator via the target-driven API.
    Skills are handled separately because ``SkillIntegrator`` already
    routes across all targets internally.

    When *scope* is ``InstallScope.USER``, targets and primitives that
    do not support user-scope deployment are silently skipped.

    When *ctx* is provided, the cowork non-skill primitive warning
    (Amendment 6) is emitted once per install run for packages that
    contain non-skill primitives when the cowork target is active.

    Returns a dict with integration counters and the list of deployed file paths.
    """
    command_integrator: Any = kwargs.get("command_integrator")
    hook_integrator: Any = kwargs.get("hook_integrator")
    force: bool = kwargs.get("force", False)
    managed_files: Any = kwargs.get("managed_files")
    diagnostics: DiagnosticCollector = kwargs.get("diagnostics")
    package_name: str = kwargs.get("package_name", "")
    logger: InstallLogger | None = kwargs.get("logger")
    skill_subset: tuple | None = kwargs.get("skill_subset")
    ctx: InstallContext | None = kwargs.get("ctx")
    scratch_root: Path | None = kwargs.get("scratch_root")
    from apm_cli.integration.dispatch import get_dispatch_table

    _dispatch = get_dispatch_table()
    result = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "sub_skills": 0,
        "instructions": 0,
        "commands": 0,
        "hooks": 0,
        "links_resolved": 0,
        "deployed_files": [],
    }
    deployed = result["deployed_files"]

    if not targets:
        return result

    # Drift-replay safety guard (#drift): assert project_root is within
    # scratch_root when the caller redirects integration to an isolated dir.
    if scratch_root is not None:
        from apm_cli.utils.path_security import ensure_path_within

        scratch_root = Path(scratch_root).resolve()
        ensure_path_within(Path(project_root).resolve(), scratch_root)

    _maybe_emit_cowork_warning(package_info, package_name, targets, ctx, logger, diagnostics)

    def _log_integration(msg: str) -> None:
        if logger:
            logger.tree_item(msg)

    _verbose = bool(getattr(ctx, "verbose", False)) if ctx is not None else False

    _INTEGRATOR_KWARGS: dict[str, Any] = {
        "prompts": prompt_integrator,
        "agents": agent_integrator,
        "commands": command_integrator,
        "instructions": instruction_integrator,
        "hooks": hook_integrator,
        "skills": skill_integrator,
    }

    _dispatch_non_skill_primitives(
        _DispatchCtx(
            dispatch=_dispatch,
            integrator_kwargs=_INTEGRATOR_KWARGS,
            targets=targets,
            package_info=package_info,
            project_root=project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            deployed=deployed,
            result=result,
            log_fn=_log_integration,
            verbose=_verbose,
        )
    )

    _collect_and_log_skills(
        _SkillLogCtx(
            skill_integrator=skill_integrator,
            package_info=package_info,
            project_root=project_root,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            targets=targets,
            skill_subset=skill_subset,
            result=result,
            deployed=deployed,
            log_fn=_log_integration,
            verbose=_verbose,
        )
    )

    _total_integrated = (
        result["prompts"]
        + result["agents"]
        + result["commands"]
        + result["instructions"]
        + result["hooks"]
        + result["skills"]
        + result["sub_skills"]
    )
    if _total_integrated == 0:
        _log_integration("  |-- (files unchanged)")

    return result


__all__ = ["integrate_package_primitives"]
