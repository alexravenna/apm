"""Command-building helpers for the APM script runner.

This private module contains the prompt-compilation and per-runtime
command-transformation logic that was previously embedded in runtime.py.
All public symbols from this module are re-exported through runtime.py so
that class_.py can continue to call them via ``_runtime.<name>``.
"""

import re

# ---------------------------------------------------------------------------
# Prompt compilation
# ---------------------------------------------------------------------------


def _auto_compile_prompts(self, command: str, params: dict[str, str]) -> tuple[str, list[str], str]:
    """Auto-compile .prompt.md files and transform runtime commands.

    Args:
        command: Original script command
        params: Parameters for compilation

    Returns:
        Tuple of (compiled_command, list_of_compiled_prompt_files, runtime_content_or_none)
    """
    # Find all .prompt.md files in the command using regex
    prompt_files = re.findall(r"(\S+\.prompt\.md)", command)
    compiled_prompt_files = []
    runtime_content = None

    compiled_command = command
    for prompt_file in prompt_files:
        # Compile the prompt file with current params
        compiled_path = self.compiler.compile(prompt_file, params)
        compiled_prompt_files.append(prompt_file)

        # Read the compiled content
        with open(compiled_path, encoding="utf-8") as f:
            compiled_content = f.read().strip()

        # Check if this is a runtime command before transformation
        is_runtime_cmd = any(
            re.search(r"(?:^|\s)" + runtime + r"(?:\s|$)", command)
            for runtime in ["copilot", "codex", "llm", "gemini"]
        ) and re.search(re.escape(prompt_file), command)

        # Transform command based on runtime pattern
        compiled_command = self._transform_runtime_command(
            compiled_command, prompt_file, compiled_content, compiled_path
        )

        # Store content for runtime commands that need subprocess execution
        if is_runtime_cmd:
            runtime_content = compiled_content

    return compiled_command, compiled_prompt_files, runtime_content


# ---------------------------------------------------------------------------
# Command transformation
# ---------------------------------------------------------------------------


def _transform_runtime_command(
    self, command: str, prompt_file: str, compiled_content: str, compiled_path: str
) -> str:
    """Transform runtime commands to their proper execution format.

    Dispatches to per-runtime builders after extracting arguments
    around the prompt file reference.

    Args:
        command: Original command
        prompt_file: Original .prompt.md file path
        compiled_content: Compiled prompt content as string
        compiled_path: Path to compiled .txt file

    Returns:
        Transformed command for proper runtime execution
    """
    # Handle environment variables prefix (e.g., "ENV1=val1 ENV2=val2 codex [args] file.prompt.md")
    # More robust approach: split by runtime commands to separate env vars from command
    runtime_commands = ["codex", "copilot", "llm", "gemini"]

    # Try matching with env-var prefix (e.g. "ENV=val codex args file.prompt.md")
    for runtime_cmd in runtime_commands:
        runtime_pattern = f" {runtime_cmd} "
        if runtime_pattern in command and re.search(re.escape(prompt_file), command):
            parts = command.split(runtime_pattern, 1)
            potential_env_part = parts[0]
            runtime_part = runtime_cmd + " " + parts[1]

            if "=" in potential_env_part and not potential_env_part.startswith(runtime_cmd):
                result = self._parse_and_build_runtime_command(
                    runtime_cmd,
                    runtime_part,
                    prompt_file,
                    env_prefix=potential_env_part,
                )
                if result is not None:
                    return result

    # Try individual runtime patterns without environment variables
    for runtime_cmd in runtime_commands:
        if re.search(r"^" + runtime_cmd + r"\s+.*" + re.escape(prompt_file), command):
            result = self._parse_and_build_runtime_command(
                runtime_cmd,
                command,
                prompt_file,
            )
            if result is not None:
                return result

    # Handle bare "file.prompt.md" -> "codex exec" (default to codex)
    if command.strip() == prompt_file:
        return "codex exec"

    # Fallback: just replace file path with compiled path (for non-runtime commands)
    return command.replace(prompt_file, compiled_path)


def _parse_and_build_runtime_command(
    self,
    runtime_cmd: str,
    command_part: str,
    prompt_file: str,
    env_prefix: str | None = None,
) -> str | None:
    """Parse arguments around the prompt file and delegate to a per-runtime builder.

    Args:
        runtime_cmd: Runtime name (codex, copilot, llm, or gemini)
        command_part: The command portion containing the runtime invocation
        prompt_file: The .prompt.md filename to strip
        env_prefix: Optional environment variable prefix (e.g. "DEBUG=1")

    Returns:
        Transformed command string, or None if the pattern does not match
    """
    match = re.search(
        f"{runtime_cmd}\\s+(.*?)(" + re.escape(prompt_file) + r")(.*?)$",
        command_part,
    )
    if not match:
        return None

    args_before = match.group(1).strip()
    args_after = match.group(3).strip()

    # In the env-var path, non-codex runtimes strip -p flags (matches
    # original behaviour where copilot and llm shared an else branch).
    if env_prefix is not None and runtime_cmd != "codex":
        args_before = args_before.replace("-p", "").strip()

    builders = {
        "codex": self._build_codex_command,
        "copilot": self._build_copilot_command,
        "llm": self._build_llm_command,
        "gemini": self._build_gemini_command,
    }
    builder = builders.get(runtime_cmd)
    if builder:
        return builder(args_before, args_after, env_prefix)
    return None


# ---------------------------------------------------------------------------
# Per-runtime command builders
# ---------------------------------------------------------------------------


def _build_codex_command(
    self,
    args_before: str,
    args_after: str,
    env_prefix: str | None = None,
) -> str:
    """Build a codex command from parsed arguments.

    Args:
        args_before: Arguments that appeared before the prompt file
        args_after: Arguments that appeared after the prompt file
        env_prefix: Optional environment variable prefix

    Returns:
        Assembled codex command string
    """
    prefix = f"{env_prefix} " if env_prefix else ""
    result = f"{prefix}codex exec"
    if args_before:
        result += f" {args_before}"
    if args_after:
        result += f" {args_after}"
    return result


def _build_copilot_command(
    self,
    args_before: str,
    args_after: str,
    env_prefix: str | None = None,
) -> str:
    """Build a copilot command from parsed arguments.

    Removes any existing -p flag since content is passed separately
    during execution.

    Args:
        args_before: Arguments that appeared before the prompt file
        args_after: Arguments that appeared after the prompt file
        env_prefix: Optional environment variable prefix

    Returns:
        Assembled copilot command string
    """
    prefix = f"{env_prefix} " if env_prefix else ""
    result = f"{prefix}copilot"
    if args_before:
        # Remove any existing -p flag since we handle it in execution
        cleaned_args = args_before.replace("-p", "").strip()
        if cleaned_args:
            result += f" {cleaned_args}"
    if args_after:
        result += f" {args_after}"
    return result


def _build_llm_command(
    self,
    args_before: str,
    args_after: str,
    env_prefix: str | None = None,
) -> str:
    """Build an llm command from parsed arguments.

    Args:
        args_before: Arguments that appeared before the prompt file
        args_after: Arguments that appeared after the prompt file
        env_prefix: Optional environment variable prefix

    Returns:
        Assembled llm command string
    """
    prefix = f"{env_prefix} " if env_prefix else ""
    result = f"{prefix}llm"
    if args_before:
        result += f" {args_before}"
    if args_after:
        result += f" {args_after}"
    return result


def _build_gemini_command(
    self,
    args_before: str,
    args_after: str,
    env_prefix: str | None = None,
) -> str:
    """Build a gemini command from parsed arguments.

    Args:
        args_before: Arguments that appeared before the prompt file
        args_after: Arguments that appeared after the prompt file
        env_prefix: Optional environment variable prefix

    Returns:
        Assembled gemini command string
    """
    prefix = f"{env_prefix} " if env_prefix else ""
    result = f"{prefix}gemini"
    if args_before:
        cleaned_args = re.sub(r"(^|\s)-p(?=\s|$)", "", args_before).strip()
        if cleaned_args:
            result += f" {cleaned_args}"
    if args_after:
        result += f" {args_after}"
    return result
