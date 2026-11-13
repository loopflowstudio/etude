"""The config and seed that must agree between attempts of one run.

`resume_conflicts` supplies field-level refusals to local and launched callers.
Source revisions and progress targets may change without changing this identity.
"""

import json


def resume_conflicts(
    *,
    config: str | None,
    seed: int | None,
    prior_config: str | None,
    prior_seed: int | None,
) -> list[str]:
    """Describe changed identity fields; compare JSON by value, other formats by text."""
    differences: list[str] = []
    if prior_seed != seed:
        differences.append(f"seed was {prior_seed!r}, is {seed!r}")
    if prior_config == config:
        return differences
    try:
        before = None if prior_config is None else json.loads(prior_config)
        after = None if config is None else json.loads(config)
    except ValueError:
        # Command jobs can carry configs in their own formats, such as TOML.
        before, after = prior_config, config
    if isinstance(before, dict) and isinstance(after, dict):
        differences.extend(
            f"{key} was {before.get(key)!r}, is {after.get(key)!r}"
            for key in sorted(before.keys() | after.keys())
            if key not in before or key not in after or before[key] != after[key]
        )
    elif before != after:
        differences.append("config differs")
    return differences
