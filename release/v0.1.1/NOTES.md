# v0.1.1

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.1.1 establishes an explicit release path from reviewed release notes to a checked PyPI package. This patch focuses on how Etude is shipped: maintainers can prepare a release with `lf release run`, and the publishing pipeline checks the built package before publishing it. The changes concern release operations; no runtime feature changes are recorded in this release range.

## Trace a release to its commit

Release preparation now records the version and notes together in a release pull request, giving maintainers a reviewable record of what they intend to ship.

- `lf release run patch`, `minor`, or `X.Y.Z` prepares notes and updates the version in `pyproject.toml`.
- Notes are retained in `release/vX.Y.Z/NOTES.md` and surfaced in `RELEASE_NOTES.md`.
- After the release pull request merges, Loopflow starts the publishing workflow on that merged commit. It pushes the `vX.Y.Z` tag only after the workflow succeeds.

## Check the package users install

The release workflow validates the release metadata and exercises the wheel before publishing, so release checks cover the packaged installation.

- The workflow checks that the requested tag matches the package version and that release notes exist.
- It builds the package, installs the wheel alone, and checks `etude --help` and imports.
- It publishes `etude-sdk` to PyPI through the GitHub `pypi` environment using trusted publishing.

## Operational notes

Publishing is explicitly started by `lf release run`; merging the release setup alone publishes nothing. The release procedure is documented in `release/README.md`, and `.lf/config.yaml` identifies `pyproject.toml` as the version file and `release.yml` as the publishing workflow.