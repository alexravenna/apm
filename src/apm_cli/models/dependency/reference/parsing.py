"""DependencyReference model  -- core dependency representation and parsing."""

import re
import urllib.parse
from pathlib import Path

from ....cache.url_normalize import SCP_LIKE_RE
from ....utils.github_host import (
    default_host,
    is_azure_devops_hostname,
    maybe_raise_bare_fqdn_github_gitlab_conflict,
    unsupported_host_error,
)
from ....utils.path_security import (
    validate_path_segments,
)

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}


from ._object_form import (
    _normalize_parent_repo_decl_path,
    _parse_object_git_overrides,
    _parse_object_local_path,
    _parse_object_parent,
    parse_from_dict,
)
from .core import DependencyReference


@staticmethod
def _parse_ssh_protocol_url(url: str):
    """Parse an ``ssh://`` protocol URL using ``urllib.parse.urlparse``.

    Unlike SCP shorthand (``git@host:path``), the ``ssh://`` form is a real
    URL that can carry a port. Parsing it via ``urlparse`` preserves the
    port and cleanly separates the fragment (``#ref``) from the path, so
    APM-specific ``@alias`` suffixes are handled without regex gymnastics.

    Supported forms:
        ssh://git@host/owner/repo.git
        ssh://git@host:7999/owner/repo.git
        ssh://git@host/owner/repo.git#ref
        ssh://git@host:7999/owner/repo.git#ref@alias
        ssh://git@host/owner/repo.git@alias

    Returns:
        ``(host, port, repo_url, reference, alias)`` or ``None`` if the
        input is not an ``ssh://`` URL.
    """
    if not url.startswith("ssh://"):
        return None

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port  # int or None
    # Normalise default SSH port so ssh://host:22/... matches ssh://host/...
    if port == _DEFAULT_SCHEME_PORTS.get("ssh"):
        port = None
    path = parsed.path.lstrip("/")
    fragment = parsed.fragment

    reference: str | None = None
    alias: str | None = None

    # Fragment holds "ref" or "ref@alias"
    if fragment:
        if "@" in fragment:
            ref_part, alias_part = fragment.rsplit("@", 1)
            reference = ref_part.strip() or None
            alias = alias_part.strip() or None
        else:
            reference = fragment.strip() or None

    # Bare "@alias" (no #ref) still lives on the path
    if alias is None and "@" in path:
        path, alias_part = path.rsplit("@", 1)
        alias = alias_part.strip() or None

    if path.endswith(".git"):
        path = path[:-4]

    repo_url = path.strip()

    # Security: reject traversal sequences in SSH repo paths
    validate_path_segments(repo_url, context="SSH repository path", reject_empty=True)

    return host, port, repo_url, reference, alias


@staticmethod
def _parse_ssh_url(dependency_str: str):
    """Parse an SCP-shorthand SSH URL (``<user>@host:owner/repo``).

    Accepts any SSH username (not just ``git``), so EMU and custom GHE
    SSH accounts (e.g. ``enterprise-user@ghe.corp.com:org/repo``) parse
    correctly. SCP shorthand cannot carry a port (``:`` is the path
    separator), so the returned port is always ``None``. For custom SSH
    ports, use the ``ssh://`` URL form which is handled by
    ``_parse_ssh_protocol_url``.

    Returns:
        ``(host, port, repo_url, reference, alias)`` or *None* if not an SCP URL.
    """
    ssh_match = SCP_LIKE_RE.match(dependency_str)
    if not ssh_match:
        return None

    user = ssh_match.group("user")
    host = ssh_match.group("host")
    ssh_repo_part = ssh_match.group("path")

    reference = None
    alias = None

    if "@" in ssh_repo_part:
        ssh_repo_part, alias = ssh_repo_part.rsplit("@", 1)
        alias = alias.strip()

    if "#" in ssh_repo_part:
        repo_part, reference = ssh_repo_part.rsplit("#", 1)
        reference = reference.strip()
    else:
        repo_part = ssh_repo_part

    had_git_suffix = repo_part.endswith(".git")
    if had_git_suffix:
        repo_part = repo_part[:-4]

    repo_url = repo_part.strip()

    # SCP syntax (git@host:path) uses ':' as the path separator, so it
    # cannot carry a port.  Detect when the first segment is a valid TCP
    # port number (1-65535) and raise an actionable error instead of
    # silently misparsing the port as part of the repo path.
    segments = repo_url.split("/", 1)
    first_segment = segments[0]
    if re.fullmatch(r"[0-9]+", first_segment):
        port_candidate = int(first_segment)
        if 1 <= port_candidate <= 65535:
            remaining_path = segments[1] if len(segments) > 1 else ""
            if remaining_path:
                git_suffix = ".git" if had_git_suffix else ""
                ref_suffix = f"#{reference}" if reference else ""
                alias_suffix = f"@{alias}" if alias else ""
                suggested = f"ssh://{user}@{host}:{port_candidate}/{remaining_path}{git_suffix}{ref_suffix}{alias_suffix}"
                raise ValueError(
                    f"It looks like '{first_segment}' in '{user}@{host}:{repo_url}' "
                    f"is a port number, but SCP-style URLs (<user>@host:path) cannot "
                    f"carry a port. Use the ssh:// URL form instead:\n"
                    f"  {suggested}"
                )
            else:
                raise ValueError(
                    f"It looks like '{first_segment}' in '{user}@{host}:{first_segment}' "
                    f"is a port number, but no repository path follows it. "
                    f"SCP-style URLs (<user>@host:path) cannot carry a port. "
                    f"Use the ssh:// URL form: ssh://{user}@{host}:{port_candidate}/<owner>/<repo>.git"
                )

    # Security: reject traversal sequences in SSH repo paths
    validate_path_segments(repo_url, context="SSH repository path", reject_empty=True)

    return host, None, repo_url, reference, alias


@classmethod
def _parse_standard_url(
    cls,
    dependency_str: str,
    is_virtual_package: bool,
    virtual_path: str | None,
    validated_host: str | None,
) -> tuple[str, int | None, str, str | None, str | None, bool, str | None]:
    """Parse a non-SSH dependency string (HTTPS, FQDN, or shorthand).

    Detects scheme vs shorthand, delegates host-specific resolution to
    helpers, then validates the resulting URL path.

    Returns:
        ``(host, port, repo_url, reference, alias, effective_is_virtual,
        effective_virtual_path)`` -- the last two reflect any ADO sub-path
        segments embedded in the URL itself (issue #1128).
    """
    host = None
    port = None
    alias = None

    reference = None
    if "#" in dependency_str:
        repo_part, reference = dependency_str.rsplit("#", 1)
        reference = reference.strip()
    else:
        repo_part = dependency_str

    repo_url = repo_part.strip()

    # Lowercase copy for scheme detection -- kept from the original
    # repo_url so the URL-vs-shorthand check below still works after
    # the virtual shorthand resolver has narrowed repo_url.
    repo_url_lower = repo_url.lower()

    # For virtual packages without a URL scheme, narrow to just owner/repo
    if is_virtual_package and not repo_url_lower.startswith(("https://", "http://")):
        host, repo_url = cls._resolve_virtual_shorthand_repo(repo_url, validated_host, virtual_path)

    # Normalize to URL format for secure parsing
    if repo_url_lower.startswith(("https://", "http://")):
        parsed_url = urllib.parse.urlparse(repo_url)
        host = parsed_url.hostname or ""
        port = parsed_url.port  # capture :PORT from https://host:8443/...
        # Normalise default-scheme ports (443 for HTTPS, 80 for HTTP)
        # so lockfile keys are consistent regardless of URL spelling.
        scheme = (parsed_url.scheme or "").lower()
        if port == _DEFAULT_SCHEME_PORTS.get(scheme):
            port = None
    else:
        parsed_url, host = cls._resolve_shorthand_to_parsed_url(repo_url, host)

    repo_url, url_virtual_path = cls._validate_url_repo_path(parsed_url)

    # If URL contained extra ADO sub-path segments, they become the virtual
    # path (overriding the _detect_virtual_package result which returns
    # early for https:// URLs).
    effective_is_virtual = is_virtual_package
    effective_virtual_path = virtual_path
    if url_virtual_path is not None:
        effective_is_virtual = True
        effective_virtual_path = url_virtual_path

    if not host:
        host = default_host()

    return host, port, repo_url, reference, alias, effective_is_virtual, effective_virtual_path


@classmethod
def parse(cls, dependency_str: str) -> "DependencyReference":
    """Parse a dependency string into a DependencyReference.

    Supports formats:
    - user/repo
    - user/repo#branch
    - user/repo#v1.0.0
    - user/repo#commit_sha
    - github.com/user/repo#ref
    - user/repo@alias
    - user/repo#ref@alias
    - user/repo/path/to/file.prompt.md (virtual file package)
    - user/repo/skills/foo (virtual subdirectory package)
    - user/repo/collections/foo (virtual subdirectory package)
    - https://gitlab.com/owner/repo.git (generic HTTPS git URL)
    - git@gitlab.com:owner/repo.git (SSH git URL)
    - ssh://git@gitlab.com/owner/repo.git (SSH protocol URL)

    Ambiguous GitLab nested-group shorthand cannot cover every depth; use
    object form (``git:`` + ``path:`` in ``apm.yml``) as the supported
    escape hatch.

    - ./local/path (local filesystem path)
    - /absolute/path (local filesystem path)
    - ../relative/path (local filesystem path)

    Any valid FQDN is accepted as a git host (GitHub, GitLab, Bitbucket,
    self-hosted instances, etc.).

    Args:
        dependency_str: The dependency string to parse

    Returns:
        DependencyReference: Parsed dependency reference

    Raises:
        ValueError: If the dependency string format is invalid
    """
    if not dependency_str.strip():
        raise ValueError("Empty dependency string")

    dependency_str = urllib.parse.unquote(dependency_str)

    if any(ord(c) < 32 for c in dependency_str):
        raise ValueError("Dependency string contains invalid control characters")

    # --- Local path detection (must run before URL/host parsing) ---
    if cls.is_local_path(dependency_str):
        local = dependency_str.strip()
        pkg_name = Path(local).name
        if not pkg_name or pkg_name in (".", ".."):
            raise ValueError(
                f"Local path '{local}' does not resolve to a named directory. "
                f"Use a path that ends with a directory name "
                f"(e.g., './my-package' instead of './')."
            )
        return cls(
            repo_url=f"_local/{pkg_name}",
            is_local=True,
            local_path=local,
        )

    if dependency_str.startswith("//"):
        raise ValueError(
            unsupported_host_error("//...", context="Protocol-relative URLs are not supported")
        )

    maybe_raise_bare_fqdn_github_gitlab_conflict(dependency_str)

    # Phase 1: detect virtual packages
    is_virtual_package, virtual_path, validated_host = cls._detect_virtual_package(dependency_str)

    # Phase 2: parse SSH (ssh:// URL first -- it preserves port; then SCP
    # shorthand), otherwise fall back to HTTPS/shorthand parsing.
    explicit_scheme: str | None = None
    ssh_proto_result = cls._parse_ssh_protocol_url(dependency_str)
    if ssh_proto_result:
        host, port, repo_url, reference, alias = ssh_proto_result
        explicit_scheme = "ssh"
    else:
        scp_result = cls._parse_ssh_url(dependency_str)
        if scp_result:
            host, port, repo_url, reference, alias = scp_result
            explicit_scheme = "ssh"
        else:
            host, port, repo_url, reference, alias, is_virtual_package, virtual_path = (
                cls._parse_standard_url(
                    dependency_str, is_virtual_package, virtual_path, validated_host
                )
            )
            _stripped = dependency_str.strip().lower()
            if _stripped.startswith("https://"):
                explicit_scheme = "https"
            elif _stripped.startswith("http://"):
                explicit_scheme = "http"

    # Phase 3: final validation and ADO field extraction
    ado_organization, ado_project, ado_repo = cls._validate_final_repo_fields(host, repo_url)

    if alias and not re.match(r"^[a-zA-Z0-9._-]+$", alias):
        raise ValueError(
            f"Invalid alias: {alias}. Aliases can only contain letters, numbers, dots, underscores, and hyphens"
        )

    # Extract Artifactory prefix from the original path if applicable
    is_ado_final = host and is_azure_devops_hostname(host)
    artifactory_prefix = None
    if host and not is_ado_final:
        artifactory_prefix = cls._extract_artifactory_prefix(dependency_str, host)

    return cls(
        repo_url=repo_url,
        host=host,
        port=port,
        explicit_scheme=explicit_scheme,
        reference=reference,
        alias=alias,
        virtual_path=virtual_path,
        is_virtual=is_virtual_package,
        ado_organization=ado_organization,
        ado_project=ado_project,
        ado_repo=ado_repo,
        artifactory_prefix=artifactory_prefix,
        is_insecure=urllib.parse.urlparse(dependency_str).scheme.lower() == "http",
    )
