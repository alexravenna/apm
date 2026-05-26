"""Thin wrapper around ``subprocess.run(["git", ...])`` for APM internals.

Centralises git executable lookup, ambient-env scrubbing, PyInstaller
library-path restoration, and the ``GIT_TERMINAL_PROMPT=0`` default so
every git invocation in apm_cli is consistent.

Callers that need authentication semantics (PAT/bearer fallback)
**must** route through :class:`apm_cli.core.auth.AuthResolver` -- this
helper is intentionally credential-agnostic and relies on the user's
existing git credential setup (helper, SSH agent, or PAT in remote URL).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from .git_env import get_git_executable, git_subprocess_env
from .subprocess_env import external_process_env


class GitNotFoundError(FileNotFoundError):
    """Raised when the git executable cannot be located on PATH."""


def run_git(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = None,
    check: bool = False,
    no_prompt: bool = True,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` with sanitised environment and captured output.

    Parameters
    ----------
    args:
        Argument list passed to git (without the leading ``git``).
    cwd:
        Working directory for the subprocess.
    timeout:
        Optional timeout in seconds.
    check:
        If True, raise :class:`subprocess.CalledProcessError` on non-zero exit.
    no_prompt:
        If True (default), set ``GIT_TERMINAL_PROMPT=0`` so git never
        prompts on a missing credential.

    Raises
    ------
    GitNotFoundError
        If git is not on PATH.
    """
    try:
        git_bin = get_git_executable()
    except FileNotFoundError as exc:
        raise GitNotFoundError(str(exc)) from exc

    env = external_process_env(git_subprocess_env())
    if no_prompt:
        env["GIT_TERMINAL_PROMPT"] = "0"

    return subprocess.run(
        [git_bin, *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=check,
    )
