"""Helper for resolving APM dependency declaration order from apm.yml / apm.lock."""

from pathlib import Path
from typing import Any

from ..deps.lockfile import LockFile
from ..models.apm_package import APMPackage


def get_dependency_declaration_order(
    base_dir: str,
    *,
    apm_package_cls: type[APMPackage] = APMPackage,
    lockfile_cls: type[LockFile] = LockFile,
) -> list[str]:
    """Get APM dependency installed paths in their declaration order.

    The returned list contains the actual installed path for each dependency,
    combining:
    1. Direct dependencies from apm.yml (highest priority, declaration order)
    2. Transitive dependencies from apm.lock (appended after direct deps)

    This ensures transitive dependencies are included in primitive discovery
    and compilation, not just direct dependencies. The installed path differs for:
    - Regular packages: owner/repo (GitHub) or org/project/repo (ADO)
    - Virtual packages: owner/virtual-pkg-name (GitHub) or org/project/virtual-pkg-name (ADO)

    Args:
        base_dir (str): Base directory containing apm.yml.

    Returns:
        List[str]: List of dependency installed paths in declaration order.
    """
    try:
        apm_yml_path = Path(base_dir) / "apm.yml"
        if not apm_yml_path.exists():
            return []

        package = apm_package_cls.from_apm_yml(apm_yml_path)
        apm_dependencies = package.get_apm_dependencies()

        # Extract installed paths from dependency references
        # Virtual file/collection packages use get_virtual_package_name() (flattened),
        # while virtual subdirectory packages use natural repo/subdir paths.
        dependency_names = []
        for dep in apm_dependencies:
            if dep.alias:
                dependency_names.append(dep.alias)
            elif dep.is_virtual:
                repo_parts = dep.repo_url.split("/")

                if dep.is_virtual_subdirectory() and dep.virtual_path:
                    # Virtual subdirectory packages keep natural path structure.
                    # GitHub: owner/repo/subdir
                    # ADO: org/project/repo/subdir
                    if dep.is_azure_devops() and len(repo_parts) >= 3:
                        dependency_names.append(
                            f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}/{dep.virtual_path}"
                        )
                    elif len(repo_parts) >= 2:
                        dependency_names.append(
                            f"{repo_parts[0]}/{repo_parts[1]}/{dep.virtual_path}"
                        )
                    else:
                        dependency_names.append(dep.virtual_path)
                else:
                    # Virtual file/collection packages are flattened by package name.
                    # GitHub: owner/virtual-pkg-name
                    # ADO: org/project/virtual-pkg-name
                    virtual_name = dep.get_virtual_package_name()
                    if dep.is_azure_devops() and len(repo_parts) >= 3:
                        dependency_names.append(f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}")
                    elif len(repo_parts) >= 2:
                        dependency_names.append(f"{repo_parts[0]}/{virtual_name}")
                    else:
                        dependency_names.append(virtual_name)
            else:
                # Regular packages: use full org/repo path
                # This matches our org-namespaced directory structure
                dependency_names.append(dep.repo_url)

        # Include transitive dependencies from apm.lock
        # Direct deps from apm.yml have priority; transitive deps are appended
        lockfile_paths = lockfile_cls.installed_paths_for_project(Path(base_dir))
        direct_set = set(dependency_names)
        for path in lockfile_paths:
            if path not in direct_set:
                dependency_names.append(path)

        return dependency_names

    except Exception as e:
        print(f"Warning: Failed to parse dependency order from apm.yml: {e}")
        return []
