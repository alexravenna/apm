"""Script runner for APM NPM-like script execution."""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..token_manager import setup_runtime_environment


def run_script(self, script_name: str, params: dict[str, str]) -> bool:
    """Run a script from apm.yml with parameter substitution.

    Execution priority:
    1. Explicit scripts in apm.yml (takes precedence)
    2. Auto-discovered prompt files (fallback)
    3. Error if not found

    Args:
        script_name: Name of the script to run
        params: Parameters for compilation and script execution

    Returns:
        bool: True if script executed successfully
    """
    # Display script execution header
    header_lines = self.formatter.format_script_header(script_name, params)
    for line in header_lines:
        print(line)

    # Check if this is a virtual package (before loading config)
    is_virtual_package = self._is_virtual_package_reference(script_name)

    # Load apm.yml configuration (or create minimal one for virtual packages)
    config = self._load_config()
    if not config:
        if is_virtual_package:
            # Create minimal config for zero-config virtual package execution
            print("  [i]  Creating minimal apm.yml for zero-config execution...")
            self._create_minimal_config()
            config = self._load_config()
        else:
            raise RuntimeError("No apm.yml found in current directory")

    # 1. Check explicit scripts first (existing behavior - highest priority)
    scripts = config.get("scripts", {})
    if script_name in scripts:
        command = scripts[script_name]
        return self._execute_script_command(command, params)

    # 2. Auto-discover prompt file (fallback)
    discovered_prompt = self._discover_prompt_file(script_name)

    if discovered_prompt:
        # Print discovery message early to allow E2E tests to validate
        # This message appears before runtime detection, which may fail in test environments
        print(f"[i] Auto-discovered: {discovered_prompt.as_posix()}")

        # Detect runtime and generate command
        runtime = self._detect_installed_runtime()
        command = self._generate_runtime_command(runtime, discovered_prompt)

        # Execute with existing logic
        return self._execute_script_command(command, params)

    # 2.5 Try auto-install if it looks like a virtual package reference
    if self._is_virtual_package_reference(script_name):
        print(f"\n Auto-installing virtual package: {script_name}")
        if self._auto_install_virtual_package(script_name):
            # Retry discovery after install
            discovered_prompt = self._discover_prompt_file(script_name)
            if discovered_prompt:
                # Signal successful install before attempting runtime detection
                # This allows E2E tests to validate auto-install without requiring runtime
                print("\n* Package installed and ready to run\n")
                runtime = self._detect_installed_runtime()
                command = self._generate_runtime_command(runtime, discovered_prompt)
                return self._execute_script_command(command, params)
            else:
                raise RuntimeError(
                    f"Package installed successfully but prompt not found.\n"
                    f"The package may not contain the expected prompt file.\n"
                    f"Check {Path('apm_modules')} for installed files."
                )

    # 3. Not found anywhere
    available = ", ".join(scripts.keys()) if scripts else "none"

    # Build helpful error message
    error_msg = f"Script or prompt '{script_name}' not found.\n"
    error_msg += f"Available scripts in apm.yml: {available}\n"
    error_msg += "\nTo find available prompts, check:\n"
    error_msg += "  - Local: .apm/prompts/, .github/prompts/, or project root\n"
    error_msg += "  - Dependencies: apm_modules/*/.apm/prompts/\n"
    error_msg += "\nOr install a prompt package:\n"
    error_msg += "  apm install <owner>/<repo>/path/to/prompt.prompt.md\n"

    raise RuntimeError(error_msg)


def _execute_script_command(self, command: str, params: dict[str, str]) -> bool:
    """Execute a script command (from apm.yml or auto-generated).

    This is the existing run_script logic, extracted for reuse.

    Args:
        command: Script command to execute
        params: Parameters for compilation and script execution

    Returns:
        bool: True if script executed successfully
    """

    # Auto-compile any .prompt.md files in the command
    compiled_command, compiled_prompt_files, runtime_content = self._auto_compile_prompts(
        command, params
    )

    # Show compilation progress if needed
    if compiled_prompt_files:
        compilation_lines = self.formatter.format_compilation_progress(compiled_prompt_files)
        for line in compilation_lines:
            print(line)

    # Detect runtime and show execution details
    runtime = self._detect_runtime(compiled_command)

    # Execute the final command
    if runtime_content is not None:
        # Show runtime execution details
        execution_lines = self.formatter.format_runtime_execution(
            runtime, compiled_command, len(runtime_content)
        )
        for line in execution_lines:
            print(line)

        # Show content preview
        preview_lines = self.formatter.format_content_preview(runtime_content)
        for line in preview_lines:
            print(line)

    try:
        # Set up GitHub token environment for all runtimes using centralized manager
        env = setup_runtime_environment(os.environ.copy())

        # Show environment setup if relevant
        env_vars_set = []
        if env.get("GITHUB_TOKEN"):
            env_vars_set.append("GITHUB_TOKEN")
        if env.get("GITHUB_APM_PAT"):
            env_vars_set.append("GITHUB_APM_PAT")

        if env_vars_set:
            env_lines = self.formatter.format_environment_setup(runtime, env_vars_set)
            for line in env_lines:
                print(line)

        # Track execution time
        start_time = time.time()

        # Check if this command needs subprocess execution (has compiled content)
        if runtime_content is not None:
            # Use argument list approach for all runtimes to avoid shell parsing issues
            result = self._execute_runtime_command(compiled_command, runtime_content, env)
        else:
            # Use regular shell execution for other commands
            # (shell=True works cross-platform: bash on Unix, cmd.exe on Windows)
            result = subprocess.run(compiled_command, shell=True, check=True, env=env)

        execution_time = time.time() - start_time

        # Show success message
        success_lines = self.formatter.format_execution_success(runtime, execution_time)
        for line in success_lines:
            print(line)

        return result.returncode == 0

    except subprocess.CalledProcessError as e:
        execution_time = time.time() - start_time

        # Show error message
        error_lines = self.formatter.format_execution_error(runtime, e.returncode)
        for line in error_lines:
            print(line)

        raise RuntimeError(f"Script execution failed with exit code {e.returncode}")  # noqa: B904


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


def _detect_runtime(self, command: str) -> str:
    """Detect which runtime is being used in the command.

    Args:
        command: The command to analyze

    Returns:
        Name of the detected runtime (copilot, codex, llm, gemini, or unknown)
    """
    command_lower = command.lower().strip()
    if re.search(r"(?:^|\s)copilot(?:\s|$)", command_lower):
        return "copilot"
    elif re.search(r"(?:^|\s)codex(?:\s|$)", command_lower):
        return "codex"
    elif re.search(r"(?:^|\s)llm(?:\s|$)", command_lower):
        return "llm"
    elif re.search(r"(?:^|\s)gemini(?:\s|$)", command_lower):
        return "gemini"
    else:
        return "unknown"


def _execute_runtime_command(
    self, command: str, content: str, env: dict
) -> subprocess.CompletedProcess:
    """Execute a runtime command using subprocess argument list to avoid shell parsing issues.

    Args:
        command: The simplified runtime command (without content)
        content: The compiled prompt content to pass to the runtime
        env: Environment variables

    Returns:
        subprocess.CompletedProcess: The result of the command execution
    """
    import shlex

    package_module = sys.modules[__package__]

    # Parse the command into arguments
    if package_module.sys.platform == "win32":
        # On Windows, use posix=False to preserve Windows quoting semantics
        # (e.g., paths with spaces, quoted arguments like --model "gpt-4o mini")
        args = shlex.split(command.strip(), posix=False)
    else:
        args = shlex.split(command.strip())

    # Handle environment variables at the beginning of the command
    # Extract environment variables (key=value pairs) from the beginning of args
    env_vars = env.copy()  # Start with existing environment
    actual_command_args = []

    for arg in args:
        if "=" in arg and not actual_command_args:
            # This looks like an environment variable and we haven't started the actual command yet
            key, value = arg.split("=", 1)
            # Validate environment variable name with restrictive pattern
            # Only allow uppercase letters, numbers, and underscores, starting with letter or underscore
            if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", key):
                env_vars[key] = value
                continue
        # Once we hit a non-env-var argument, everything else is part of the command
        actual_command_args.append(arg)

    # Determine how to pass content based on runtime
    runtime = self._detect_runtime(" ".join(actual_command_args))

    if runtime == "copilot":
        # Copilot uses -p flag
        actual_command_args.extend(["-p", content])
    elif runtime == "codex":
        # Codex exec expects content as the last argument
        actual_command_args.append(content)
    elif runtime == "llm":
        # LLM expects content as argument
        actual_command_args.append(content)
    elif runtime == "gemini":
        # Gemini uses -p flag for prompt content
        actual_command_args.extend(["-p", content])
    else:
        # Default: assume content as last argument
        actual_command_args.append(content)

    # Show subprocess details for debugging
    subprocess_lines = self.formatter.format_subprocess_details(
        actual_command_args[:-1], len(content)
    )
    for line in subprocess_lines:
        print(line)

    # Show environment variables if any were extracted
    if len(env_vars) > len(env):
        extracted_env_vars = []
        for key, value in env_vars.items():
            if key not in env:
                extracted_env_vars.append(f"{key}={value}")
        if extracted_env_vars:
            env_lines = self.formatter.format_environment_setup("command", extracted_env_vars)
            for line in env_lines:
                print(line)

    # Execute using argument list (no shell interpretation) with updated environment
    # On Windows, resolve the executable via shutil.which() so that shell
    # wrappers like copilot.cmd / copilot.ps1 are found without shell=True.
    if package_module.sys.platform == "win32" and actual_command_args:
        resolved = package_module.shutil.which(actual_command_args[0])
        if resolved:
            actual_command_args[0] = resolved
    return package_module.subprocess.run(actual_command_args, check=True, env=env_vars)


def _detect_installed_runtime(self) -> str:
    """Detect installed runtime with priority order.

    Priority: copilot > codex > gemini > error

    Returns:
        Name of detected runtime

    Raises:
        RuntimeError: If no compatible runtime is found
    """
    import shutil

    if shutil.which("copilot"):
        return "copilot"
    elif shutil.which("codex"):
        return "codex"
    elif shutil.which("gemini"):
        return "gemini"
    else:
        raise RuntimeError(
            "No compatible runtime found.\n"
            "Install GitHub Copilot CLI with:\n"
            "  apm runtime setup copilot\n"
            "Or install Codex CLI with:\n"
            "  apm runtime setup codex\n"
            "Or install Gemini CLI with:\n"
            "  apm runtime setup gemini"
        )


def _generate_runtime_command(self, runtime: str, prompt_file: Path) -> str:
    """Generate appropriate runtime command with proper defaults.

    Args:
        runtime: Name of runtime (copilot or codex)
        prompt_file: Path to the prompt file

    Returns:
        Full command string with runtime-specific defaults
    """
    if runtime == "copilot":
        return f"copilot --log-level all --log-dir copilot-logs --allow-all-tools -p {prompt_file}"
    elif runtime == "codex":
        return f"codex -s workspace-write --skip-git-repo-check {prompt_file}"
    elif runtime == "gemini":
        return f"gemini -p {prompt_file}"
    else:
        raise ValueError(f"Unsupported runtime: {runtime}")
