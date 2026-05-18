"""Package reference resolution and validation helpers."""

import sys
from dataclasses import dataclass, field
from typing import Any


def _process_marketplace_ref(
    package, auth_resolver, logger, _marketplace_provenance, _apm_yml_entries, _misconfig_risks
):
    """Process marketplace references and resolve them to canonical form.

    Returns tuple of (resolved_package, marketplace_dep_ref, marketplace_provenance, misconfig_risk, error).
    Error is None on success, or a reason string on failure.
    """
    marketplace_provenance = None
    marketplace_dep_ref = None
    misconfig_risk = None

    # --- Marketplace pre-parse intercept ---
    # If input has no slash and is not a local path, check if it is a
    # marketplace ref (NAME@MARKETPLACE).  If so, resolve it to a
    # canonical owner/repo[#ref] string before entering the standard
    # parse path.  Anything that doesn't match is rejected as an
    # invalid format.
    if "/" not in package and not sys.modules[
        "apm_cli.commands.install"
    ].DependencyReference.is_local_path(package):
        try:
            from ....marketplace.resolver import (
                parse_marketplace_ref,
                resolve_marketplace_plugin,
            )

            mkt_ref = parse_marketplace_ref(package)
        except ImportError:
            mkt_ref = None

        if mkt_ref is not None:
            plugin_name, marketplace_name, version_spec = mkt_ref
            try:
                warning_handler = None
                if logger:

                    def warning_handler(msg):
                        return logger.warning(msg)

                    logger.verbose_detail(
                        f"    Resolving {plugin_name}@{marketplace_name} via marketplace..."
                    )
                resolution = resolve_marketplace_plugin(
                    plugin_name,
                    marketplace_name,
                    version_spec=version_spec,
                    auth_resolver=auth_resolver,
                    warning_handler=warning_handler,
                )
                canonical_str, _resolved_plugin = resolution
                if logger:
                    logger.verbose_detail(f"    Resolved to: {canonical_str}")
                marketplace_provenance = {
                    "discovered_via": marketplace_name,
                    "marketplace_plugin_name": plugin_name,
                }
                package = canonical_str
                marketplace_dep_ref = getattr(resolution, "dependency_reference", None)
                _risk = getattr(resolution, "cross_repo_misconfig_risk", None)
                if _risk is not None:
                    misconfig_risk = (marketplace_name, plugin_name, _risk)
            except Exception as mkt_err:
                return None, None, None, None, str(mkt_err)
        else:
            # No slash, not a local path, and not a marketplace ref
            return (
                None,
                None,
                None,
                None,
                "invalid format -- use 'owner/repo' or 'plugin-name@marketplace'",
            )

    return package, marketplace_dep_ref, marketplace_provenance, misconfig_risk, None


def _canonicalize_package_ref(
    package,
    marketplace_dep_ref,
    direct_gitlab_virtual_resolved_holder,
    auth_resolver,
    logger,
    _apm_yml_entries,
):
    """Canonicalize and parse package reference.

    Returns tuple of (dep_ref, canonical, identity, error).
    Error is None on success, or a reason string on failure.
    """
    from apm_cli.install.package_resolution import (
        dependency_reference_to_yaml_entry,
        resolve_parsed_dependency_reference,
    )

    # Canonicalize input
    try:
        dep_ref, direct_gitlab_virtual_resolved = resolve_parsed_dependency_reference(
            package,
            marketplace_dep_ref,
            dependency_reference_cls=sys.modules["apm_cli.commands.install"].DependencyReference,
            try_resolve_gitlab_direct_shorthand=sys.modules[
                "apm_cli.commands.install"
            ]._try_resolve_gitlab_direct_shorthand,
            auth_resolver=auth_resolver,
            verbose=bool(logger and logger.verbose),
        )
        canonical = dep_ref.to_canonical()
        identity = dep_ref.get_identity()
        if marketplace_dep_ref is not None or direct_gitlab_virtual_resolved:
            _apm_yml_entries[canonical] = dependency_reference_to_yaml_entry(dep_ref)
            direct_gitlab_virtual_resolved_holder[0] = direct_gitlab_virtual_resolved
    except ValueError as e:
        return None, None, None, str(e)

    return dep_ref, canonical, identity, None


def _check_insecure_dependency(dep_ref, allow_insecure, canonical, _apm_yml_entries):
    """Check if dependency is insecure and handle accordingly.

    Returns error reason if insecure and not allowed, None otherwise.
    """
    from apm_cli.install.insecure_policy import (
        _format_insecure_dependency_requirements,
        _get_insecure_dependency_url,
    )

    if dep_ref.is_insecure:
        if not allow_insecure:
            # The reason string embeds the full URL already, so skip
            # logger.validation_fail (which prepends "{package} -- ") to
            # avoid rendering the URL twice. Use logger.error directly.
            return _format_insecure_dependency_requirements(_get_insecure_dependency_url(dep_ref))
        dep_ref.allow_insecure = True
        _apm_yml_entries[canonical] = dep_ref.to_apm_yml_entry()

    return None


def _check_scope_rejection(dep_ref, scope):
    """Check if package should be rejected based on scope.

    Returns rejection reason if rejected, None otherwise.
    """
    from apm_cli.install.package_resolution import user_scope_rejection_reason

    scope_reject = user_scope_rejection_reason(dep_ref, scope)
    return scope_reject


@dataclass
class _PackageAccessCtx:
    """Bundled arguments for :func:`_validate_package_accessibility`."""

    package: Any
    dep_ref: Any
    canonical: Any
    identity: Any
    already_in_deps: bool
    validated_packages: list
    existing_identities: Any
    valid_outcomes: list
    marketplace_provenance: Any
    _marketplace_provenance: dict
    _apm_yml_entries: dict
    current_deps: Any
    misconfig_risk: Any
    auth_resolver: Any
    logger: Any


def _validate_package_accessibility(ctx: _PackageAccessCtx):
    """Validate package exists and is accessible.

    Returns tuple of (validation_success, dependencies_changed, error_reason).
    """
    from apm_cli.install.package_resolution import merge_structured_entry_into_current_deps

    package = ctx.package
    dep_ref = ctx.dep_ref
    canonical = ctx.canonical
    identity = ctx.identity
    already_in_deps = ctx.already_in_deps
    validated_packages = ctx.validated_packages
    existing_identities = ctx.existing_identities
    valid_outcomes = ctx.valid_outcomes
    marketplace_provenance = ctx.marketplace_provenance
    _marketplace_provenance = ctx._marketplace_provenance
    _apm_yml_entries = ctx._apm_yml_entries
    current_deps = ctx.current_deps
    misconfig_risk = ctx.misconfig_risk
    auth_resolver = ctx.auth_resolver
    logger = ctx.logger
    dependencies_changed = False

    # Validate package exists and is accessible
    verbose = bool(logger and logger.verbose)
    if sys.modules["apm_cli.commands.install"]._validate_package_exists(
        package,
        verbose=verbose,
        auth_resolver=auth_resolver,
        logger=logger,
        dep_ref=dep_ref,
    ):
        valid_outcomes.append((canonical, already_in_deps))
        if logger:
            logger.validation_pass(canonical, already_present=already_in_deps)

        if not already_in_deps:
            validated_packages.append(canonical)
            existing_identities.add(identity)  # prevent duplicates within batch
        elif canonical in _apm_yml_entries:
            structured_entry = _apm_yml_entries[canonical]
            merge_structured_entry_into_current_deps(
                current_deps,
                structured_entry,
                identity,
                canonical,
                dependency_reference_cls=sys.modules[
                    "apm_cli.commands.install"
                ].DependencyReference,
                logger=logger,
            )
            dependencies_changed = True
        if marketplace_provenance:
            _marketplace_provenance[identity] = marketplace_provenance
        return True, dependencies_changed, None

    # Validation failed - build error reason
    reason = sys.modules["apm_cli.commands.install"]._local_path_failure_reason(dep_ref)
    if not reason:
        # Round-4 panel fix (devx-ux): name the four-step probe
        # chain explicitly when the validator exhausted it
        # (virtual subdirectory + explicit ref). Generic "not
        # accessible" hides the failure mode for the precise
        # case where the most diagnostics are available.
        is_subdir_ref_chain = (
            dep_ref.is_virtual and dep_ref.is_virtual_subdirectory() and bool(dep_ref.reference)
        )
        if is_subdir_ref_chain:
            reason = (
                "all probes failed (marker-file, Contents API, "
                "git ls-remote, shallow-fetch) -- verify the path "
                "and ref exist and that your credentials have "
                "read access"
            )
            if not verbose:
                reason += " (run with --verbose for the full probe log)"
        else:
            reason = "not accessible or doesn't exist"
            if not verbose:
                reason += " -- run with --verbose for auth details"

    # #1305: when a cross-repo dict ``type: github`` source on an
    # enterprise marketplace fails validation, the failure is most
    # likely the silent auth mis-route (bare canonical fell back to
    # ``github.com``). Surface the host-qualify hint inline so the
    # operator can correct ``marketplace.json`` without rerunning
    # under ``--verbose`` to decode the auth trace. ``logger.warning``
    # is used (not ``info``) per the PR #1292 panel review's explicit
    # guidance for this exact follow-up: a misconfiguration that
    # voids ``apm install`` should be at warning level, not buried
    # in info-level ambient output. The second clause acknowledges
    # the legitimate cross-host alternative so operators whose
    # github.com dep failed for a transient reason (rate limit,
    # network, expired PAT) are not misdirected into adding an
    # enterprise host prefix that would break a working config.
    if misconfig_risk is not None and logger:
        _mp_name, _plugin_name, _risk = misconfig_risk
        logger.warning(
            f"'{_plugin_name}@{_mp_name}' is registered on "
            f"'{_risk.marketplace_host}' but the plugin's bare "
            f"`repo: {_risk.bare_repo_field}` resolved to "
            "'github.com'. If you meant the enterprise host, set "
            "the plugin's `repo` field to "
            f"'{_risk.suggested_qualified_repo}' in marketplace.json. "
            "If this is intentionally a github.com dependency, "
            "verify your github.com credentials and that the "
            "repository is accessible."
        )

    return False, dependencies_changed, reason


def _resolve_package_references(
    packages,
    current_deps,
    existing_identities,
    *,
    auth_resolver=None,
    logger=None,
    scope=None,
    allow_insecure=False,
):
    """Validate, canonicalize, and resolve package references.

    Handles marketplace refs, canonical parsing, insecure-URL guards,
    local-at-user-scope rejection, and accessibility checks.

    *existing_identities* is mutated (new identities are added to prevent
    duplicates within the same batch).

    Returns:
        Tuple of ``(valid_outcomes, invalid_outcomes, validated_packages,
        marketplace_provenance, apm_yml_entries, dependencies_changed)``.
    """
    valid_outcomes = []  # (canonical, already_present) tuples
    invalid_outcomes = []  # (package, reason) tuples
    _marketplace_provenance = {}  # canonical -> {discovered_via, marketplace_plugin_name}
    _apm_yml_entries = {}  # canonical -> apm.yml entry (str or dict for HTTP deps)
    validated_packages = []
    dependencies_changed = False

    if logger:
        logger.validation_start(len(packages))

    for package in packages:
        # Process marketplace references
        resolved_pkg, marketplace_dep_ref, marketplace_provenance, misconfig_risk, error = (
            _process_marketplace_ref(
                package, auth_resolver, logger, _marketplace_provenance, _apm_yml_entries, {}
            )
        )
        if error:
            invalid_outcomes.append((package, error))
            if logger:
                logger.validation_fail(package, error)
            continue
        if resolved_pkg:
            package = resolved_pkg

        # Track if gitlab virtual subdirectory was resolved
        direct_gitlab_virtual_resolved_holder = [False]

        # Canonicalize package reference
        dep_ref, canonical, identity, error = _canonicalize_package_ref(
            package,
            marketplace_dep_ref,
            direct_gitlab_virtual_resolved_holder,
            auth_resolver,
            logger,
            _apm_yml_entries,
        )
        if error:
            invalid_outcomes.append((package, error))
            if logger:
                logger.validation_fail(package, error)
            continue

        # Check insecure dependency
        error = _check_insecure_dependency(dep_ref, allow_insecure, canonical, _apm_yml_entries)
        if error:
            invalid_outcomes.append((package, error))
            if logger:
                logger.error(error)
            continue

        # Check scope rejection
        error = _check_scope_rejection(dep_ref, scope)
        if error:
            invalid_outcomes.append((package, error))
            if logger:
                logger.validation_fail(package, error)
            continue

        # Check if package is already in dependencies (by identity)
        already_in_deps = identity in existing_identities

        # Validate package accessibility
        success, deps_changed, error = _validate_package_accessibility(
            _PackageAccessCtx(
                package=package,
                dep_ref=dep_ref,
                canonical=canonical,
                identity=identity,
                already_in_deps=already_in_deps,
                validated_packages=validated_packages,
                existing_identities=existing_identities,
                valid_outcomes=valid_outcomes,
                marketplace_provenance=marketplace_provenance,
                _marketplace_provenance=_marketplace_provenance,
                _apm_yml_entries=_apm_yml_entries,
                current_deps=current_deps,
                misconfig_risk=misconfig_risk,
                auth_resolver=auth_resolver,
                logger=logger,
            )
        )

        if not success:
            invalid_outcomes.append((package, error))
            if logger:
                logger.validation_fail(package, error)

        if deps_changed:
            dependencies_changed = True

    return (
        valid_outcomes,
        invalid_outcomes,
        validated_packages,
        _marketplace_provenance,
        _apm_yml_entries,
        dependencies_changed,
    )
