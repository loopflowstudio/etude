# Releases

A release is one explicit commit on `main`, landed through a pull request like
any other:

```sh
uv version 0.2.0                 # or: uv version --bump minor
$EDITOR release/v0.2.0/NOTES.md  # what changed for people using it
git commit -am "Release 0.2.0"   # with the notes file added
```

When that commit reaches `main`, `.github/workflows/release.yml` sees notes for
a version that has no tag yet. It builds the package, installs the wheel in a
clean environment and runs it, publishes `etude-sdk` to PyPI, then tags the
commit `v0.2.0` and makes a GitHub Release from the notes. Any other push to
`main` releases nothing.

PyPI trusts that workflow by name, through the `pypi` environment, so no
token is stored. If the run fails before publishing, fix it in a new commit;
the next push to `main` tries again. If it fails after publishing, rerun it:
PyPI keeps the files it already has and the tag step completes.

`release/vX.Y.Z/NOTES.md` stays as the record of each shipped version.
