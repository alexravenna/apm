"""Test Runtime Factory."""

from unittest.mock import Mock, patch  # noqa: F401

import pytest

from apm_cli.runtime.factory import RuntimeFactory

_llm_available = RuntimeFactory.runtime_exists("llm")
_any_runtime_available = bool(RuntimeFactory.get_available_runtimes())

skip_no_llm = pytest.mark.skipif(
    not _llm_available, reason="LLM runtime not installed on this system"
)
skip_no_runtime = pytest.mark.skipif(
    not _any_runtime_available, reason="No runtime available on this system"
)


class TestRuntimeFactory:
    """Test Runtime Factory."""

    def test_get_available_runtimes_real_system(self):
        """Test getting available runtimes - returns a list (may be empty in CI)."""
        available = RuntimeFactory.get_available_runtimes()

        assert isinstance(available, list)
        assert all(rt.get("available") for rt in available)

    @skip_no_llm
    def test_get_available_runtimes_includes_llm(self):
        """Test that LLM appears in available runtimes when installed."""
        available = RuntimeFactory.get_available_runtimes()

        assert any(rt.get("name") == "llm" for rt in available)

    @skip_no_llm
    def test_get_runtime_by_name_llm_real(self):
        """Test getting LLM runtime by name (real system)."""
        runtime = RuntimeFactory.get_runtime_by_name("llm")

        assert runtime is not None
        assert runtime.get_runtime_name() == "llm"

    def test_get_runtime_by_name_unknown(self):
        """Test getting unknown runtime by name."""
        with pytest.raises(ValueError, match="Unknown runtime: unknown"):
            RuntimeFactory.get_runtime_by_name("unknown")

    @skip_no_runtime
    def test_get_best_available_runtime_real(self):
        """Test getting best available runtime on real system."""
        runtime = RuntimeFactory.get_best_available_runtime()

        assert runtime is not None
        assert runtime.get_runtime_name() in ["copilot", "codex", "llm"]

    @skip_no_llm
    def test_create_runtime_with_name_real(self):
        """Test creating runtime with specific name (real system)."""
        runtime = RuntimeFactory.create_runtime("llm")

        assert runtime is not None
        assert runtime.get_runtime_name() == "llm"

    @skip_no_runtime
    def test_create_runtime_auto_detect_real(self):
        """Test creating runtime with auto-detection (real system)."""
        runtime = RuntimeFactory.create_runtime()

        assert runtime is not None
        assert runtime.get_runtime_name() in ["copilot", "codex", "llm"]

    @skip_no_llm
    def test_runtime_exists_llm_true(self):
        """Test runtime exists check for LLM - true when installed."""
        assert RuntimeFactory.runtime_exists("llm") is True

    def test_runtime_exists_false(self):
        """Test runtime exists check - false for unknown runtime."""
        assert RuntimeFactory.runtime_exists("unknown") is False

    def test_runtime_exists_codex_depends_on_system(self):
        """Test runtime exists check for Codex - depends on system."""
        # Codex availability depends on whether it's installed
        # This test just verifies the method doesn't crash
        result = RuntimeFactory.runtime_exists("codex")
        assert isinstance(result, bool)
