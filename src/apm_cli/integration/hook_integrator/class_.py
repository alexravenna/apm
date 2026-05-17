"""Hook integration functionality for APM packages.

Integrates hook JSON files and their referenced scripts during package
installation. Supports VSCode Copilot (.github/hooks/), Claude Code
(.claude/settings.json), and Cursor (.cursor/hooks.json) targets.

Hook JSON format (Claude Code  -- nested matcher groups):
    {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": "./scripts/validate.sh", "timeout": 10}
                    ]
                }
            ]
        }
    }

Hook JSON format (GitHub Copilot  -- flat arrays with bash/powershell keys):
    {
        "version": 1,
        "hooks": {
            "preToolUse": [
                {"type": "command", "bash": "./scripts/validate.sh", "timeoutSec": 10}
            ]
        }
    }

Hook JSON format (Cursor  -- flat arrays with command key):
    {
        "hooks": {
            "afterFileEdit": [
                {"command": "./hooks/format.sh"}
            ]
        }
    }

Script path handling:
    - ${CLAUDE_PLUGIN_ROOT}/path, ${CURSOR_PLUGIN_ROOT}/path, ${PLUGIN_ROOT}/path
      -> resolved relative to package root, rewritten for target
    - ./path -> relative path, resolved from hook file's parent directory, rewritten for target
    - System commands (no path separators) -> passed through unchanged
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult

_log = logging.getLogger(__name__)


# DEPRECATED -- use IntegrationResult directly for new code.
# Backward-compatible shim: accepts hooks_integrated= kwarg and
# exposes a hooks_integrated property for consumers of the old API.
class HookIntegrationResult(IntegrationResult):
    """Backward-compatible wrapper around IntegrationResult."""

    def __init__(self, *args, hooks_integrated=None, **kwargs):
        if hooks_integrated is not None:
            kwargs.setdefault("files_integrated", hooks_integrated)
            kwargs.setdefault("files_updated", 0)
            kwargs.setdefault("files_skipped", 0)
            kwargs.setdefault("target_paths", [])
        super().__init__(*args, **kwargs)

    @property
    def hooks_integrated(self):
        """Alias for files_integrated (backward compat)."""
        return self.files_integrated


@dataclass(frozen=True)
class _MergeHookConfig:
    """Configuration for targets that merge hooks into a single JSON file."""

    config_filename: str  # e.g. "settings.json" or "hooks.json"
    target_key: str  # target name passed to _rewrite_hooks_data
    require_dir: bool  # True = skip if target dir doesn't exist


# Per-target hook event name mapping.  Packages are authored with
# Copilot (camelCase) or Claude (PascalCase) names; targets that use
# different conventions get their events renamed during merge.
_HOOK_EVENT_MAP: dict[str, dict[str, str]] = {
    "claude": {
        # Copilot camelCase -> Claude PascalCase
        "preToolUse": "PreToolUse",
        "postToolUse": "PostToolUse",
    },
    "gemini": {
        # Copilot / Claude -> Gemini
        "PreToolUse": "BeforeTool",
        "preToolUse": "BeforeTool",
        "PostToolUse": "AfterTool",
        "postToolUse": "AfterTool",
        "Stop": "SessionEnd",
    },
}


def _to_gemini_hook_entries(entries: list) -> list:
    """Transform hook entries into Gemini CLI format.

    Gemini requires ``{"hooks": [...]}`` nesting, uses ``command`` (not
    ``bash``), and ``timeout`` in milliseconds (not ``timeoutSec`` in
    seconds).  Entries already in Claude/Gemini nested format are left
    unchanged.
    """
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        # Already nested (Claude / Gemini format) -- just fix inner keys
        if "hooks" in entry and isinstance(entry["hooks"], list):
            for hook in entry["hooks"]:
                _copilot_keys_to_gemini(hook)
            result.append(entry)
            continue
        # Flat Copilot entry -- wrap in nested format
        inner = dict(entry)
        _copilot_keys_to_gemini(inner)
        # Pull _apm_source to outer level (set later, but keep if present)
        apm_source = inner.pop("_apm_source", None)
        outer: dict = {"hooks": [inner]}
        if apm_source:
            outer["_apm_source"] = apm_source
        result.append(outer)
    return result


def _copilot_keys_to_gemini(hook: dict) -> None:
    """Rename Copilot hook keys to Gemini equivalents in-place."""
    # bash / powershell -> command
    if "command" not in hook:
        for key in ("bash", "powershell", "windows"):
            if key in hook:
                hook["command"] = hook.pop(key)
                break
    # timeoutSec (seconds) -> timeout (milliseconds)
    if "timeoutSec" in hook:
        hook["timeout"] = hook.pop("timeoutSec") * 1000


_MERGE_HOOK_TARGETS: dict[str, _MergeHookConfig] = {
    "claude": _MergeHookConfig(
        config_filename="settings.json",
        target_key="claude",
        require_dir=False,
    ),
    "cursor": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="cursor",
        require_dir=True,
    ),
    "codex": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="codex",
        require_dir=True,
    ),
    "gemini": _MergeHookConfig(
        config_filename="settings.json",
        target_key="gemini",
        require_dir=True,
    ),
    "windsurf": _MergeHookConfig(
        config_filename="hooks.json",
        target_key="windsurf",
        require_dir=True,
    ),
}


# Mapping from hook-file stem suffix to the set of target keys that
# should receive the file.  Files whose stem does not match any
# suffix are treated as universal and deployed to every target.
_HOOK_FILE_TARGET_SUFFIXES: dict[str, set[str]] = {
    "copilot-hooks": {"copilot", "vscode"},
    "cursor-hooks": {"cursor"},
    "claude-hooks": {"claude"},
    "codex-hooks": {"codex"},
    "gemini-hooks": {"gemini"},
    "windsurf-hooks": {"windsurf"},
}


def _filter_hook_files_for_target(
    hook_files: list[Path],
    target_key: str,
) -> list[Path]:
    """Return only hook files intended for *target_key*.

    Routing is based on the file stem (case-insensitive):
      - Stems ending with a known ``-<target>-hooks`` suffix are
        restricted to matching targets.
      - All other stems (e.g. ``hooks``, ``my-custom-hooks``) are
        universal and pass through for every target.

    Args:
        hook_files: All discovered hook JSON files.
        target_key: Lowercase target name (e.g. ``"claude"``, ``"cursor"``).

    Returns:
        Filtered list preserving original order.
    """
    result: list[Path] = []
    for hf in hook_files:
        stem_lower = hf.stem.lower()
        matched_suffix: str | None = None
        for suffix, allowed_targets in _HOOK_FILE_TARGET_SUFFIXES.items():
            if stem_lower == suffix or stem_lower.endswith(f"-{suffix}"):
                matched_suffix = suffix
                if target_key in allowed_targets:
                    result.append(hf)
                break
        if matched_suffix is None:
            # Universal file -- deploy to all targets
            result.append(hf)
    return result


class HookIntegrator(BaseIntegrator):
    """Handles integration of APM package hooks into target locations.

    Discovers hook JSON files and their referenced scripts from packages,
    then installs them to the appropriate target location:
    - VSCode: .github/hooks/<pkg>-<name>.json + .github/hooks/scripts/<pkg>/
    - Claude: Merged into .claude/settings.json hooks key + .claude/hooks/<pkg>/
    - Cursor: Merged into .cursor/hooks.json hooks key + .cursor/hooks/<pkg>/
    """

    # Superset of all known script-path keys across supported hook specs.
    # Every call site in _rewrite_hooks_data() iterates over this tuple,
    # so a single addition here propagates everywhere.
    #
    #   "command":    Claude Code (primary), VS Code (default/cross-platform), Cursor
    #   "bash":       GitHub Copilot Agent cloud/CLI
    #   "powershell": GitHub Copilot Agent cloud/CLI
    #   "windows":    VS Code (OS-specific override)
    #   "linux":      VS Code (OS-specific override)
    #   "osx":        VS Code (OS-specific override)
    #
    # Refs:
    #   GH Copilot Agent: https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-hooks
    #   VS Code:          https://code.visualstudio.com/docs/copilot/customization/hooks
    #   Claude Code:      https://code.claude.com/docs/en/hooks
    HOOK_COMMAND_KEYS: tuple[str, ...] = (
        "command",
        "bash",
        "powershell",
        "windows",
        "linux",
        "osx",
    )

    def find_hook_files(self, package_path: Path) -> list[Path]:
        return _filter_files.find_hook_files(self, package_path)

    def _parse_hook_json(self, hook_file: Path) -> dict | None:
        """Parse a hook JSON file and return the data dict.

        Args:
            hook_file: Path to the hook JSON file

        Returns:
            Optional[Dict]: Parsed JSON dict, or None if invalid
        """
        try:
            with open(hook_file, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return data
        except (json.JSONDecodeError, OSError):
            return None

    def _rewrite_command_for_target(
        self,
        command: str,
        package_path: Path,
        package_name: str,
        target: str,
        hook_file_dir: Path | None = None,
        root_dir: str | None = None,
    ) -> tuple[str, list[tuple[Path, str]]]:
        return _gemini_translate._rewrite_command_for_target(
            self, command, package_path, package_name, target, hook_file_dir, root_dir
        )

    def _rewrite_hooks_data(
        self,
        data: dict,
        package_path: Path,
        package_name: str,
        target: str,
        hook_file_dir: Path | None = None,
        root_dir: str | None = None,
    ) -> tuple[dict, list[tuple[Path, str]]]:
        return _gemini_translate._rewrite_hooks_data(
            self, data, package_path, package_name, target, hook_file_dir, root_dir
        )

    def _get_package_name(self, package_info) -> str:
        """Get a short package name for use in file/directory naming.

        Args:
            package_info: PackageInfo object

        Returns:
            str: Package name derived from install path
        """
        return package_info.install_path.name

    def integrate_package_hooks(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
        target=None,
    ) -> HookIntegrationResult:
        return _merge_config.integrate_package_hooks(
            self, package_info, project_root, force, managed_files, diagnostics, target
        )

    # ------------------------------------------------------------------
    # Shared JSON-merge implementation for Claude / Cursor / Codex
    # ------------------------------------------------------------------

    def _integrate_merged_hooks(
        self,
        config: "_MergeHookConfig",
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
        target=None,
    ) -> HookIntegrationResult:
        return _merge_config._integrate_merged_hooks(
            self,
            config,
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            target=target,
        )

    # ------------------------------------------------------------------
    # DEPRECATED per-target methods -- delegate to _integrate_merged_hooks
    # ------------------------------------------------------------------

    def integrate_package_hooks_claude(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> HookIntegrationResult:
        """Integrate hooks into .claude/settings.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["claude"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    def integrate_package_hooks_cursor(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> HookIntegrationResult:
        """Integrate hooks into .cursor/hooks.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["cursor"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    def integrate_package_hooks_codex(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> HookIntegrationResult:
        """Integrate hooks into .codex/hooks.json.

        .. deprecated:: Use :meth:`integrate_hooks_for_target` instead.
        """
        return self._integrate_merged_hooks(
            _MERGE_HOOK_TARGETS["codex"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------
    # Target-driven API
    # ------------------------------------------------------------------

    def integrate_hooks_for_target(
        self,
        target,
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set | None = None,
        diagnostics=None,
    ) -> "HookIntegrationResult":
        return _merge_config.integrate_hooks_for_target(
            self,
            target,
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    def sync_integration(
        self, apm_package, project_root: Path, managed_files: set | None = None, targets=None
    ) -> dict:
        return _filter_files.sync_integration(
            self, apm_package, project_root, managed_files, targets
        )

    @staticmethod
    @staticmethod
    def _clean_apm_entries_from_json(json_path: Path, stats: dict[str, int]) -> None:
        return _filter_files._clean_apm_entries_from_json(json_path, stats)


from . import filter_files as _filter_files
from . import gemini_translate as _gemini_translate
from . import merge_config as _merge_config
