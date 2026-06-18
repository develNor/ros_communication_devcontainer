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
- `main` commit SHA:
- Previous release tag:
- Promotion/validation evidence:

## Tasks

- [ ] Choose the target tag in the form `vX.Y.Z`.
- [ ] Identify the previous release tag.
- [ ] Confirm the release commit is on `main` and record its exact SHA.
- [ ] Review all commits and the full diff since the previous release.
- [ ] Summarize user-facing CLI, config, Docker, dependency, and packaging changes.
- [ ] Copy `docs/release-notes/TEMPLATE.md` to `docs/release-notes/vX.Y.Z.md`.
- [ ] State breaking changes and migration steps, or explicitly say there are none.
- [ ] Include local command results in the PR description.
- [ ] After merge, tag the release commit and push the tag.
- [ ] Confirm the tag workflow succeeded for the exact recorded SHA.
- [ ] Confirm the configured package deployment succeeded.
- [ ] Inspect the wheel, source distribution, checksums, and GitHub Release.
- [ ] Record release, workflow, and deployment links.
- [ ] Do not create a downstream sync PR until all release evidence is reviewed.

## Checks

- [ ] `just lint`
- [ ] `just typecheck`
- [ ] `just test-unit`
- [ ] `just test-contract`
- [ ] `just docs`
- [ ] `just package`
- [ ] `just test-e2e-smoke`

## Evidence

- Release PR:
- GitHub Release:
- Release workflow:
- Deployment:
- Artifacts inspected:
