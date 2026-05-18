"""Shared CLI helpers for APM command modules.

This module must NOT import from any command module.
"""

import builtins
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import click
from colorama import Fore, Style
from colorama import init as colorama_init

from ..constants import (
    APM_DIR,
    APM_MODULES_DIR,
    APM_MODULES_GITIGNORE_PATTERN,
    APM_YML_FILENAME,
    GITIGNORE_FILENAME,
)
from ..update_policy import get_update_hint_message, is_self_update_enabled
from ..utils.atomic_io import atomic_write_text as _atomic_write
from ..utils.console import _rich_echo, _rich_info, _rich_warning
from ..utils.path_security import PathTraversalError, validate_path_segments
from ..utils.version_checker import check_for_updates
from ..version import get_build_sha, get_version
from ._helpers_init import (
    _auto_detect_author,
    _auto_detect_description,
    _create_minimal_apm_yml,
    _create_plugin_json,
    _get_default_config,
    _get_default_script,
    _list_available_scripts,
    _load_apm_config,
    _validate_plugin_name,
    _validate_project_name,
)

# CRITICAL: Shadow Click commands at module level to prevent namespace collision
# When Click commands like 'config set' are defined, calling set() can invoke the command
# instead of the Python built-in. This affects ALL functions in this module.
set = builtins.set
list = builtins.list
dict = builtins.dict

# Initialize colorama for fallback
colorama_init(autoreset=True)

# Legacy colorama constants for compatibility
TITLE = f"{Fore.CYAN}{Style.BRIGHT}"
SUCCESS = f"{Fore.GREEN}{Style.BRIGHT}"
ERROR = f"{Fore.RED}{Style.BRIGHT}"
INFO = f"{Fore.BLUE}"
WARNING = f"{Fore.YELLOW}"
HIGHLIGHT = f"{Fore.MAGENTA}{Style.BRIGHT}"
RESET = Style.RESET_ALL


# -------------------------------------------------------------------
# TTY detection
# -------------------------------------------------------------------


def _is_interactive():
    """Return True when both stdin and stdout are attached to a TTY."""
    return sys.stdin.isatty() and sys.stdout.isatty()


# Lazy loading for Rich components to improve startup performance
_console = None


def _get_console():
    """Get Rich console instance with lazy loading."""
    global _console
    if _console is None:
        from rich.console import Console
        from rich.theme import Theme

        custom_theme = Theme(
            {
                "info": "cyan",
                "warning": "yellow",
                "error": "bold red",
                "success": "bold green",
                "highlight": "bold magenta",
                "muted": "dim white",
                "accent": "bold blue",
                "title": "bold cyan",
            }
        )

        _console = Console(theme=custom_theme)
    return _console


def _rich_blank_line():
    """Print a blank line with Rich if available, otherwise use click."""
    console = _get_console()
    if console:
        console.print()
    else:
        click.echo()


def _lazy_yaml():
    """Lazy import for yaml module to improve startup performance."""
    try:
        import yaml

        return yaml
    except ImportError:
        raise ImportError("PyYAML is required but not installed")  # noqa: B904


def _lazy_prompt():
    """Lazy import for Rich Prompt to improve startup performance."""
    try:
        from rich.prompt import Prompt

        return Prompt
    except ImportError:
        return None


def _lazy_confirm():
    """Lazy import for Rich Confirm to improve startup performance."""
    try:
        from rich.prompt import Confirm

        return Confirm
    except ImportError:
        return None


# ------------------------------------------------------------------
# Shared orphan-detection helpers
# ------------------------------------------------------------------


def _build_expected_install_paths(declared_deps, lockfile, apm_modules_dir: Path) -> set:
    """Build expected package paths under *apm_modules_dir*.

    Combines direct deps (from ``apm.yml``) with transitive deps
    (depth > 1 from ``apm.lock``), using ``get_install_path()`` for
    consistency with how packages are actually installed.
    """
    expected = set()
    for dep in declared_deps:
        install_path = dep.get_install_path(apm_modules_dir)
        try:
            relative_path = install_path.relative_to(apm_modules_dir)
            expected.add(relative_path.as_posix())
        except ValueError:
            expected.add(str(install_path))

    if lockfile:
        for dep in lockfile.get_package_dependencies():
            if dep.depth is not None and dep.depth > 1:
                dep_ref = dep.to_dependency_ref()
                install_path = dep_ref.get_install_path(apm_modules_dir)
                try:
                    relative_path = install_path.relative_to(apm_modules_dir)
                    expected.add(relative_path.as_posix())
                except ValueError:
                    pass
    return expected


def _expand_with_ancestors(
    paths: Iterable[str], installed: Iterable[str] | None = None
) -> set[str]:
    """Expand a set of expected paths to include ancestor prefixes.

    Given ``{"owner/repo/.apm/skills/my-skill"}``, returns a set containing
    the original path plus all intermediate path prefixes with 2+ segments
    (e.g., ``"owner/repo"``, ``"owner/repo/.apm"``,
    ``"owner/repo/.apm/skills"``, plus the original
    ``"owner/repo/.apm/skills/my-skill"``).
    This allows O(1) membership checks when determining whether a scanned
    directory is an ancestor of an expected package path.

    Ancestor expansion exists because a subdirectory dependency
    (``git: owner/repo, path: .apm/skills/x``) is installed by cloning the
    entire repo to ``apm_modules/owner/repo/``. Intermediate filesystem
    directories created by that clone are required parts of the install --
    not stale leftovers.

    Real-orphan safety: when *installed* is supplied, an ancestor that
    matches one of the installed paths is NOT added to the expansion
    unless that path is also directly declared in *paths*. Callers should
    pass only the subset of installed paths that look like *real
    standalone packages* (i.e., directories that ship their own
    ``apm.yml``) -- not filesystem intermediaries (which typically have
    only a ``.apm/`` subtree from a cloned subdir dep). This preserves
    orphan detection for the case where a user has a genuinely orphaned
    ``owner/repo`` package on disk alongside a declared sibling
    subdirectory dep (``owner/repo/.apm/skills/foo``): only filesystem
    intermediaries are suppressed, never real installed packages.

    Security contract -- ancestor depth cap: ``get_install_path()``
    anchors installs at the 2-segment repo root (GitHub) or 3-segment
    root (ADO). Anything deeper is a filesystem-intermediary path
    (``.apm/``, ``skills/``, ...) that ``_scan_installed_packages``
    skips, so emitting ancestors past depth 3 would only widen the
    orphan-suppression surface without serving any real lookup. The
    loop is therefore capped at depth 3 (``min(4, len(parts))``), which
    bounds the number of paths an attacker-influenced ``apm.yml`` dep
    declaration can hide from orphan detection. If the install strategy
    ever grows deeper roots, lift this cap and document the new
    invariant here.

    Traversal guard: any input path that fails
    :func:`apm_cli.utils.path_security.validate_path_segments` (which
    rejects both ``.`` and ``..`` segments after backslash
    normalisation) is kept in the result as-is (membership check) but
    produces no ancestors. Routing through the canonical guard --
    rather than a hand-rolled ``".." in parts`` check -- ensures
    single-dot segments (``owner/./repo``) are also caught and keeps
    the project's path-validation contract centralised.
    """
    materialized = list(paths)
    materialized_set = set(materialized)
    expanded = set(materialized)
    installed_set = set(installed) if installed is not None else set()
    for p in materialized:
        try:
            validate_path_segments(p, context="ancestor expansion")
        except PathTraversalError:
            continue
        # Normalise backslashes so Windows-style tokens split into the
        # same parts as POSIX inputs for the depth-capped loop below.
        normalised = p.replace("\\", "/")
        parts = normalised.split("/")
        # Cap at depth 3 -- the ADO install-root depth -- to bound the
        # ancestor-suppression surface (see security contract above).
        for i in range(2, min(4, len(parts))):
            ancestor = "/".join(parts[:i])
            # Do not mask a real installed package via ancestor expansion;
            # only filesystem intermediaries should be added. A real
            # installed package that is also directly declared remains in
            # expanded via materialized_set.
            if ancestor in installed_set and ancestor not in materialized_set:
                continue
            expanded.add(ancestor)
    return expanded


def _standalone_installed_packages(
    installed: Iterable[str], apm_modules_dir: Path, lockfile=None
) -> list:
    """Filter *installed* to entries that look like real standalone packages.

    Determination order (tamper-evident first):

    1. Path appears as a dependency key in *lockfile* -- the canonical
       record of what APM installed. The lockfile is integrity-checked
       and not forgeable by dropping/omitting files in ``apm_modules/``.
    2. Fallback: path has its own ``apm.yml``. Used when the lockfile
       is absent (older installs / fresh checkouts) or does not list
       the key. A directory with only a ``.apm/`` marker is treated as
       a filesystem intermediary, not a standalone package.

    Combining both signals closes the suppression-via-absence gap
    (panel finding: forgeable ``apm.yml`` heuristic) while preserving
    behaviour for projects that pre-date the lockfile or have not yet
    re-installed.

    Failure mode: only narrowly-typed shape errors against
    ``lockfile.dependencies`` (``AttributeError`` / ``TypeError`` /
    ``KeyError``) are absorbed and degrade to the ``apm.yml``-only
    fallback. Any other exception (e.g. lockfile parse / I/O failure)
    propagates so the outer caller can decide whether to log or fail
    closed -- preventing a corrupted or attacker-crafted lockfile from
    silently disabling the tamper-evident standalone check.
    """
    lockfile_keys: set[str] = set()
    if lockfile is not None:
        try:
            for dep_key in lockfile.dependencies:
                if dep_key:
                    lockfile_keys.add(dep_key)
        except (AttributeError, TypeError, KeyError):
            lockfile_keys = set()
    standalone: list = []
    for p in installed:
        if p in lockfile_keys:
            standalone.append(p)
            continue
        if (apm_modules_dir / p / APM_YML_FILENAME).exists():
            standalone.append(p)
    return standalone


def _check_orphaned_packages():
    """Check for packages in apm_modules/ that are not declared in apm.yml or apm.lock.

    Considers both direct dependencies (from apm.yml) and transitive dependencies
    (from apm.lock) as expected packages, so transitive deps are not falsely
    flagged as orphaned.

    Returns:
        List[str]: List of orphaned package names in org/repo or org/project/repo format
    """
    try:
        if not Path(APM_YML_FILENAME).exists():
            return []

        apm_modules_dir = Path(APM_MODULES_DIR)
        if not apm_modules_dir.exists():
            return []

        try:
            from ..deps.lockfile import LockFile, get_lockfile_path
            from ..models.apm_package import APMPackage

            apm_package = APMPackage.from_apm_yml(Path(APM_YML_FILENAME))
            declared_deps = apm_package.get_apm_dependencies()
            lockfile = LockFile.read(get_lockfile_path(Path.cwd()))
            expected = _build_expected_install_paths(declared_deps, lockfile, apm_modules_dir)
        except Exception:
            return []

        installed = _scan_installed_packages(apm_modules_dir)
        # Combined lockfile-membership + apm.yml fallback determines
        # which installed paths are real standalone packages (and so
        # must NOT be masked by ancestor expansion). The lockfile is
        # the canonical, tamper-evident record; apm.yml-existence is
        # the fallback for projects without a lockfile yet.
        # See _expand_with_ancestors for the user-safety rationale.
        standalone_installed = _standalone_installed_packages(
            installed, apm_modules_dir, lockfile=lockfile
        )
        expected_with_ancestors = _expand_with_ancestors(expected, standalone_installed)
        # Sort for deterministic, diffable output across runs (rglob
        # traversal order is filesystem-dependent).
        return sorted(p for p in installed if p not in expected_with_ancestors)
    except Exception:
        return []


def print_version(ctx, param, value):
    """Print version and exit."""
    if not value or ctx.resilient_parsing:
        return

    version_str = get_version()
    sha = get_build_sha()
    if sha:
        version_str += f" ({sha})"

    console = _get_console()
    if console:
        try:
            console.print(
                f"[bold cyan]Agent Package Manager (APM) CLI[/bold cyan] version {version_str}"
            )
        except Exception:
            click.echo(f"{TITLE}Agent Package Manager (APM) CLI{RESET} version {version_str}")
    else:
        # Graceful fallback when Rich isn't available (e.g., stripped automation environment)
        click.echo(f"{TITLE}Agent Package Manager (APM) CLI{RESET} version {version_str}")

    # Gated verbose-version output (experimental flag)
    try:
        from ..core.experimental import is_enabled

        if is_enabled("verbose_version"):
            import platform
            import sys

            python_ver = platform.python_version()
            plat = f"{sys.platform}-{platform.machine()}"
            install_path = str(Path(__file__).resolve().parent.parent)

            _rich_echo(f"  {'Python:':<14}{python_ver}", color="dim")
            _rich_echo(f"  {'Platform:':<14}{plat}", color="dim")
            _rich_echo(f"  {'Install path:':<14}{install_path}", color="dim")
    except Exception:
        # Never let experimental flag logic break --version
        pass

    ctx.exit()


def _check_and_notify_updates():
    """Check for updates and notify user non-blockingly."""
    try:
        # Skip notifications when self-update is disabled by distribution policy.
        if not is_self_update_enabled():
            return

        # Skip version check in E2E test mode to avoid interfering with tests
        if os.environ.get("APM_E2E_TESTS", "").lower() in ("1", "true", "yes"):
            return

        current_version = get_version()

        # Skip check for development versions
        if current_version == "unknown":
            return

        latest_version = check_for_updates(current_version)

        if latest_version:
            # Display yellow warning with update command
            _rich_warning(
                f"A new version of APM is available: {latest_version} (current: {current_version})",
                symbol="warning",
            )

            # Show update command using helper for consistency
            _rich_echo(get_update_hint_message(), color="yellow", bold=True)

            # Add a blank line for visual separation
            click.echo()
    except Exception:
        # Silently fail - version checking should never block CLI usage
        pass


def _update_gitignore_for_apm_modules(logger=None):
    """Add apm_modules/ to .gitignore if not already present."""
    gitignore_path = Path(GITIGNORE_FILENAME)
    apm_modules_pattern = APM_MODULES_GITIGNORE_PATTERN

    # Read current .gitignore content
    current_content = []
    if gitignore_path.exists():
        try:
            with open(gitignore_path, encoding="utf-8") as f:
                current_content = [line.rstrip("\n\r") for line in f.readlines()]
        except Exception as e:
            if logger:
                logger.warning(f"Could not read .gitignore: {e}")
            else:
                _rich_warning(f"Could not read .gitignore: {e}")
            return

    # Check if apm_modules/ is already in .gitignore
    if any(line.strip() == apm_modules_pattern for line in current_content):
        return  # Already present

    # Add apm_modules/ to .gitignore
    try:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            # Add a blank line before our entry if file isn't empty
            if current_content and current_content[-1].strip():
                f.write("\n")
            f.write(f"\n# APM dependencies\n{apm_modules_pattern}\n")

        if logger:
            logger.progress(f"Added {apm_modules_pattern} to .gitignore")
        else:
            _rich_info(f"Added {apm_modules_pattern} to .gitignore")
    except Exception as e:
        if logger:
            logger.warning(f"Could not update .gitignore: {e}")
        else:
            _rich_warning(f"Could not update .gitignore: {e}")
