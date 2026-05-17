"""Script runner for APM NPM-like script execution."""

import subprocess
from pathlib import Path

from ...output.script_formatters import ScriptExecutionFormatter


class ScriptRunner:
    """Executes APM scripts with auto-compilation of .prompt.md files."""

    def __init__(self, compiler=None, use_color: bool = True):
        """Initialize script runner with optional compiler.

        Args:
            compiler: Optional prompt compiler instance
            use_color: Whether to use colored output
        """
        self.compiler = compiler or PromptCompiler()
        self.formatter = ScriptExecutionFormatter(use_color=use_color)

    def run_script(self, script_name: str, params: dict[str, str]) -> bool:
        return _runtime.run_script(self, script_name, params)

    def _execute_script_command(self, command: str, params: dict[str, str]) -> bool:
        return _runtime._execute_script_command(self, command, params)

    def list_scripts(self) -> dict[str, str]:
        """List all available scripts from apm.yml.

        Returns:
            Dict mapping script names to their commands
        """
        config = self._load_config()
        return config.get("scripts", {}) if config else {}

    def _load_config(self) -> dict | None:
        """Load apm.yml from current directory."""
        config_path = Path("apm.yml")
        if not config_path.exists():
            return None

        from ...utils.yaml_io import load_yaml

        return load_yaml(config_path)

    def _auto_compile_prompts(
        self, command: str, params: dict[str, str]
    ) -> tuple[str, list[str], str]:
        return _runtime._auto_compile_prompts(self, command, params)

    def _transform_runtime_command(
        self, command: str, prompt_file: str, compiled_content: str, compiled_path: str
    ) -> str:
        return _runtime._transform_runtime_command(
            self, command, prompt_file, compiled_content, compiled_path
        )

    def _parse_and_build_runtime_command(
        self, runtime_cmd: str, command_part: str, prompt_file: str, env_prefix: str | None = None
    ) -> str | None:
        return _runtime._parse_and_build_runtime_command(
            self, runtime_cmd, command_part, prompt_file, env_prefix
        )

    def _build_codex_command(
        self, args_before: str, args_after: str, env_prefix: str | None = None
    ) -> str:
        return _runtime._build_codex_command(self, args_before, args_after, env_prefix)

    def _build_copilot_command(
        self, args_before: str, args_after: str, env_prefix: str | None = None
    ) -> str:
        return _runtime._build_copilot_command(self, args_before, args_after, env_prefix)

    def _build_llm_command(
        self, args_before: str, args_after: str, env_prefix: str | None = None
    ) -> str:
        return _runtime._build_llm_command(self, args_before, args_after, env_prefix)

    def _build_gemini_command(
        self, args_before: str, args_after: str, env_prefix: str | None = None
    ) -> str:
        return _runtime._build_gemini_command(self, args_before, args_after, env_prefix)

    def _detect_runtime(self, command: str) -> str:
        return _runtime._detect_runtime(self, command)

    def _execute_runtime_command(
        self, command: str, content: str, env: dict
    ) -> subprocess.CompletedProcess:
        return _runtime._execute_runtime_command(self, command, content, env)

    def _discover_prompt_file(self, name: str) -> Path | None:
        return _prompts._discover_prompt_file(self, name)

    def _discover_qualified_prompt(self, qualified_path: str) -> Path | None:
        return _prompts._discover_qualified_prompt(self, qualified_path)

    def _matches_qualified_path(self, prompt_path: Path, qualified_path: str) -> bool:
        return _prompts._matches_qualified_path(self, prompt_path, qualified_path)

    def _handle_prompt_collision(self, name: str, matches: list[Path]) -> None:
        return _prompts._handle_prompt_collision(self, name, matches)

    def _is_virtual_package_reference(self, name: str) -> bool:
        return _prompts._is_virtual_package_reference(self, name)

    def _auto_install_virtual_package(self, package_ref: str) -> bool:
        return _prompts._auto_install_virtual_package(self, package_ref)

    def _add_dependency_to_config(self, package_ref: str) -> None:
        return _prompts._add_dependency_to_config(self, package_ref)

    def _create_minimal_config(self) -> None:
        return _prompts._create_minimal_config(self)

    def _detect_installed_runtime(self) -> str:
        return _runtime._detect_installed_runtime(self)

    def _generate_runtime_command(self, runtime: str, prompt_file: Path) -> str:
        return _runtime._generate_runtime_command(self, runtime, prompt_file)


class PromptCompiler:
    """Compiles .prompt.md files with parameter substitution."""

    DEFAULT_COMPILED_DIR = Path(".apm/compiled")

    def __init__(self):
        """Initialize compiler."""
        self.compiled_dir = self.DEFAULT_COMPILED_DIR

    def compile(self, prompt_file: str, params: dict[str, str]) -> str:
        return _prompt_compiler.compile(self, prompt_file, params)

    def _resolve_prompt_file(self, prompt_file: str) -> Path:
        return _prompt_compiler._resolve_prompt_file(self, prompt_file)

    def _collect_dependency_dirs(self, apm_modules_dir: Path) -> list:
        return _prompt_compiler._collect_dependency_dirs(self, apm_modules_dir)

    def _raise_prompt_not_found(self, prompt_file: str, prompt_path: Path, dep_dirs: list) -> None:
        return _prompt_compiler._raise_prompt_not_found(self, prompt_file, prompt_path, dep_dirs)

    def _substitute_parameters(self, content: str, params: dict[str, str]) -> str:
        return _prompt_compiler._substitute_parameters(self, content, params)


from . import prompt_compiler as _prompt_compiler
from . import prompts as _prompts
from . import runtime as _runtime
