# Release

This project publishes on two channels, and the difference between them is who
decides, not how much is tested. Both build from the same commit and the same
`just package`.

| | Development channel | Stable release |
|---|---|---|
| Trigger | every commit that lands on `develop` with green CI | a `vX.Y.Z` tag |
| Version | `X.Y.devN` (PEP 440 pre-release) | `X.Y.Z` |
| Built by | `.github/workflows/dev-release.yml` | `.github/workflows/release.yml` |
| Decided by | nobody — it follows the branch | a maintainer |
| Reached by | an exact pin, or `--pre` | a normal install |

The point of the development channel is that consuming this package should never
be more expensive than consuming a checkout. Downstream repositories pin an
exact version; without a continuous channel their only alternatives are to wait
for a release decision or to bypass the package boundary, and the second one
always wins in practice.

`pip` does not resolve pre-releases unless asked, so a dev version can never
reach a consumer by accident.

## Version Source

Package versions come from Git tags through `setuptools-scm`. The project does
not keep a static version in `pyproject.toml` or `src/rosotacom/__init__.py`.

After `v2.3`, commits on `develop` therefore build as `2.4.devN`: a
pre-release *of the next* stable version, ordered before `2.4` itself.

The distribution name is never hardcoded in the source. This code publishes as
`rosotacom-dev` from the development fork and as `rosotacom` upstream, and
`rosotacom/__init__.py` looks its own distribution up rather than assuming a
name. Assuming one is what made every installed `rosotacom-dev` up to 2.3 report
its version as `0+unknown`.

The two "dev" in `rosotacom-dev==2.4.dev3` are not the same word, and only one
of them is about maturity:

- `-dev` in the **distribution name** says which line publishes it — the
  development fork, as opposed to the FZI upstream that publishes as
  `rosotacom`. It is part of every version of this package, stable ones
  included: `rosotacom-dev==2.4` is a stable release.
- `.dev3` in the **version** says how far along that particular artefact is
  within this line: the third commit after `v2.3`, on the way to `2.4`.

So `rosotacom-dev` alone tells you nothing about maturity. The version does.

## Tags

Stable releases use tags in this form:

```text
vX.Y.Z
```

Tags are cut from a validated commit on the release line — in this repository
`develop`, which is also its default branch (see [ci.md](ci.md) for the branch
model). Before cutting one, a maintainer should record the manual full-suite
gate result described in [testing.md](testing.md). Push the tag from that exact
commit.

The tag build asserts that the version it produces is the version the tag names,
and the development channel asserts the mirror image: it refuses to publish
anything that is not a pre-release. Neither channel can publish under the
other's identity.

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
artifacts but does not publish; publishing a stable version requires a
deliberate `vX.Y.Z` tag.

Development-channel uploads use the same `PYPI_PUBLISH_URL`. They are triggered
by the CI workflow *completing successfully* on `develop`, not by the push
itself, so a commit whose checks failed is never published. They skip the
TestPyPI rehearsal: the dev channel is already the rehearsal.

They need their **own** Trusted Publisher, because PyPI matches the OIDC claims
of the workflow file and this one is `dev-release.yml`. Registering it is a
one-time step, listed with the other out-of-git settings in
[github-repository-settings.md](github-repository-settings.md). Without it the
build succeeds and the upload fails with `invalid-publisher` — which reads like
a code problem and is not one.

The target index is set per repository by the `PYPI_PUBLISH_URL` Actions
variable, and authorization is granted by the Trusted Publisher registered for
that repository. Both controls live in repository settings rather than shared
git content.

Publishing a release does not authorize or trigger synchronization to another
repository. A downstream sync should use one stable tag as its boundary and
must separately verify the exact tag SHA, release notes, successful release
workflow, successful deployment, and generated artifacts before a PR is
created.
