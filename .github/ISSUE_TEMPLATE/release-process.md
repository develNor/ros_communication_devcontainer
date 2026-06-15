---
name: Release process
about: Prepare, validate, describe, and publish a rosotacom release.
title: "Prepare release vX.Y.Z"
labels: ready, documentation
assignees: ""
---

## Goal

Ship a validated, well-described `rosotacom` release.

## Target

- Target tag: `vX.Y.Z`

## Tasks

- [ ] Choose the target tag in the form `vX.Y.Z`.
- [ ] Identify the previous release tag.
- [ ] Summarize user-facing CLI, config, Docker, dependency, and packaging changes.
- [ ] Copy `docs/release-notes/TEMPLATE.md` to `docs/release-notes/vX.Y.Z.md`.
- [ ] State breaking changes and migration steps, or explicitly say there are none.
- [ ] Include local command results in the PR description.
- [ ] After merge, tag the release commit and push the tag.
- [ ] Confirm PyPI and GitHub Release publication.

## Checks

- [ ] `just lint`
- [ ] `just typecheck`
- [ ] `just test-unit`
- [ ] `just test-contract`
- [ ] `just docs`
- [ ] `just package`
- [ ] `just test-e2e-smoke`
