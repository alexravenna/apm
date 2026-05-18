"""Managed-files partitioning helpers for BaseIntegrator.

Extracted from :mod:`apm_cli.integration.base_integrator` to keep
that module under the 500-line ceiling while preserving all behaviour.

``BaseIntegrator`` re-exports these as thin ``@staticmethod`` wrappers
so all call-sites remain unchanged.
"""

from __future__ import annotations

# Backward-compat aliases mapping raw ``{prim}_{target}`` keys to
# the bucket names that existing callers expect.  Shared between
# ``partition_managed_files`` and ``partition_bucket_key`` so the
# mapping is defined exactly once.
_BUCKET_ALIASES: dict = {  # noqa: RUF012
    "prompts_copilot": "prompts",
    "agents_copilot": "agents_github",
    "commands_claude": "commands",
    "commands_cursor": "commands_cursor",
    "commands_opencode": "commands_opencode",
    "instructions_copilot": "instructions",
    "instructions_cursor": "rules_cursor",
    "instructions_claude": "rules_claude",
}


def partition_bucket_key(prim_name: str, target_name: str) -> str:
    """Return the canonical bucket key for a (primitive, target) pair.

    Applies backward-compat aliases so callers stay in sync with
    ``partition_managed_files`` bucket naming.
    """
    raw = f"{prim_name}_{target_name}"
    return _BUCKET_ALIASES.get(raw, raw)


def partition_managed_files(
    managed_files: set[str],
    targets=None,
) -> dict:
    """Partition *managed_files* by integration prefix in a single pass.

    When *targets* is provided, prefixes and bucket keys are derived
    from those (scope-resolved) profiles.  Otherwise falls back to
    ``KNOWN_TARGETS`` for backward compatibility.

    Bucket keys are generated dynamically so adding a new target or
    primitive automatically creates the corresponding bucket.

    Cross-target buckets (``skills``, ``hooks``) group all targets
    together because ``SkillIntegrator`` and ``HookIntegrator``
    handle multi-target sync internally.

    Path routing uses a longest-prefix-match strategy so multi-level
    roots like ``.config/opencode/`` are handled correctly.
    """
    from apm_cli.integration.targets import KNOWN_TARGETS

    source = targets if targets is not None else KNOWN_TARGETS.values()

    buckets: dict = {}

    # Skills and hooks are cross-target (single bucket each)
    skill_prefixes: list = []
    hook_prefixes: list = []

    # prefix -> bucket_key (longest-prefix-match routing)
    prefix_map: dict = {}

    for target in source:
        for prim_name, mapping in target.primitives.items():
            # Dynamic-root targets (cowork) use cowork:// URI prefix.
            if target.resolved_deploy_root is not None:
                if prim_name == "skills":
                    from apm_cli.integration.copilot_cowork_paths import COWORK_LOCKFILE_PREFIX

                    skill_prefixes.append(COWORK_LOCKFILE_PREFIX)
                continue
            effective_root = mapping.deploy_root or target.root_dir
            prefix = (
                f"{effective_root}/{mapping.subdir}/" if mapping.subdir else f"{effective_root}/"
            )
            if prim_name == "skills":
                skill_prefixes.append(prefix)
            elif prim_name == "hooks":
                hook_prefixes.append(prefix)
            else:
                raw_key = f"{prim_name}_{target.name}"
                bucket_key = _BUCKET_ALIASES.get(raw_key, raw_key)
                if bucket_key not in buckets:
                    buckets[bucket_key] = set()
                prefix_map[prefix] = bucket_key

    buckets["skills"] = set()
    buckets["hooks"] = set()

    skill_tuple = tuple(skill_prefixes)
    hook_tuple = tuple(hook_prefixes)

    # Build a prefix trie keyed by path segments for O(depth) routing.
    # Each node is a dict; the special key "_bucket" stores the bucket
    # for a complete prefix ending at that node.  This preserves the
    # "single pass, O(1) per path" property from the original
    # component_map approach while supporting multi-level roots like
    # .config/opencode/.
    trie: dict = {}
    for prefix, bucket_key in prefix_map.items():
        segments = [s for s in prefix.split("/") if s]
        node = trie
        for segment in segments:
            child = node.get(segment)
            if child is None:
                child = {}
                node[segment] = child
            node = child
        node["_bucket"] = bucket_key

    for p in managed_files:
        # Walk the trie; keep the deepest bucket match (longest prefix).
        segments = [s for s in p.split("/") if s]
        node = trie
        last_bucket: str | None = None
        for segment in segments:
            child = node.get(segment)
            if child is None:
                break
            node = child
            bk = node.get("_bucket")
            if bk is not None:
                last_bucket = bk
        if last_bucket is not None:
            buckets[last_bucket].add(p)
            continue
        # Fall back to cross-target buckets
        if p.startswith(skill_tuple):
            buckets["skills"].add(p)
        elif p.startswith(hook_tuple):
            buckets["hooks"].add(p)

    return buckets
