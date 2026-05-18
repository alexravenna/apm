"""TLS verification helpers for the install validation pipeline.

Extracted from ``apm_cli.install.validation`` so the TLS-detection and
CA-trust logging logic lives in a focused, independently testable module.
``apm_cli.install.validation`` re-exports both public names so all existing
import sites remain valid.
"""

from __future__ import annotations

import requests

# Marker prefix used on RuntimeError messages raised when the underlying
# network probe fails TLS verification. Lets the caller distinguish trust
# failures from auth / 404 / network errors so the user is not pushed down
# the PAT troubleshooting path for a CA-trust problem.
_TLS_ERROR_PREFIX = "TLS verification failed"

__all__ = ["_is_tls_failure", "_log_tls_failure"]


def _is_tls_failure(exc: BaseException) -> bool:
    """Return True if *exc* (or any cause in its chain) is a TLS verification failure."""
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 8:
        msg = str(cur)
        if _TLS_ERROR_PREFIX in msg or "CERTIFICATE_VERIFY_FAILED" in msg:
            return True
        if isinstance(cur, requests.exceptions.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


def _log_tls_failure(host_display: str, exc: BaseException, verbose_log, logger) -> None:
    """Surface a TLS verification failure with an actionable CA-trust hint.

    Default verbosity: a single one-liner via ``logger.warning`` so users behind
    a corporate proxy see the right next step without re-running with --verbose.
    Verbose: also include the host name and the underlying exception text.
    """
    logger.warning(
        "TLS verification failed -- if you're behind a corporate proxy or "
        "firewall, set the REQUESTS_CA_BUNDLE environment variable to the "
        "path of your organisation's CA bundle (a PEM file) and retry. "
        "See: https://microsoft.github.io/apm/troubleshooting/ssl-issues/"
    )
    if verbose_log:
        verbose_log(f"underlying error from {host_display}: {exc}")
