from __future__ import annotations

from ._command_context import InstallContext  # noqa: F401
from .argv_split import _get_invocation_argv, _split_argv_at_double_dash  # noqa: F401
from .cli import install  # noqa: F401
from .flags import InstallDependencyParams, MCPInvokeParams  # noqa: F401
from .manifest_ops import (  # noqa: F401
    _check_insecure_dependencies,
    _check_package_conflicts,
    _hash_deployed,
    _maybe_rollback_manifest,
    _merge_packages_into_yml,
    _resolve_package_references,
    _restore_manifest_from_snapshot,
    _validate_and_add_packages_to_apm_yml,
)
from .mcp_flow import _handle_mcp_install  # noqa: F401
from .pipeline import _install_apm_dependencies, _install_apm_packages  # noqa: F401
from .scanning import _pre_deploy_security_scan  # noqa: F401
from .summary import _post_install_summary  # noqa: F401

CommandInstallContext = InstallContext

from .manifest_ops import (  # noqa: F401
    APM_DEPS_AVAILABLE,
    APMPackage,
    DependencyReference,
    DiagnosticCollector,
    InstallLogger,
    LockFile,
    MCPIntegrator,
    Path,
    _add_mcp_to_apm_yml,
    _allow_insecure_host_callback,
    _build_mcp_entry,
    _collect_insecure_dependency_infos,
    _copy_local_package,
    _format_insecure_dependency_warning,
    _get_insecure_dependency_url,
    _guard_transitive_insecure_dependencies,
    _has_local_apm_content,
    _InsecureDependencyInfo,
    _integrate_local_content,
    _integrate_package_primitives,
    _local_path_failure_reason,
    _local_path_no_markers_hint,
    _project_has_root_primitives,
    _rich_success,
    _try_resolve_gitlab_direct_shorthand,
    _validate_package_exists,
    get_lockfile_path,
    migrate_lockfile_if_needed,
)
from .mcp_flow import _run_mcp_install  # noqa: F401
from .pipeline import _APM_IMPORT_ERROR  # noqa: F401

# Re-export contract for ruff --ignore-noqa.
__all__ = [
    "APM_DEPS_AVAILABLE",
    "_APM_IMPORT_ERROR",
    "APMPackage",
    "DependencyReference",
    "DiagnosticCollector",
    "InstallDependencyParams",
    "InstallLogger",
    "LockFile",
    "MCPIntegrator",
    "MCPInvokeParams",
    "Path",
    "_InsecureDependencyInfo",
    "_add_mcp_to_apm_yml",
    "_allow_insecure_host_callback",
    "_build_mcp_entry",
    "_check_insecure_dependencies",
    "_check_package_conflicts",
    "_collect_insecure_dependency_infos",
    "_copy_local_package",
    "_format_insecure_dependency_warning",
    "_get_insecure_dependency_url",
    "_get_invocation_argv",
    "_guard_transitive_insecure_dependencies",
    "_handle_mcp_install",
    "_has_local_apm_content",
    "_hash_deployed",
    "_install_apm_dependencies",
    "_install_apm_packages",
    "_integrate_local_content",
    "_integrate_package_primitives",
    "_local_path_failure_reason",
    "_local_path_no_markers_hint",
    "_maybe_rollback_manifest",
    "_merge_packages_into_yml",
    "_post_install_summary",
    "_pre_deploy_security_scan",
    "_project_has_root_primitives",
    "_resolve_package_references",
    "_restore_manifest_from_snapshot",
    "_rich_success",
    "_run_mcp_install",
    "_split_argv_at_double_dash",
    "_try_resolve_gitlab_direct_shorthand",
    "_validate_and_add_packages_to_apm_yml",
    "_validate_package_exists",
    "get_lockfile_path",
    "install",
    "migrate_lockfile_if_needed",
]
