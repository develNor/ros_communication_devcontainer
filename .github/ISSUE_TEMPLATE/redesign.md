---
name: Redesign
about: Bold, cross-cutting redesign of one diagnosed concern, backed by tests.
title: "Redesign: "
labels: ready
assignees: ""
---

## Goal

Redesign the specific concern named by the
[repository diagnosis](repository-diagnosis.md). This is for one coherent
vertical slice when a finding spans code, tests, docs, and naming together.

## Scope

- The single diagnosed concern.
- Code, tests, docs, examples, and packaging only where that concern reaches.

## Constraints

- Follow the shared [task contract](../../docs/quality-model.md#task-contract).
- Be bold inside the diagnosed scope; do not expand beyond it.
- Prefer clean removal, renaming, or restructuring over compatibility shims.
- Public behavior changes must update tests and docs in the same PR.
- Do not weaken Docker/runtime checks when the redesign touches runtime behavior.

## Tasks

- [ ] Restate the diagnosis and target shape.
- [ ] Make one coherent vertical-slice change.
- [ ] Update README, docs, examples, schemas, and tests for public behavior.
- [ ] Remove obsolete code or docs created by the old shape.

## Acceptance Criteria

- [ ] The diagnosed concern is resolved.
- [ ] Tests cover the new shape.
- [ ] Public behavior changes are documented.
- [ ] The PR stays inside the diagnosed scope.

## Checks

- [ ] `just lint`
- [ ] `just typecheck`
- [ ] `just test-unit`
- [ ] `just test-contract`
- [ ] `just docs`
- [ ] `just check`
- [ ] `just test-e2e-smoke`, if Docker/runtime behavior changed.

## Final Report

- Concern redesigned:
- Target shape:
- Code / tests / docs changed:
- Public behavior changes:
- Commands run:
- Remaining risks or follow-up:
