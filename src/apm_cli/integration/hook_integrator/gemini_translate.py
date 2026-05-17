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

import logging
import re
from pathlib import Path

from .class_ import _MergeHookConfig

_log = logging.getLogger(__name__)
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
_HOOK_FILE_TARGET_SUFFIXES: dict[str, set[str]] = {
    "copilot-hooks": {"copilot", "vscode"},
    "cursor-hooks": {"cursor"},
    "claude-hooks": {"claude"},
    "codex-hooks": {"codex"},
    "gemini-hooks": {"gemini"},
    "windsurf-hooks": {"windsurf"},
}


def _rewrite_command_for_target(
    self,
    command: str,
    package_path: Path,
    package_name: str,
    target: str,
    hook_file_dir: Path | None = None,
    root_dir: str | None = None,
) -> tuple[str, list[tuple[Path, str]]]:
    """Rewrite a hook command to use installed script paths.

    Handles:
    - ${CLAUDE_PLUGIN_ROOT}/path references (resolved from package root)
    - ./path relative references (resolved from hook file's parent directory)
    - Windows backslash variants of both (.\\ and ${CLAUDE_PLUGIN_ROOT}\\)

    Args:
        command: Original command string
        package_path: Root path of the source package
        package_name: Name used for the scripts subdirectory
        target: "vscode" or "claude"
        hook_file_dir: Directory containing the hook JSON file (for ./path resolution)
        root_dir: Override root directory (e.g. ".copilot" for user scope)

    Returns:
        Tuple of (rewritten_command, list of (source_file, relative_target_path))
    """
    scripts_to_copy = []
    new_command = command

    if target == "vscode":
        base_root = root_dir or ".github"
        scripts_base = f"{base_root}/hooks/scripts/{package_name}"
    elif target == "cursor":
        base_root = root_dir or ".cursor"
        scripts_base = f"{base_root}/hooks/{package_name}"
    elif target == "codex":
        base_root = root_dir or ".codex"
        scripts_base = f"{base_root}/hooks/{package_name}"
    elif target == "windsurf":
        base_root = root_dir or ".windsurf"
        scripts_base = f"{base_root}/hooks/{package_name}"
    else:
        base_root = root_dir or ".claude"
        scripts_base = f"{base_root}/hooks/{package_name}"

    # Handle plugin root variable references (always relative to package root)
    # Match both forward-slash and backslash separators (Windows hook JSON
    # may use backslashes: ${CLAUDE_PLUGIN_ROOT}\scripts\scan.ps1)
    plugin_root_pattern = (
        r"\$\{(?:CLAUDE_PLUGIN_ROOT|CURSOR_PLUGIN_ROOT|PLUGIN_ROOT)\}([\\/][^\s]+)"
    )
    for match in re.finditer(plugin_root_pattern, command):
        full_var = match.group(0)
        # Normalize backslashes to forward slashes before Path construction
        # (on Unix, Path treats backslashes as literal filename chars)
        rel_path = match.group(1).replace("\\", "/").lstrip("/")

        source_file = (package_path / rel_path).resolve()
        # Reject path traversal outside the package directory
        if not source_file.is_relative_to(package_path.resolve()):
            continue
        if source_file.exists() and source_file.is_file():
            target_rel = f"{scripts_base}/{rel_path}"
            scripts_to_copy.append((source_file, target_rel))
            new_command = new_command.replace(full_var, target_rel)

    # Handle relative ./path and .\path references (safe to run after
    # ${CLAUDE_PLUGIN_ROOT} substitution since replacements produce paths
    # like ".github/..." not "./" or ".\")
    # Match both forward-slash and backslash separators (Windows hook JSON
    # may use backslashes: .\scripts\scan.ps1)
    # Resolve from hook file's directory if available, else fall back to package root
    resolve_base = hook_file_dir if hook_file_dir else package_path
    rel_pattern = r"(\.[\\/][^\s]+)"
    for match in re.finditer(rel_pattern, new_command):
        rel_ref = match.group(1)
        # Normalize to forward slashes for path resolution
        rel_path = rel_ref[2:].replace("\\", "/")

        source_file = (resolve_base / rel_path).resolve()
        # Reject path traversal outside the package directory
        if not source_file.is_relative_to(package_path.resolve()):
            continue
        if source_file.exists() and source_file.is_file():
            target_rel = f"{scripts_base}/{rel_path}"
            scripts_to_copy.append((source_file, target_rel))
            new_command = new_command.replace(rel_ref, target_rel)

    return new_command, scripts_to_copy


def _rewrite_hooks_data(
    self,
    data: dict,
    package_path: Path,
    package_name: str,
    target: str,
    hook_file_dir: Path | None = None,
    root_dir: str | None = None,
) -> tuple[dict, list[tuple[Path, str]]]:
    """Rewrite all command paths in a hooks JSON structure.

    Creates a deep copy and rewrites command paths for the target platform.

    Args:
        data: Parsed hook JSON data
        package_path: Root path of the source package
        package_name: Name for scripts subdirectory
        target: "vscode" or "claude"
        hook_file_dir: Directory containing the hook JSON file (for ./path resolution)
        root_dir: Override root directory (e.g. ".copilot" for user scope)

    Returns:
        Tuple of (rewritten_data_copy, list of (source_file, target_rel_path))
    """
    import copy

    rewritten = copy.deepcopy(data)
    all_scripts: list[tuple[Path, str]] = []

    hooks = rewritten.get("hooks", {})
    for event_name, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            # Rewrite script paths in the matcher dict itself
            # (GitHub Copilot flat format: bash/powershell/windows keys at this level)
            for key in self.HOOK_COMMAND_KEYS:
                if key in matcher:
                    new_cmd, scripts = self._rewrite_command_for_target(
                        matcher[key],
                        package_path,
                        package_name,
                        target,
                        hook_file_dir=hook_file_dir,
                        root_dir=root_dir,
                    )
                    if scripts:
                        _log.debug(
                            "Hook %s/%s: rewrote '%s' key (%d script(s))",
                            package_name,
                            event_name,
                            key,
                            len(scripts),
                        )
                    matcher[key] = new_cmd
                    all_scripts.extend(scripts)

            # Rewrite script paths in nested hooks array
            # (Claude format: matcher groups with inner hooks array)
            for hook in matcher.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                for key in self.HOOK_COMMAND_KEYS:
                    if key in hook:
                        new_cmd, scripts = self._rewrite_command_for_target(
                            hook[key],
                            package_path,
                            package_name,
                            target,
                            hook_file_dir=hook_file_dir,
                            root_dir=root_dir,
                        )
                        if scripts:
                            _log.debug(
                                "Hook %s/%s: rewrote '%s' key (%d script(s))",
                                package_name,
                                event_name,
                                key,
                                len(scripts),
                            )
                        hook[key] = new_cmd
                        all_scripts.extend(scripts)

    # De-duplicate by target path to avoid redundant copies when
    # multiple keys (e.g. command + bash) reference the same script.
    seen_targets: dict[str, Path] = {}
    for source, target_rel in all_scripts:
        if target_rel not in seen_targets:
            seen_targets[target_rel] = source
    unique_scripts = [(src, tgt) for tgt, src in seen_targets.items()]

    return rewritten, unique_scripts
