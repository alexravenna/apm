"""Private helpers shared within apm_cli.registry.

Module-level constants, the ``ServerNotFoundError`` exception, and small
utility functions used by :class:`~apm_cli.registry.client.SimpleRegistryClient`.

Not part of the public API; import from :mod:`apm_cli.registry.client` instead.
"""

import os
import re


def _safe_headers(response) -> dict[str, str]:
    """Return response headers as a plain dict, tolerating Mock objects in tests."""
    try:
        return dict(response.headers)
    except (TypeError, AttributeError):
        return {}


_DEFAULT_REGISTRY_URL = "https://api.mcp.github.com"

# MCP Registry API version path prefix. Bumping this here is the
# single grep target the day v0.2 ships. See
# https://github.com/modelcontextprotocol/registry for the spec.
_V0_1_PREFIX = "/v0.1"

# Allowlist for server names used as URL path segments. The MCP spec
# constrains names to reverse-DNS-style identifiers, optionally with a
# single ``/<repo>`` slug suffix. ``quote(name, safe='')`` already
# neutralises traversal/SSRF; this allowlist makes the threat model
# explicit at the call site so a future caller cannot bypass search
# and feed attacker-controlled strings into the path.
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)?$")


class ServerNotFoundError(ValueError):
    """Raised when a server lookup against the registry returns 404.

    Carries the registry URL so the CLI boundary can render an
    actionable hint about MCP Registry v0.1 spec compliance for
    self-hosted registries. Inherits from ``ValueError`` so existing
    ``except ValueError`` callers (e.g. ``find_server_by_reference``)
    keep treating 404s as "not found" without code changes.
    """

    def __init__(self, server_name: str, registry_url: str) -> None:
        self.server_name = server_name
        self.registry_url = registry_url
        super().__init__(
            f"Server '{server_name}' not found in registry {registry_url}. "
            f"If this is a self-hosted registry, verify it implements the "
            f"MCP Registry v0.1 API (apm uses /v0.1/servers/...)."
        )


# Network timeouts for registry HTTP calls. ``connect`` bounds the TCP
# handshake (typo in --registry / unreachable host) so ``apm install``
# never hangs in CI; ``read`` bounds slow registries / proxies.
# Exposed via ``MCP_REGISTRY_CONNECT_TIMEOUT`` / ``MCP_REGISTRY_READ_TIMEOUT``
# for enterprise tuning, with sane defaults otherwise.
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_READ_TIMEOUT = 30.0


def _resolve_timeout() -> tuple:
    """Return the ``(connect, read)`` timeout tuple for registry HTTP calls."""

    def _read_float(env_key: str, default: float) -> float:
        raw = os.environ.get(env_key)
        if not raw:
            return default
        try:
            value = float(raw)
            if value <= 0:
                return default
            return value
        except (TypeError, ValueError):
            return default

    return (
        _read_float("MCP_REGISTRY_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT),
        _read_float("MCP_REGISTRY_READ_TIMEOUT", _DEFAULT_READ_TIMEOUT),
    )
