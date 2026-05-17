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
import shutil
from pathlib import Path

from apm_cli.utils.path_security import ensure_path_within
from apm_cli.utils.paths import portable_relpath

from .class_ import (
    HookIntegrationResult,
    _filter_hook_files_for_target,
    _MergeHookConfig,
    _to_gemini_hook_entries,
)

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
    """Integrate hooks by merging into a target-specific JSON config.

    This is the shared implementation for Claude, Cursor, and Codex
    targets that merge hook entries into a single JSON file (as
    opposed to Copilot which uses individual JSON files).
    """
    _empty = HookIntegrationResult(
        files_integrated=0,
        files_updated=0,
        files_skipped=0,
        target_paths=[],
    )

    root_dir = target.root_dir if target else f".{config.target_key}"
    target_dir = project_root / root_dir

    # Opt-in check: some targets only deploy when their dir exists
    if config.require_dir and not target_dir.exists():
        return _empty

    hook_files = self.find_hook_files(package_info.install_path)
    hook_files = _filter_hook_files_for_target(hook_files, config.target_key)
    if not hook_files:
        return _empty

    package_name = self._get_package_name(package_info)
    hooks_integrated = 0
    scripts_copied = 0
    scripts_adopted = 0
    target_paths: list[Path] = []
    # Events whose prior-owned entries have already been cleared on
    # this install run. Packages can contribute to the same event
    # from multiple hook files -- we must only strip once so earlier
    # files' fresh entries aren't wiped by later iterations.
    cleared_events: set = set()

    # Read existing JSON config
    json_path = target_dir / config.config_filename
    json_config: dict = {}
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                json_config = json.load(f)
        except (json.JSONDecodeError, OSError):
            json_config = {}

    if "hooks" not in json_config:
        json_config["hooks"] = {}

    for hook_file in hook_files:
        data = self._parse_hook_json(hook_file)
        if data is None:
            continue

        # Rewrite script paths for the target
        rewritten, scripts = self._rewrite_hooks_data(
            data,
            package_info.install_path,
            package_name,
            config.target_key,
            hook_file_dir=hook_file.parent,
            root_dir=root_dir,
        )

        # Merge hooks into config (additive)
        hooks = rewritten.get("hooks", {})
        event_map = _HOOK_EVENT_MAP.get(config.target_key, {})

        # Build reverse map: normalised name -> set of source aliases
        reverse_map: dict[str, set[str]] = {}
        for source_name, norm_name in event_map.items():
            reverse_map.setdefault(norm_name, set()).add(source_name)

        for raw_event_name, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            event_name = event_map.get(raw_event_name, raw_event_name)
            if event_name not in json_config["hooks"]:
                json_config["hooks"][event_name] = []

            # Transform flat Copilot entries to Gemini nested format
            if config.target_key == "gemini":
                entries = _to_gemini_hook_entries(entries)

            # Mark each entry with APM source for sync/cleanup
            for entry in entries:
                if isinstance(entry, dict):
                    entry["_apm_source"] = package_name

            # Idempotent upsert: drop any prior entries owned by this
            # package before appending fresh ones. Without this, every
            # `apm install` re-run duplicates the package's hooks
            # because `.extend()` is unconditional. See microsoft/apm#708.
            # Only strip once per event per install run -- a package
            # with multiple hook files targeting the same event
            # contributes each file's entries in turn, and stripping
            # on every iteration would erase earlier files' work.
            if event_name not in cleared_events:
                # Clear from the normalised event
                json_config["hooks"][event_name] = [
                    e
                    for e in json_config["hooks"][event_name]
                    if not (isinstance(e, dict) and e.get("_apm_source") == package_name)
                ]
                # Also clear from any alias events that map to
                # this normalised name (handles migration from
                # corrupted installs with mixed-case event keys).
                for alias in reverse_map.get(event_name, set()):
                    if alias != event_name and alias in json_config["hooks"]:
                        json_config["hooks"][alias] = [
                            e
                            for e in json_config["hooks"][alias]
                            if not (isinstance(e, dict) and e.get("_apm_source") == package_name)
                        ]
                        # Remove the alias key entirely if now empty
                        if not json_config["hooks"][alias]:
                            del json_config["hooks"][alias]
                cleared_events.add(event_name)
            json_config["hooks"][event_name].extend(entries)

            # Deduplicate same-package entries by content.
            # Safety net for edge cases where multiple source files
            # produce semantically identical entries.
            seen_content: list[dict] = []
            deduped: list = []
            for entry in json_config["hooks"][event_name]:
                if not isinstance(entry, dict):
                    deduped.append(entry)
                    continue
                # Build comparison key (all fields except _apm_source)
                cmp = {k: v for k, v in sorted(entry.items()) if k != "_apm_source"}
                source = entry.get("_apm_source")
                is_dup = False
                for seen in seen_content:
                    if seen.get("_source") == source and seen.get("_cmp") == cmp:
                        is_dup = True
                        break
                if not is_dup:
                    seen_content.append({"_source": source, "_cmp": cmp})
                    deduped.append(entry)
            json_config["hooks"][event_name] = deduped

        hooks_integrated += 1

        # Copy referenced scripts
        for source_file, target_rel in scripts:
            target_script = project_root / target_rel
            ensure_path_within(target_script, project_root)
            if self.is_content_identical_to_source(target_script, source_file):
                target_paths.append(target_script)
                scripts_adopted += 1
                continue
            if self.check_collision(
                target_script,
                target_rel,
                managed_files,
                force,
                diagnostics=diagnostics,
            ):
                continue
            target_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_script)
            scripts_copied += 1
            target_paths.append(target_script)

    # Write JSON config back
    # Don't track the config file in target_paths -- it's a shared
    # file cleaned via _apm_source markers, not file-level deletion
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_config, f, indent=2)
        f.write("\n")

    return HookIntegrationResult(
        files_integrated=hooks_integrated,
        files_updated=0,
        files_skipped=0,
        target_paths=target_paths,
        scripts_copied=scripts_copied,
        files_adopted=scripts_adopted,
    )


def integrate_package_hooks(
    self,
    package_info,
    project_root: Path,
    force: bool = False,
    managed_files: set | None = None,
    diagnostics=None,
    target=None,
) -> HookIntegrationResult:
    """Integrate hooks from a package into hooks dir (Copilot target).

    Deploys hook JSON files with clean filenames and copies referenced
    script files. Skips user-authored files unless force=True.

    Args:
        package_info: PackageInfo with package metadata and install path
        project_root: Root directory of the project
        force: If True, overwrite user-authored files on collision
        managed_files: Set of relative paths known to be APM-managed
        target: Optional TargetProfile for scope-resolved root_dir

    Returns:
        HookIntegrationResult: Results of the integration operation
    """
    hook_files = self.find_hook_files(package_info.install_path)
    hook_files = _filter_hook_files_for_target(hook_files, "copilot")

    if not hook_files:
        return HookIntegrationResult(
            files_integrated=0,
            files_updated=0,
            files_skipped=0,
            target_paths=[],
        )

    root_dir = target.root_dir if target else ".github"
    hooks_dir = project_root / root_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    package_name = self._get_package_name(package_info)
    hooks_integrated = 0
    scripts_copied = 0
    scripts_adopted = 0
    target_paths: list[Path] = []

    for hook_file in hook_files:
        data = self._parse_hook_json(hook_file)
        if data is None:
            continue

        # Rewrite script paths for VSCode target
        rewritten, scripts = self._rewrite_hooks_data(
            data,
            package_info.install_path,
            package_name,
            "vscode",
            hook_file_dir=hook_file.parent,
            root_dir=root_dir,
        )

        # Generate target filename (clean, no -apm suffix)
        stem = hook_file.stem
        target_filename = f"{package_name}-{stem}.json"
        target_path = hooks_dir / target_filename
        rel_path = portable_relpath(target_path, project_root)

        if self.check_collision(
            target_path, rel_path, managed_files, force, diagnostics=diagnostics
        ):
            continue

        # Write rewritten JSON
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(rewritten, f, indent=2)
            f.write("\n")

        hooks_integrated += 1
        target_paths.append(target_path)

        # Copy referenced scripts (individual file tracking)
        for source_file, target_rel in scripts:
            target_script = project_root / target_rel
            ensure_path_within(target_script, project_root)
            if self.is_content_identical_to_source(target_script, source_file):
                target_paths.append(target_script)
                scripts_adopted += 1
                continue
            if self.check_collision(
                target_script, target_rel, managed_files, force, diagnostics=diagnostics
            ):
                continue
            target_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_script)
            scripts_copied += 1
            target_paths.append(target_script)

    return HookIntegrationResult(
        files_integrated=hooks_integrated,
        files_updated=0,
        files_skipped=0,
        target_paths=target_paths,
        scripts_copied=scripts_copied,
        files_adopted=scripts_adopted,
    )


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
    """Integrate hooks for a single *target*.

    Copilot uses individual JSON files (genuinely different pattern).
    All other merge-based targets are dispatched via the
    ``_MERGE_HOOK_TARGETS`` registry.
    """
    if target.name == "copilot":
        return self.integrate_package_hooks(
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            target=target,
        )

    config = _MERGE_HOOK_TARGETS.get(target.name)
    if config is not None:
        return self._integrate_merged_hooks(
            config,
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            target=target,
        )

    return HookIntegrationResult(
        files_integrated=0,
        files_updated=0,
        files_skipped=0,
        target_paths=[],
    )
