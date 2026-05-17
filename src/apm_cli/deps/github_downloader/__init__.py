from __future__ import annotations

import shutil  # noqa: F401
import tempfile  # noqa: F401
import time  # noqa: F401

import git  # noqa: F401
import requests  # noqa: F401
from git import Repo  # noqa: F401

from apm_cli.core.auth import AuthResolver  # noqa: F401
from apm_cli.models.apm_package import validate_apm_package  # noqa: F401
from apm_cli.utils.console import _rich_warning  # noqa: F401

from .class_ import (  # noqa: F401
    GitHubPackageDownloader,
    _close_repo,  # noqa: F401
    _debug,
    _rmtree,
)

# Re-export contract for ruff --ignore-noqa.
__all__ = [
    "AuthResolver",
    "GitHubPackageDownloader",
    "Repo",
    "_close_repo",
    "_debug",
    "_rich_warning",
    "_rmtree",
    "git",
    "requests",
    "shutil",
    "tempfile",
    "time",
    "validate_apm_package",
]
