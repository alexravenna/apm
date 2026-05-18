"""``apm marketplace doctor`` command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.yml_schema import load_marketplace_yml
from . import doctor_checks as _doctor_checks
from . import marketplace
from ._doctor import _render_doctor_table
from .doctor_checks import (
    _check_auth,
    _check_duplicate_names,
    _check_gh_cli,
    _check_git,
    _check_marketplace_config,
    _check_network,
    _critical_checks_passed,
)


@marketplace.command(help="Run environment diagnostics for marketplace publishing")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def doctor(verbose):
    """Check git, network, auth, and marketplace config readiness."""
    _doctor_checks.subprocess = subprocess
    _doctor_checks.load_marketplace_yml = load_marketplace_yml
    logger = CommandLogger("marketplace-doctor", verbose=verbose)
    project_root = Path.cwd()
    checks = [
        _check_git(),
        _check_network(),
        _check_auth(),
        _check_gh_cli(),
    ]
    config_check, yml_obj = _check_marketplace_config(project_root)
    checks.append(config_check)
    duplicate_check = _check_duplicate_names(yml_obj)
    if duplicate_check is not None:
        checks.append(duplicate_check)
    _render_doctor_table(logger, checks)
    if not _critical_checks_passed(checks):
        sys.exit(1)
