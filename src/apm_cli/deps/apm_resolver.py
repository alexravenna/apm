"""APM dependency resolution engine with recursive resolution and conflict detection."""

import inspect
import logging
import os
import threading
from pathlib import Path
from typing import Optional, Protocol

from ..models.apm_package import APMPackage, DependencyReference
from .dependency_graph import (
    CircularRef,
    DependencyGraph,
    DependencyTree,
    FlatDependencyMap,
)

_logger = logging.getLogger(__name__)


# Default worker pool size for the level-batched BFS download phase.
# Parallel resolution is the CENTRAL execution model (uv-inspired);
# the ``APM_RESOLVE_PARALLEL`` env var exists solely as a diagnostic /
# parity-testing knob (e.g. ``APM_RESOLVE_PARALLEL=1 apm install`` to
# reproduce legacy sequential ordering for diff-debugging).  It is NOT
# a user-facing feature toggle.
_DEFAULT_RESOLVE_PARALLEL = 4


# Type alias for the download callback.
# Takes (dep_ref, apm_modules_dir, parent_chain, parent_pkg) and returns the
# install path if successful. ``parent_chain`` is a human-readable breadcrumb
# string like "root-pkg > mid-pkg > this-pkg" showing the full dependency
# path including the current node, or just the node's display name for
# direct (depth-1) deps. ``parent_pkg`` is the APMPackage that declared this
# dependency (None for direct deps from the root); callers use its
# ``source_path`` to anchor relative ``local_path`` resolution (#857).
#
# Note: NOT @runtime_checkable -- we use signature inspection
# (``_signature_accepts_parent_pkg``) to detect legacy callbacks, never
# isinstance, so the runtime-checkable overhead would be dead weight.
class DownloadCallback(Protocol):
    def __call__(
        self,
        dep_ref: "DependencyReference",
        apm_modules_dir: Path,
        parent_chain: str = "",
        parent_pkg: Optional["APMPackage"] = None,
    ) -> Path | None: ...


class APMDependencyResolver:
    """Handles recursive APM dependency resolution similar to NPM."""

    def __init__(
        self,
        max_depth: int = 50,
        apm_modules_dir: Path | None = None,
        download_callback: DownloadCallback | None = None,
        max_parallel: int | None = None,
    ):
        """Initialize the resolver with maximum recursion depth.

        Args:
            max_depth: Maximum depth for dependency resolution (default: 50)
            apm_modules_dir: Optional explicit apm_modules directory. If not provided,
                             will be determined from project_root during resolution.
            download_callback: Optional callback to download missing packages. If provided,
                               the resolver will attempt to fetch uninstalled transitive deps.
            max_parallel: Max worker threads for the level-batched
                parallel BFS download phase (the default execution
                model). ``None`` resolves from the
                ``APM_RESOLVE_PARALLEL`` env var, falling back to
                ``_DEFAULT_RESOLVE_PARALLEL`` (4). Set to ``1`` ONLY
                for parity-testing against the legacy sequential path
                -- this is a diagnostic knob, not a user toggle.
        """
        self.max_depth = max_depth
        self._apm_modules_dir: Path | None = apm_modules_dir
        self._project_root: Path | None = None
        self._download_callback = download_callback
        # Whether ``download_callback`` accepts ``parent_pkg`` (added in #857).
        # Detected once via signature inspection so legacy callbacks that
        # predate the field still work without raising a silent TypeError
        # that would mask the dependency.
        self._callback_accepts_parent_pkg: bool = (
            self._signature_accepts_parent_pkg(download_callback)
            if download_callback is not None
            else False
        )
        self._downloaded_packages: set[str] = (
            set()
        )  # Track what we downloaded during this resolution
        # Tracks ``dep_ref.get_unique_key()`` values rejected by the
        # remote-parent local_path guard (#940 / PR #1111 review C2). The
        # resolve phase folds this into ``ctx.callback_failures`` so the
        # integrate phase skips them with the same "already failed during
        # resolution" path used for download failures -- otherwise the
        # rejected dep would still sit in the dependency tree and get
        # copied later via ``_copy_local_package``, defeating the
        # fail-closed posture this guard is meant to enforce.
        self._rejected_remote_local_keys: set[str] = set()
        # Protects mutations of ``_downloaded_packages`` and
        # ``_rejected_remote_local_keys`` when the parallel BFS
        # dispatches ``_try_load_dependency_package`` calls onto a
        # worker pool. The ``max_parallel=1`` parity path still
        # acquires the lock -- the overhead is negligible and the
        # symmetry simplifies reasoning.
        self._download_lock = threading.Lock()
        self._max_parallel = self._resolve_max_parallel(max_parallel)

    @staticmethod
    def _resolve_max_parallel(explicit: int | None) -> int:
        """Compute effective worker count for level-batched parallel BFS.

        Parallel is the default and central execution model.  The
        override exists for parity testing (``APM_RESOLVE_PARALLEL=1``)
        and CI diagnostics, not as a user-facing knob.

        Order of precedence:
        1. Explicit ``max_parallel`` ctor arg.
        2. ``APM_RESOLVE_PARALLEL`` env var (diagnostic/parity knob).
        3. ``_DEFAULT_RESOLVE_PARALLEL``.

        Always coerced to ``>= 1`` so the executor never gets a zero
        or negative ``max_workers``.
        """
        if explicit is not None:
            return max(1, int(explicit))
        env = os.environ.get("APM_RESOLVE_PARALLEL", "").strip()
        if env:
            try:
                return max(1, int(env))
            except ValueError:
                _logger.debug("Ignoring invalid APM_RESOLVE_PARALLEL=%r", env)
        return _DEFAULT_RESOLVE_PARALLEL

    @staticmethod
    def _signature_accepts_parent_pkg(callback) -> bool:
        """Return True if ``callback`` declares a ``parent_pkg`` parameter
        (or accepts ``**kwargs``).

        Falls back to False if the signature can't be introspected (e.g. C
        extensions, builtins). The conservative fallback is correct: if we
        don't know the callback's shape, assume the legacy 3-arg form so
        the resolver won't pass an extra positional/keyword that triggers
        TypeError and silently drops the dependency (#940 SR1).
        """
        try:
            sig = inspect.signature(callback)
        except (TypeError, ValueError):
            return False
        for param in sig.parameters.values():
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if param.name == "parent_pkg":
                return True
        return False

    def resolve_dependencies(self, project_root: Path) -> DependencyGraph:
        return _download_work.resolve_dependencies(self, project_root)

    def _remote_parent_eligible(self, parent_dep: DependencyReference) -> bool:
        """Return True if *parent_dep* can serve as the Git repo for ``git: parent`` expansion."""
        if parent_dep.is_azure_devops():
            return bool(parent_dep.ado_repo and parent_dep.repo_url.count("/") >= 2)
        return "/" in parent_dep.repo_url

    def expand_parent_repo_decl(
        self, parent_dep: DependencyReference, child_dep: DependencyReference
    ) -> DependencyReference:
        return _download_work.expand_parent_repo_decl(self, parent_dep, child_dep)

    def build_dependency_tree(self, root_apm_yml: Path) -> DependencyTree:
        return _tree.build_dependency_tree(self, root_apm_yml)

    def detect_circular_dependencies(self, tree: DependencyTree) -> list[CircularRef]:
        return _tree.detect_circular_dependencies(self, tree)

    def flatten_dependencies(self, tree: DependencyTree) -> FlatDependencyMap:
        return _tree.flatten_dependencies(self, tree)

    def _validate_dependency_reference(self, dep_ref: DependencyReference) -> bool:
        """
        Validate that a dependency reference is well-formed.

        Args:
            dep_ref: The dependency reference to validate

        Returns:
            bool: True if valid, False otherwise
        """
        if not dep_ref.repo_url:
            return False

        # Basic validation - in real implementation would be more thorough
        if "/" not in dep_ref.repo_url:  # noqa: SIM103
            return False

        return True

    def _load_work_item(self, item):
        """Worker payload for the level-batched parallel BFS.

        Pure I/O wrapper around ``_try_load_dependency_package`` that
        returns ``(item, loaded_package_or_None, exception_or_None)``
        so the main thread can keep all tree mutations on its side.
        Defined as a method (not a closure inside the BFS while-loop)
        to satisfy ruff B023 -- no risk of accidentally capturing a
        loop-iteration variable.
        """
        node, dep_ref, parent_node, _is_dev = item
        # Compute breadcrumb chain from this node's ancestry so download
        # errors can report "root > mid > failing-dep" context.
        parent_chain = node.get_ancestor_chain()
        try:
            loaded = self._try_load_dependency_package(
                dep_ref,
                parent_chain=parent_chain,
                parent_pkg=parent_node.package if parent_node else None,
            )
            return (item, loaded, None)
        except (ValueError, FileNotFoundError) as exc:
            return (item, None, exc)

    def _try_load_dependency_package(
        self,
        dep_ref: DependencyReference,
        parent_chain: str = "",
        parent_pkg: APMPackage | None = None,
    ) -> APMPackage | None:
        return _download_work._try_load_dependency_package(self, dep_ref, parent_chain, parent_pkg)

    @staticmethod
    def _is_remote_parent(parent_pkg: APMPackage | None) -> bool:
        """Return True if *parent_pkg* is a REMOTE package (i.e. fetched via
        git URL or pinned by ref/path).

        Used to gate ``local_path`` deps: only the root project and other
        local packages may legitimately declare them. Remote packages
        declaring a local_path is a path-confusion vector.

        SECURITY NOTE: this is a heuristic on the ``source`` field. A
        sufficiently adversarial remote could spoof a local-looking source.
        The downstream containment check via ``ensure_path_within`` is the
        actual security boundary; this gate just produces the user-facing
        error early.
        """
        if parent_pkg is None or not parent_pkg.source:
            return False
        src = str(parent_pkg.source)
        # Local deps get ``source = "_local/<name>"`` (see DependencyReference
        # construction for is_local=True). Treat that prefix as definitively
        # local even though it contains a slash.
        if src.startswith("_local/"):
            return False
        # Remote sources look like URLs or owner/repo refs. Local sources
        # are filesystem paths the user typed in their apm.yml.
        return (
            src.startswith(("http://", "https://", "git@", "ssh://", "git+"))
            or "://" in src
            or (src.count("/") >= 1 and not src.startswith((".", "/", "~")))
        )

    @staticmethod
    def _compute_dep_source_path(
        dep_ref: DependencyReference,
        parent_pkg: APMPackage | None,
        install_path: Path,
    ) -> Path:
        """Return the source-path anchor for a dependency.

        For LOCAL deps we return the *original* user source directory so that
        transitive ``../sibling`` references inside its apm.yml resolve as a
        developer reading the file expects (#857). For REMOTE deps we return
        the clone location under apm_modules.
        """
        if dep_ref.is_local and dep_ref.local_path:
            local = Path(dep_ref.local_path).expanduser()
            if not local.is_absolute() and parent_pkg is not None and parent_pkg.source_path:
                return (parent_pkg.source_path / local).resolve()
            return local.resolve()
        return install_path.resolve()

    @staticmethod
    def _download_dedup_key(dep_ref: DependencyReference, parent_pkg: APMPackage | None) -> str:
        """Dedup key for the download cache.

        Includes the parent's source_path so two parents anchoring the same
        local dep at different absolute locations don't collide on the first
        one's resolved path. For non-local deps, the parent anchor doesn't
        affect resolution, so the bare unique key suffices.
        """
        base = dep_ref.get_unique_key()
        if dep_ref.is_local and parent_pkg is not None and parent_pkg.source_path:
            return f"{base}@{parent_pkg.source_path}"
        return base

    @staticmethod
    def _effective_base_dir(parent_pkg: APMPackage | None, project_root: Path) -> Path:
        """Return the directory used to anchor relative ``local_path`` deps.

        For direct (root-declared) deps, this is the project root. For
        transitive deps, it is the declaring package's source_path so a
        ``../sibling`` written inside the original package directory means
        what the author meant (#857).
        """
        if parent_pkg is not None and parent_pkg.source_path is not None:
            return parent_pkg.source_path
        return project_root

    def _create_resolution_summary(self, graph: DependencyGraph) -> str:
        """
        Create a human-readable summary of the resolution results.

        Args:
            graph: The resolved dependency graph

        Returns:
            str: Summary string
        """
        summary = graph.get_summary()
        lines = [
            "Dependency Resolution Summary:",
            f"  Root package: {summary['root_package']}",
            f"  Total dependencies: {summary['total_dependencies']}",
            f"  Maximum depth: {summary['max_depth']}",
        ]

        if summary["has_conflicts"]:
            lines.append(f"  Conflicts detected: {summary['conflict_count']}")

        if summary["has_circular_dependencies"]:
            lines.append(f"  Circular dependencies: {summary['circular_count']}")

        if summary["has_errors"]:
            lines.append(f"  Resolution errors: {summary['error_count']}")

        lines.append(f"  Status: {'[+] Valid' if summary['is_valid'] else '[x] Invalid'}")

        return "\n".join(lines)


from . import download_work as _download_work
from . import tree as _tree
