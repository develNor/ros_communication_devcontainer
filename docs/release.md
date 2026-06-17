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

Tags are cut from `main`, which only carries multi-machine-proven commits
promoted from `develop` (see [ci.md](ci.md) for the branch model). Push the tag
from the chosen `main` commit.

## Release Notes

Each stable release must include:

```text
docs/release-notes/vX.Y.Z.md
```

Start from [docs/release-notes/TEMPLATE.md](release-notes/TEMPLATE.md). The
release notes should summarize user-facing CLI, config, packaging, Docker,
dependency, compatibility, and migration changes.

## Publishing

Tag pushes publish to PyPI through Trusted Publishing and create a GitHub
Release with the release notes, wheel, source distribution, and `SHA256SUMS`.
Manual workflow dispatch publishes only to TestPyPI.
