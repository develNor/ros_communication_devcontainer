# Release

Releases are built by `.github/workflows/release.yml`.

## Version Source

Package versions come from Git tags through `setuptools-scm`. The project does
not keep a static version in `pyproject.toml` or `src/rosotacom/__init__.py`.

## Tags

Stable releases use tags in this form:

```text
vX.Y.Z
```

Tags are cut from a deliberately selected and validated `main` commit (see
[ci.md](ci.md) for the branch model). Push the tag from that exact commit.

## Release Notes

Each stable release must include:

```text
docs/release-notes/vX.Y.Z.md
```

Start from [docs/release-notes/TEMPLATE.md](release-notes/TEMPLATE.md). The
release notes should summarize user-facing CLI, config, packaging, Docker,
dependency, compatibility, and migration changes.

## Publishing

Tag pushes validate, publish through the repository's configured Trusted
Publisher, and create a GitHub Release with the release notes, wheel, source
distribution, and `SHA256SUMS`. Manual workflow dispatch validates and builds
artifacts but does not publish; publishing requires a deliberate `vX.Y.Z` tag.

The target index is set per repository by the `PYPI_PUBLISH_URL` Actions
variable, and authorization is granted by the Trusted Publisher registered for
that repository. Both controls live in repository settings rather than shared
git content.

Publishing a release does not authorize or trigger synchronization to another
repository. A downstream sync should use one stable tag as its boundary and
must separately verify the exact tag SHA, release notes, successful release
workflow, successful deployment, and generated artifacts before a PR is
created.
