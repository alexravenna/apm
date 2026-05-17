from ....core.token_manager import GitHubTokenManager as GitHubTokenManager  # noqa: F401
from ....registry.client import SimpleRegistryClient as SimpleRegistryClient  # noqa: F401
from ....registry.integration import RegistryIntegration as RegistryIntegration  # noqa: F401
from ....utils.console import _rich_warning as _rich_warning  # noqa: F401
from .class_ import CopilotClientAdapter as CopilotClientAdapter  # noqa: F401

# Re-export contract for ruff --ignore-noqa.
__all__ = [
    "CopilotClientAdapter",
    "GitHubTokenManager",
    "RegistryIntegration",
    "SimpleRegistryClient",
    "_rich_warning",
]
