"""Smoke tests: every resolve/ submodule imports without error."""

import importlib

import pytest

RESOLVE_MODULES = [
    "apm_cli.install.phases.resolve",
    "apm_cli.install.phases.resolve.run",
    "apm_cli.install.phases.resolve.lockfile_load",
    "apm_cli.install.phases.resolve.transitive_resolve",
    "apm_cli.install.phases.resolve.state_capture",
    "apm_cli.install.phases.resolve.downloader_setup",
    "apm_cli.install.phases.resolve.download_callback",
]


@pytest.mark.parametrize("module_path", RESOLVE_MODULES)
def test_resolve_submodule_imports(module_path: str) -> None:
    """Each resolve submodule must be importable (no circular deps, no missing symbols)."""
    mod = importlib.import_module(module_path)
    assert mod is not None
