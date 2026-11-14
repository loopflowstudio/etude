"""A run's identity: its address, and the config and seed its attempts must share.

`run_address` qualifies a run's name by its experiment for storage and the
launcher. `resume_conflicts` supplies field-level refusals to local and launched
callers; source revisions and progress targets may change without changing
this identity.
"""

import json
import re

# A run or experiment name that is safe as a folder in any bucket and shell.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=@-]{0,199}$")


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


def run_address(run: str, experiment: str | None) -> str:
    """The storage and launcher address of a run, qualified by its experiment.

    Unlabelled command jobs retain a flat address. A qualified address is safe
    to pass again, and cannot be silently moved into another experiment.
    """
    if not experiment:
        return run
    prefix = experiment + "/"
    name = run.removeprefix(prefix)
    if not (SAFE_NAME.fullmatch(experiment) and SAFE_NAME.fullmatch(name)):
        raise ValueError("run and experiment must be portable names")
    if run.startswith(prefix):
        return run  # Historical qualified addresses keep their stored spelling.
    if name == experiment or name.startswith(experiment + "@"):
        return name
    return prefix + name
