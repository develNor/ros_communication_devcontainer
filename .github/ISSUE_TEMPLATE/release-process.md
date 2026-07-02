---
name: Release process
about: Prepare, validate, describe, and publish a rosotacom release.
title: "Prepare release vX.Y.Z"
labels: ready, documentation
assignees: ""
---

## Goal

Ship a validated, well-described `rosotacom` release.

## Use When

A maintainer is preparing a new `vX.Y.Z` tag, package release, and GitHub
Release. This is the executable checklist for the release entrypoint in
[docs/owner-runbook.md](../../docs/owner-runbook.md#prepare-a-release).

## Target

- Target tag: `vX.Y.Z`
- `main` commit SHA:
- Previous release tag:
- Promotion/validation evidence:

## Constraints

- Follow the shared [task contract](../../docs/quality-model.md#task-contract).
- Start from the exact validated `main` commit when preparing release notes.
- Do not tag or publish until the release PR has merged.
- Open a draft review PR by default unless autonomous release work is explicitly
  requested.
- Keep external OTA gate evidence generic; do not record private hostnames,
  credentials, or deployment details.
- Update docs/tests if public CLI, config, Docker, or runtime behavior changed.

## Tasks

- [ ] Choose the target tag in the form `vX.Y.Z`.
- [ ] Identify the previous release tag.
- [ ] Confirm the release commit is on `main` and record its exact SHA.
- [ ] Review all commits and the full diff since the previous release.
- [ ] Summarize user-facing CLI, config, Docker, dependency, and packaging changes.
- [ ] Copy `docs/release-notes/TEMPLATE.md` to `docs/release-notes/vX.Y.Z.md`.
- [ ] State breaking changes and migration steps, or explicitly say there are none.
- [ ] Include local command results in the PR description.
- [ ] After merge, have a maintainer tag the release commit and push the tag.
- [ ] Confirm the tag workflow succeeded for the exact recorded SHA.
- [ ] Confirm the configured package deployment succeeded.
- [ ] Inspect the wheel, source distribution, checksums, and GitHub Release.
- [ ] Record release, workflow, and deployment links.
- [ ] Do not create a downstream sync PR until all release evidence is reviewed.

## Acceptance Criteria

- [ ] Release notes exist for the target tag.
- [ ] Compatibility, migration, Docker/runtime, dependency, packaging, and
      validation notes are explicit.
- [ ] The release workflow validates and publishes from the tag.

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

## Final Report

- Previous release tag:
- Target release tag:
- Release notes file path:
- User-facing changes:
- Compatibility or migration notes:
- Docker/runtime or dependency updates:
- Commands run:
- PR URL and CI status:
- Final package and GitHub Release status after tagging:
