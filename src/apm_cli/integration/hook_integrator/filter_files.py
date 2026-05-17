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


def find_hook_files(self, package_path: Path) -> list[Path]:
    """Find all hook JSON files in a package.

    Searches in:
    - .apm/hooks/ subdirectory (APM convention)
    - hooks/ subdirectory (Claude-native convention)

    Args:
        package_path: Path to the package directory

    Returns:
        List[Path]: List of absolute paths to hook JSON files
    """
    hook_files = []
    seen = set()

    # Search in .apm/hooks/ (APM convention)
    apm_hooks = package_path / ".apm" / "hooks"
    if apm_hooks.exists():
        for f in sorted(apm_hooks.glob("*.json")):
            if f.is_symlink():
                continue
            resolved = f.resolve()
            if resolved not in seen:
                seen.add(resolved)
                hook_files.append(f)

    # Search in hooks/ (Claude-native convention)
    hooks_dir = package_path / "hooks"
    if hooks_dir.exists():
        for f in sorted(hooks_dir.glob("*.json")):
            if f.is_symlink():
                continue
            resolved = f.resolve()
            if resolved not in seen:
                seen.add(resolved)
                hook_files.append(f)

    return hook_files


def sync_integration(
    self,
    apm_package,
    project_root: Path,
    managed_files: set | None = None,
    targets=None,
) -> dict:
    """Remove APM-managed hook files.

    Uses *managed_files* (relative paths) to surgically remove only
    APM-tracked files.  Falls back to legacy ``*-apm.json`` glob when
    *managed_files* is ``None``.

    **Never** calls ``shutil.rmtree``.

    Also cleans APM entries from merged-hook JSON files via the
    ``_apm_source`` marker.
    """
    from ..targets import KNOWN_TARGETS

    stats: dict[str, int] = {"files_removed": 0, "errors": 0}

    # Derive hook prefixes dynamically from targets
    source = targets if targets is not None else list(KNOWN_TARGETS.values())
    hook_prefixes = []
    for t in source:
        if t.supports("hooks"):
            sm = t.primitives["hooks"]
            effective_root = sm.deploy_root or t.root_dir
            hook_prefixes.append(f"{effective_root}/hooks/")
    hook_prefix_tuple = tuple(hook_prefixes)

    if managed_files is not None:
        # Manifest-based removal -- only remove tracked files
        deleted: list = []
        for rel_path in managed_files:
            normalized = rel_path.replace("\\", "/")
            if not normalized.startswith(hook_prefix_tuple):
                continue
            if ".." in rel_path:
                continue
            target_file = project_root / rel_path
            if target_file.exists() and target_file.is_file():
                try:
                    target_file.unlink()
                    stats["files_removed"] += 1
                    deleted.append(target_file)
                except Exception:
                    stats["errors"] += 1
        # Batch parent cleanup -- single bottom-up pass
        self.cleanup_empty_parents(deleted, stop_at=project_root)
    else:
        # Legacy fallback  -- glob for old -apm suffix files
        hooks_dir = project_root / ".github" / "hooks"
        if hooks_dir.exists():
            for hook_file in hooks_dir.glob("*-apm.json"):
                try:
                    hook_file.unlink()
                    stats["files_removed"] += 1
                except Exception:
                    stats["errors"] += 1

    # Clean APM entries from merged-hook JSON configs (uses _apm_source marker)
    for t in source:
        config = _MERGE_HOOK_TARGETS.get(t.name)
        if config is not None:
            json_path = project_root / t.root_dir / config.config_filename
            if t.name == "claude":
                # Claude uses settings.json with special structure
                if json_path.exists():
                    try:
                        with open(json_path, encoding="utf-8") as f:
                            settings = json.load(f)

                        if "hooks" in settings:
                            modified = False
                            for event_name in list(settings["hooks"].keys()):
                                matchers = settings["hooks"][event_name]
                                if isinstance(matchers, list):
                                    filtered = [
                                        m
                                        for m in matchers
                                        if not (isinstance(m, dict) and "_apm_source" in m)
                                    ]
                                    if len(filtered) != len(matchers):
                                        modified = True
                                    settings["hooks"][event_name] = filtered
                                    if not filtered:
                                        del settings["hooks"][event_name]

                            if not settings["hooks"]:
                                del settings["hooks"]

                            if modified:
                                with open(json_path, "w", encoding="utf-8") as f:
                                    json.dump(settings, f, indent=2)
                                    f.write("\n")
                                stats["files_removed"] += 1
                    except (json.JSONDecodeError, OSError):
                        stats["errors"] += 1
            else:
                self._clean_apm_entries_from_json(json_path, stats)

    return stats


def _clean_apm_entries_from_json(json_path: Path, stats: dict[str, int]) -> None:
    """Remove APM-tagged entries from a hooks JSON file.

    Filters out entries with ``_apm_source`` markers and cleans up
    empty event arrays and the ``hooks`` key itself.
    """
    if not json_path.exists():
        return
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if "hooks" not in data:
            return

        modified = False
        for event_name in list(data["hooks"].keys()):
            entries = data["hooks"][event_name]
            if isinstance(entries, list):
                filtered = [e for e in entries if not (isinstance(e, dict) and "_apm_source" in e)]
                if len(filtered) != len(entries):
                    modified = True
                data["hooks"][event_name] = filtered
                if not filtered:
                    del data["hooks"][event_name]

        if not data["hooks"]:
            del data["hooks"]

        if modified:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            stats["files_removed"] += 1
    except (json.JSONDecodeError, OSError):
        stats["errors"] += 1
