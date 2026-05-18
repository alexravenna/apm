"""Helpers for the install pipeline command flow."""

import builtins
import sys
from dataclasses import dataclass
from typing import Any

from apm_cli.install.errors import AuthenticationError, FrozenInstallError, PolicyViolationError
from apm_cli.install.insecure_policy import InsecureDependencyPolicyError
from apm_cli.integration.mcp_integrator_install.opts import MCPInstallOpts as _MCPOpts

from ...constants import APM_YML_FILENAME, InstallMode
from ...utils.console import _rich_echo, _rich_error


@dataclass
class _MCPInstallCtx:
    """Bundled MCP install arguments for :func:`_install_mcp_dependencies`."""

    old_mcp_servers: Any
    old_mcp_configs: Any
    lock_path: Any
    apm_diagnostics: Any


def _rollback_manifest(ctx, logger) -> None:
    """Restore the manifest snapshot when install fails."""
    sys.modules[__package__]._maybe_rollback_manifest(
        ctx.snapshot_manifest_path,
        ctx.manifest_snapshot,
        logger,
    )


def _parse_install_manifest(ctx, logger):
    """Parse ``apm.yml`` and return dependency groups plus metadata."""
    try:
        apm_package = sys.modules[__package__].APMPackage.from_apm_yml(ctx.manifest_path)
    except Exception as exc:
        logger.error(f"Failed to parse {ctx.manifest_display}: {exc}")
        sys.exit(1)

    apm_deps = apm_package.get_apm_dependencies()
    dev_apm_deps = apm_package.get_dev_apm_dependencies()
    mcp_deps = apm_package.get_mcp_dependencies()
    has_any_apm_deps = bool(apm_deps) or bool(dev_apm_deps)
    logger.verbose_detail(
        f"Parsed {APM_YML_FILENAME}: {len(apm_deps)} APM deps, {len(mcp_deps)} MCP deps"
        + (f", {len(dev_apm_deps)} dev deps" if dev_apm_deps else "")
    )
    return apm_package, apm_deps, dev_apm_deps, mcp_deps, has_any_apm_deps


def _run_dry_run_preflight(
    ctx,
    logger,
    apm_deps,
    dev_apm_deps,
    mcp_deps,
    should_install_apm: bool,
    should_install_mcp: bool,
):
    """Render the dry-run preview and return the defensive tuple."""
    from apm_cli.install.presentation.dry_run import DryRunParams, render_and_exit
    from apm_cli.policy.install_preflight import run_policy_preflight as _dry_run_preflight

    _dry_run_preflight(
        project_root=ctx.project_root,
        apm_deps=builtins.list(apm_deps) + builtins.list(dev_apm_deps),
        mcp_deps=mcp_deps if should_install_mcp else None,
        no_policy=ctx.no_policy,
        logger=logger,
        dry_run=True,
    )
    render_and_exit(
        DryRunParams(
            logger=logger,
            should_install_apm=should_install_apm,
            apm_deps=apm_deps,
            mcp_deps=mcp_deps,
            dev_apm_deps=dev_apm_deps,
            should_install_mcp=should_install_mcp,
            update=ctx.update,
            only_packages=ctx.only_packages,
            apm_dir=ctx.apm_dir,
        )
    )
    return 0, 0, None


def _capture_existing_mcp_state(apm_dir):
    """Read the current lockfile so MCP state can be restored or updated."""
    sys.modules[__package__].migrate_lockfile_if_needed(apm_dir)
    lock_path = sys.modules[__package__].get_lockfile_path(apm_dir)
    existing_lock = sys.modules[__package__].LockFile.read(lock_path)
    old_mcp_servers = builtins.set(existing_lock.mcp_servers) if existing_lock else builtins.set()
    old_mcp_configs = builtins.dict(existing_lock.mcp_configs) if existing_lock else {}
    return lock_path, existing_lock, old_mcp_servers, old_mcp_configs


def _has_orphaned_lock_deps(ctx, has_any_apm_deps: bool, existing_lock) -> bool:
    """Return whether install should enter the APM path to clean orphaned deps."""
    from apm_cli.core.scope import get_deploy_root as _get_deploy_root
    from apm_cli.deps.lockfile import _SELF_KEY as _LOCK_SELF_KEY

    _cli_project_root = _get_deploy_root(ctx.scope)
    return bool(
        existing_lock
        and not has_any_apm_deps
        and any(key != _LOCK_SELF_KEY for key in existing_lock.dependencies)
        and (sys.modules[__package__]._project_has_root_primitives(_cli_project_root) or True)
    )


def _run_apm_install(
    ctx,
    outcome,
    logger,
    apm_package,
    has_any_apm_deps: bool,
    should_install_apm: bool,
    existing_lock,
):
    """Run the APM dependency installer and handle rollback-aware errors."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.scope import get_deploy_root as _get_deploy_root
    from apm_cli.deps.lockfile import _SELF_KEY as _LOCK_SELF_KEY

    cli_project_root = _get_deploy_root(ctx.scope)
    has_orphan_deps_in_lock = bool(
        existing_lock
        and not has_any_apm_deps
        and any(key != _LOCK_SELF_KEY for key in existing_lock.dependencies)
    )
    should_enter = should_install_apm and (
        has_any_apm_deps
        or sys.modules[__package__]._project_has_root_primitives(cli_project_root)
        or has_orphan_deps_in_lock
    )
    if not should_enter:
        if should_install_apm and not has_any_apm_deps:
            logger.verbose_detail("No APM dependencies found in apm.yml")
        return 0, None

    if not sys.modules[__package__].APM_DEPS_AVAILABLE:
        logger.error("APM dependency system not available")
        logger.progress(f"Import error: {sys.modules[__package__]._APM_IMPORT_ERROR}")
        sys.exit(1)

    try:
        install_result = sys.modules[__package__]._install_apm_dependencies(
            apm_package,
            update_refs=ctx.update,
            verbose=ctx.verbose,
            only_packages=ctx.only_packages,
            force=ctx.force,
            parallel_downloads=ctx.parallel_downloads,
            logger=logger,
            scope=ctx.scope,
            auth_resolver=ctx.auth_resolver,
            target=ctx.target,
            allow_insecure=ctx.allow_insecure,
            allow_insecure_hosts=ctx.allow_insecure_hosts,
            marketplace_provenance=(
                outcome.marketplace_provenance if ctx.packages and outcome else None
            ),
            protocol_pref=ctx.protocol_pref,
            allow_protocol_fallback=ctx.allow_protocol_fallback,
            no_policy=ctx.no_policy,
            legacy_skill_paths=ctx.legacy_skill_paths,
            frozen=ctx.frozen,
            plan_callback=ctx.plan_callback,
        )
        return install_result.installed_count, install_result.diagnostics
    except InsecureDependencyPolicyError:
        _rollback_manifest(ctx, logger)
        sys.exit(1)
    except AuthenticationError as exc:
        _rollback_manifest(ctx, logger)
        _rich_error(str(exc))
        if exc.diagnostic_context:
            _rich_echo(exc.diagnostic_context)
        sys.exit(1)
    except FrozenInstallError as exc:
        _rollback_manifest(ctx, logger)
        _rich_error(str(exc))
        for reason in exc.reasons:
            _rich_echo(reason)
        sys.exit(1)
    except Exception as exc:
        _rollback_manifest(ctx, logger)
        message = (
            str(exc)
            if isinstance(exc, PolicyViolationError)
            else f"Failed to install APM dependencies: {exc}"
        )
        logger.error(message)
        if not ctx.verbose:
            logger.progress("Run with --verbose for detailed diagnostics")
        sys.exit(1)


def _collect_transitive_mcp(ctx, logger, apm_diagnostics, mcp_deps, should_install_mcp: bool):
    """Collect transitive MCP dependencies from installed APM packages."""
    from ...core.scope import get_modules_dir

    if not should_install_mcp:
        return mcp_deps

    apm_modules_path = get_modules_dir(ctx.scope)
    if not apm_modules_path.exists():
        return mcp_deps

    lock_path = sys.modules[__package__].get_lockfile_path(ctx.apm_dir)
    transitive_mcp = sys.modules[__package__].MCPIntegrator.collect_transitive(
        apm_modules_path,
        lock_path,
        ctx.trust_transitive_mcp,
        diagnostics=apm_diagnostics,
    )
    if not transitive_mcp:
        return mcp_deps

    logger.verbose_detail(f"Collected {len(transitive_mcp)} transitive MCP dependency(ies)")
    return sys.modules[__package__].MCPIntegrator.deduplicate(mcp_deps + transitive_mcp)


def _preflight_transitive_mcp(ctx, logger, should_install_mcp: bool, mcp_deps) -> None:
    """Run the second policy preflight over merged MCP dependencies."""
    if not (should_install_mcp and mcp_deps):
        return

    from apm_cli.policy.install_preflight import PolicyBlockError as _TransitivePBE
    from apm_cli.policy.install_preflight import run_policy_preflight as _transitive_preflight

    try:
        _transitive_preflight(
            project_root=ctx.project_root,
            mcp_deps=mcp_deps,
            no_policy=ctx.no_policy,
            logger=logger,
            dry_run=False,
        )
    except _TransitivePBE:
        logger.error(
            "MCP server(s) blocked by org policy. APM packages remain installed; "
            "MCP configs were NOT written."
        )
        logger.render_summary()
        sys.exit(1)


def _build_mcp_apm_config(apm_package) -> dict:
    """Build the APM config subset passed to MCP integration."""
    apm_config: dict = {"scripts": apm_package.scripts or {}}
    if apm_package.targets is not None:
        apm_config["targets"] = apm_package.targets
    elif apm_package.target is not None:
        apm_config["target"] = apm_package.target
    return apm_config


def _install_mcp_dependencies(
    ctx,
    logger,
    apm_package,
    mcp_deps,
    should_install_mcp: bool,
    mcp_ctx: _MCPInstallCtx,
):
    """Install, prune, or restore MCP servers based on the selected mode."""
    from ...core.scope import InstallScope

    old_mcp_servers = mcp_ctx.old_mcp_servers
    old_mcp_configs = mcp_ctx.old_mcp_configs
    lock_path = mcp_ctx.lock_path
    apm_diagnostics = mcp_ctx.apm_diagnostics

    if should_install_mcp and mcp_deps:
        mcp_count = sys.modules[__package__].MCPIntegrator.install(
            mcp_deps,
            _MCPOpts(
                runtime=ctx.runtime,
                exclude=ctx.exclude,
                verbose=ctx.verbose,
                stored_mcp_configs=old_mcp_configs,
                apm_config=_build_mcp_apm_config(apm_package),
                project_root=ctx.project_root,
                user_scope=(ctx.scope is InstallScope.USER),
                explicit_target=ctx.target,
                diagnostics=apm_diagnostics,
                scope=ctx.scope,
            ),
        )
        new_mcp_servers = sys.modules[__package__].MCPIntegrator.get_server_names(mcp_deps)
        new_mcp_configs = sys.modules[__package__].MCPIntegrator.get_server_configs(mcp_deps)
        stale_servers = old_mcp_servers - new_mcp_servers
        if stale_servers:
            sys.modules[__package__].MCPIntegrator.remove_stale(
                stale_servers,
                ctx.runtime,
                ctx.exclude,
                project_root=ctx.project_root,
                user_scope=(ctx.scope is InstallScope.USER),
                scope=ctx.scope,
            )
        sys.modules[__package__].MCPIntegrator.update_lockfile(
            new_mcp_servers,
            lock_path,
            mcp_configs=new_mcp_configs,
        )
        return mcp_count

    if should_install_mcp and not mcp_deps:
        if old_mcp_servers:
            sys.modules[__package__].MCPIntegrator.remove_stale(
                old_mcp_servers,
                ctx.runtime,
                ctx.exclude,
                project_root=ctx.project_root,
                user_scope=(ctx.scope is InstallScope.USER),
                scope=ctx.scope,
            )
            sys.modules[__package__].MCPIntegrator.update_lockfile(
                builtins.set(),
                lock_path,
                mcp_configs={},
            )
        logger.verbose_detail("No MCP dependencies found in apm.yml")
        return 0

    if old_mcp_servers:
        sys.modules[__package__].MCPIntegrator.update_lockfile(
            old_mcp_servers,
            lock_path,
            mcp_configs=old_mcp_configs,
        )
    return 0
