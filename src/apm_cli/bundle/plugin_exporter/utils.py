"""Utility functions for plugin export."""

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from ...models.apm_package import DependencyReference

_SAFE_BUNDLE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


def validate_output_rel(rel: str) -> bool:
    """Return True when *rel* is safe to write inside the output directory."""
    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        return False
    return ".." not in Path(rel).parts


def sanitize_bundle_name(name: str) -> str:
    """Sanitise a package name/version for use as a directory component.

    Replaces path separators and traversal characters with hyphens, then
    validates the result is a single safe path component.
    """
    sanitised = _SAFE_BUNDLE_NAME_RE.sub("-", name).strip("-") or "unnamed"
    if ".." in sanitised or "/" in sanitised or "\\" in sanitised:
        sanitised = "unnamed"
    return sanitised


def get_dev_dependency_urls(apm_yml_path: Path) -> set[tuple[str, str]]:
    """Read ``devDependencies.apm`` from raw YAML and return a set of
    ``(repo_url, virtual_path)`` tuples for matching against lockfile entries.

    Using the composite key avoids false positives when multiple virtual
    packages share the same base repo (e.g. different sub-paths under
    ``github/awesome-copilot``).
    """
    try:
        from ...utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path)
    except (yaml.YAMLError, OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    dev_deps = data.get("devDependencies", {})
    if not isinstance(dev_deps, dict):
        return set()
    apm_dev = dev_deps.get("apm", [])
    if not isinstance(apm_dev, list):
        return set()
    keys: set[tuple[str, str]] = set()
    for dep in apm_dev:
        if isinstance(dep, str):
            try:
                ref = DependencyReference.parse(dep)
                keys.add((ref.repo_url, ref.virtual_path or ""))
            except ValueError:
                keys.add((dep, ""))
        elif isinstance(dep, dict):
            try:
                ref = DependencyReference.parse_from_dict(dep)
                keys.add((ref.repo_url, ref.virtual_path or ""))
            except ValueError:
                pass
    return keys


def dep_install_path(dep, apm_modules_dir: Path) -> Path:
    """Compute the filesystem install path for a locked dependency."""
    dep_ref = dep.to_dependency_ref()
    return dep_ref.get_install_path(apm_modules_dir)


def merge_file_map(
    file_map: dict[str, tuple[Path, str]],
    components: list[tuple[Path, str]],
    owner: str,
    force: bool,
    collisions: list[str],
) -> None:
    """Merge *components* into *file_map* with collision handling.

    Without ``--force``: first writer wins (skip with warning).
    With ``--force``: last writer wins (overwrite with warning).
    """
    for source, output_rel in components:
        if not validate_output_rel(output_rel):
            continue
        if output_rel in file_map:
            existing_owner = file_map[output_rel][1]
            collisions.append(
                f"{output_rel} — collision between '{existing_owner}' and "
                f"'{owner}' ({'last writer wins' if force else 'first writer wins'})"
            )
            if force:
                file_map[output_rel] = (source, owner)
            # else: first writer wins, skip
        else:
            file_map[output_rel] = (source, owner)
