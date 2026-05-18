"""Lockfile-safe deployed-path helpers.

``_deployed_path_entry`` converts an absolute ``target_path`` returned by an
integrator into the string stored in the project lockfile.  Standard-root
targets use a ``project_root``-relative POSIX path; dynamic-root (cowork)
targets translate to the ``cowork://`` URI scheme via
``copilot_cowork_paths.to_lockfile_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _deployed_path_entry(
    target_path: Path,
    project_root: Path,
    targets: Any,
) -> str:
    """Return the lockfile-safe path string for a deployed file.

    For standard targets the entry is ``project_root``-relative.  For
    cowork (dynamic-root) targets the entry uses the synthetic
    ``cowork://`` URI scheme so the lockfile pipeline does not attempt
    a ``Path.relative_to(project_root)`` that would crash.

    Raises
    ------
    RuntimeError
        If the path is outside the project tree and cannot be
        translated to a ``cowork://`` URI via any available target.
    """
    try:
        return target_path.relative_to(project_root).as_posix()
    except ValueError:
        # Path is outside the project tree -- must be a dynamic-root
        # target.  Find the matching target and translate.
        if targets:
            for _t in targets:
                if _t.resolved_deploy_root is not None:
                    from apm_cli.integration.copilot_cowork_paths import to_lockfile_path

                    return to_lockfile_path(target_path, _t.resolved_deploy_root)
        raise RuntimeError(  # noqa: B904
            f"Cannot translate {target_path!r} to a lockfile path: "
            f"path is outside the project tree and no dynamic-root "
            f"target matched. This is a bug -- please report it."
        )


__all__ = ["_deployed_path_entry"]
