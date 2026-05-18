"""Helper functions for the ``apm init`` command."""

import os
import sys
from pathlib import Path

import click

from ..constants import APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..core.target_detection import EXPLICIT_ONLY_TARGETS, detect_signals
from ..utils.console import _create_files_table, _rich_panel
from ._helpers import (
    INFO,
    RESET,
    _get_console,
    _validate_project_name,
)

_PROMPT_TARGETS_ORDERED: list[str] = [
    "copilot",
    "claude",
    "cursor",
    "opencode",
    "codex",
    "gemini",
    "windsurf",
]


def _normalise_project_name(project_name: str | None) -> str | None:
    """Map ``.`` to the current directory sentinel used by ``apm init``."""
    return None if project_name == "." else project_name


def _prepare_project_directory(project_name: str | None, logger: CommandLogger) -> tuple[Path, str]:
    """Create and switch into the project directory when a name is provided."""
    if project_name:
        project_dir = Path(project_name)
        project_dir.mkdir(exist_ok=True)
        os.chdir(project_dir)
        logger.progress(f"Created project directory: {project_name}", symbol="folder")
        return project_dir, project_name

    project_dir = Path.cwd()
    return project_dir, project_dir.name


def _handle_existing_manifest(apm_yml_exists: bool, yes: bool, logger: CommandLogger) -> bool:
    """Return whether init should continue when ``apm.yml`` already exists."""
    if not apm_yml_exists:
        return True

    logger.warning("apm.yml already exists")
    if yes:
        logger.progress("--yes specified, overwriting apm.yml...")
        return True

    return bool(click.confirm("Continue and overwrite?"))


def _append_marketplace_block(logger: CommandLogger) -> None:
    """Append the marketplace authoring block to the local ``apm.yml``."""
    from ..marketplace.init_template import render_marketplace_block

    apm_yml_path = Path.cwd() / APM_YML_FILENAME
    try:
        existing = apm_yml_path.read_text(encoding="utf-8")
        if not existing.endswith("\n"):
            existing += "\n"
        block = render_marketplace_block()
        apm_yml_path.write_text(existing + "\n" + block, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            f"Failed to append marketplace block to apm.yml: {exc}",
            symbol="warning",
        )


def _render_created_files(plugin: bool, logger: CommandLogger) -> None:
    """Display the files created by ``apm init``."""
    try:
        console = _get_console()
        if console:
            files_data = [("*", APM_YML_FILENAME, "Project configuration")]
            if plugin:
                files_data.append(("*", "plugin.json", "Plugin metadata"))
            table = _create_files_table(files_data, title="Created Files")
            console.print(table)
            return
    except (ImportError, NameError):
        pass

    logger.progress("Created:")
    click.echo("  * apm.yml - Project configuration")
    if plugin:
        click.echo("  * plugin.json - Plugin metadata")


def _build_next_steps(plugin: bool) -> list[str]:
    """Return the next-step commands shown after init completes."""
    if plugin:
        return [
            "Add dev dependencies:    apm install --dev <owner>/<repo>",
            "Pack as plugin:          apm pack",
        ]

    return [
        "Install a skill:                apm install github/awesome-copilot/skills/documentation-writer",
        "Install a marketplace plugin:   apm install frontend-web-dev@awesome-copilot",
        "Install a versioned package:    apm install microsoft/apm-sample-package#v1.0.0",
        "Author your own plugin:         apm pack",
    ]


def _render_next_steps(plugin: bool, logger: CommandLogger) -> None:
    """Render the next-steps panel with Rich fallback handling."""
    next_steps = _build_next_steps(plugin)
    try:
        _rich_panel(
            "\n".join(f"* {step}" for step in next_steps),
            title=" Next Steps",
            style="cyan",
        )
        return
    except (ImportError, NameError):
        pass

    logger.progress("Next steps:")
    for step in next_steps:
        click.echo(f"  * {step}")


def _render_footer() -> None:
    """Render the docs/footer links shown at the end of init."""
    docs_line = "  Docs: https://microsoft.github.io/apm  |  Star: https://github.com/microsoft/apm"
    try:
        console = _get_console()
        if console:
            console.print(docs_line, style="dim")
            return
    except (ImportError, NameError):
        pass
    click.echo(docs_line)


def _interactive_project_setup(default_name, logger):
    """Interactive setup for new APM projects with auto-detection.

    Collects only the metadata fields here; target selection and final
    confirmation are run by the caller via ``_confirm_setup_summary`` so
    targets can be shown in the same "About to create" panel.
    """
    from ._helpers import _auto_detect_author, _auto_detect_description

    auto_author = _auto_detect_author()
    auto_description = _auto_detect_description(default_name)

    try:
        from rich.console import Console  # type: ignore
        from rich.prompt import Prompt  # type: ignore

        console = _get_console() or Console()
        console.print("\n[info]Setting up your APM project...[/info]")
        console.print("[muted]Press ^C at any time to quit.[/muted]\n")

        while True:
            name = Prompt.ask("Project name", default=default_name).strip()
            if _validate_project_name(name):
                break
            console.print(
                f"[error]Invalid project name '{name}': "
                "project names must not contain path separators ('/' or '\\\\') or be '..'.[/error]"
            )

        version = Prompt.ask("Version", default="1.0.0").strip()
        description = Prompt.ask("Description", default=auto_description).strip()
        author = Prompt.ask("Author", default=auto_author).strip()

    except (ImportError, NameError):
        logger.progress("Setting up your APM project...")
        logger.progress("Press ^C at any time to quit.")

        while True:
            name = click.prompt("Project name", default=default_name).strip()
            if _validate_project_name(name):
                break
            click.echo(
                f"{INFO}Invalid project name '{name}': "
                f"project names must not contain path separators ('/' or '\\\\') or be '..'.{RESET}"
            )

        version = click.prompt("Version", default="1.0.0").strip()
        description = click.prompt("Description", default=auto_description).strip()
        author = click.prompt("Author", default=auto_author).strip()

    return {
        "name": name,
        "version": version,
        "description": description,
        "author": author,
    }


def _confirm_setup_summary(config: dict, logger) -> None:
    """Render the 'About to create' panel (including targets) and confirm.

    Aborts via ``sys.exit(0)`` if the user declines.
    """
    targets = config.get("targets")
    targets_line = ", ".join(targets) if targets else "(none -- auto-detect at compile time)"

    try:
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.prompt import Confirm  # type: ignore

        console = _get_console() or Console()
        summary_content = (
            f"name: {config['name']}\n"
            f"version: {config['version']}\n"
            f"description: {config['description']}\n"
            f"author: {config['author']}\n"
            f"targets: {targets_line}"
        )
        console.print(Panel(summary_content, title="About to create", border_style="cyan"))

        if not Confirm.ask("\nIs this OK?", default=True):
            console.print("[info]Aborted.[/info]")
            sys.exit(0)
    except (ImportError, NameError):
        click.echo(f"\n{INFO}About to create:{RESET}")
        click.echo(f"  name: {config['name']}")
        click.echo(f"  version: {config['version']}")
        click.echo(f"  description: {config['description']}")
        click.echo(f"  author: {config['author']}")
        click.echo(f"  targets: {targets_line}")

        if not click.confirm("\nIs this OK?", default=True):
            logger.progress("Aborted.")
            sys.exit(0)


def _stdin_is_tty() -> bool:
    """Return whether sys.stdin is a TTY. Indirection for test patchability."""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _resolve_init_targets(
    project_root: Path,
    *,
    target_flag: str | list[str] | None,
    yes: bool,
    apm_yml_exists: bool,
    logger: CommandLogger,
) -> list[str] | None:
    """Resolve targets for init. Returns list of targets or None (auto-detect)."""
    if target_flag is not None:
        targets = [target_flag] if isinstance(target_flag, str) else list(target_flag)
        logger.progress(f"Targets set: {', '.join(targets)} (via --target flag)", symbol="info")
        return targets

    prechecked: set[str] = set()
    signal_hints: dict[str, str] = {}

    if apm_yml_exists:
        existing_targets = _read_existing_targets(project_root)
        if existing_targets:
            prechecked = set(existing_targets)
            for target in existing_targets:
                signal_hints[target] = "(from existing apm.yml)"

    if not prechecked:
        for signal in detect_signals(project_root):
            if signal.target in EXPLICIT_ONLY_TARGETS:
                continue
            prechecked.add(signal.target)
            signal_hints[signal.target] = f"(detected {signal.source})"

    is_tty = _stdin_is_tty()
    if yes or not is_tty:
        if not yes and not is_tty:
            logger.progress(
                "Non-interactive stdin: skipping target prompt "
                "(use --yes or --target to silence this notice).",
                symbol="info",
            )
        if not prechecked:
            return None
        targets = sorted(prechecked)
        sources = ", ".join(signal_hints.get(target, "") for target in targets)
        logger.progress(
            f"Auto-detected targets: {', '.join(targets)} {sources}".rstrip(),
            symbol="info",
        )
        return targets

    return _prompt_target_selection(prechecked, signal_hints)


def _parse_raw_target_field(raw: object) -> list[str]:
    """Convert a raw YAML target/targets value to a list of target strings.

    Handles None (returns []), YAML lists, and comma-separated strings.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(target).strip() for target in raw if str(target).strip()]
    return [target.strip() for target in str(raw).split(",") if target.strip()]


def _read_existing_targets(project_root: Path) -> list[str]:
    """Read targets/target field from existing apm.yml if present."""
    import yaml

    apm_yml_path = project_root / APM_YML_FILENAME
    if not apm_yml_path.exists():
        return []
    try:
        data = yaml.safe_load(apm_yml_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        raw = data.get("targets")
        if raw is not None:
            return _parse_raw_target_field(raw)
        return _parse_raw_target_field(data.get("target"))
    except Exception:
        return []


def _parse_chunk(chunk: str, max_n: int) -> tuple[list[int], str | None]:
    """Parse a single toggle chunk (number or N-M range). Returns (indices, error)."""
    if "-" in chunk:
        parts = chunk.split("-")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return [], f"Invalid range '{chunk}'. Use form 'N-M'."
        lo, hi = int(parts[0]), int(parts[1])
        if lo < 1 or hi > max_n or lo > hi:
            return [], f"Range '{chunk}' out of bounds (valid: 1-{max_n})."
        return list(range(lo - 1, hi)), None
    if not chunk.isdigit():
        return [], f"Invalid token '{chunk}'."
    number = int(chunk)
    if number < 1 or number > max_n:
        return [], f"Number {number} out of bounds (valid: 1-{max_n})."
    return [number - 1], None


def _parse_toggle_input(response: str, max_n: int) -> tuple[list[int], str | None]:
    """Parse toggle input. Returns (zero-based indices, error message or None)."""
    response = response.strip().lower().replace(" ", "")
    if not response:
        return [], None
    if response in ("all", "none"):
        return list(range(max_n)), None
    indices: list[int] = []
    for chunk in response.split(","):
        if not chunk:
            continue
        chunk_indices, err = _parse_chunk(chunk, max_n)
        if err:
            return [], err
        indices.extend(chunk_indices)
    return indices, None


def _prompt_target_selection(
    prechecked: set[str],
    signal_hints: dict[str, str],
) -> list[str] | None:
    """Interactive numbered-toggle target selection."""
    targets = [target for target in _PROMPT_TARGETS_ORDERED if target not in EXPLICIT_ONLY_TARGETS]
    selected: list[bool] = [target in prechecked for target in targets]

    def _render_choices() -> str:
        lines = []
        for index, target in enumerate(targets, start=1):
            mark = "[x]" if selected[index - 1] else "[ ]"
            hint = signal_hints.get(target, "")
            line = f"  {index}. {mark} {target}"
            if hint:
                line += f"  {hint}"
            lines.append(line)
        return "\n".join(lines)

    click.echo("\nSelect targets for this project:")
    click.echo(_render_choices())
    if not any(signal_hints.values()):
        click.echo("  (no signals detected)")

    click.echo(
        f"\n{INFO}[i] Tip: select the tools your team uses. You can change this later"
        f"\n    with 'apm targets set <target,...>' or edit apm.yml directly.{RESET}"
    )
    click.echo(
        f"{INFO}[i] Type a number to toggle, ranges like '1-3' or '1,3,5' for multiple,"
        f"\n    'all' / 'none' to flip every entry, or press Enter to confirm.{RESET}"
    )

    while True:
        response = click.prompt(
            f"Toggle (1-{len(targets)}, ranges, 'all'/'none', or Enter to confirm)",
            default="",
            show_default=False,
        )
        if not response.strip() or response.strip().lower() == "done":
            break

        indices, err = _parse_toggle_input(response, len(targets))
        if err:
            click.echo(f"  {err}")
            continue
        for idx in indices:
            selected[idx] = not selected[idx]
        click.echo(_render_choices())

    chosen = [targets[index] for index, is_selected in enumerate(selected) if is_selected]
    if chosen:
        return chosen

    click.echo(
        f"\n{INFO}[!] No targets selected. APM will auto-detect targets from your"
        "\n    filesystem on every compile (e.g. .github/ -> copilot)."
        f"\n    To pin targets later: apm targets set <target,...>{RESET}"
    )
    if click.confirm("\nContinue without pinning targets?", default=True):
        return None
    return _prompt_target_selection(prechecked, signal_hints)
