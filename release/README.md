# Releases

```sh
lf release check            # has anything merged since the last tag?
lf release run patch        # or minor, major, or an exact X.Y.Z
```

A release is an explicit commit. `lf release run` writes notes for the changes
since the last tag, bumps `version` in `pyproject.toml`, adds
`release/vX.Y.Z/NOTES.md` and `RELEASE_NOTES.md`, and lands them as a release
pull request like any other.

It then starts `.github/workflows/release.yml` on that merged commit. The
workflow checks that the tag matches the version, builds, installs the wheel
alone in a clean environment and runs it, and publishes `etude-sdk` to PyPI.
When it succeeds, `lf release run` tags the commit `vX.Y.Z`.

PyPI trusts that workflow by name, through the `pypi` environment, so no token
is stored. A failed run can be resumed with `lf release run`; publishing skips
files PyPI already has. The configuration is in `.lf/config.yaml`.

`release/vX.Y.Z/NOTES.md` stays as the record of each shipped version.
