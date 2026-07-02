---
name: Implementation cleanup
about: Simplify implementation while preserving the public contract.
title: "Clean up implementation"
labels: ready
assignees: ""
---

## Goal

Improve implementation quality while preserving the current public contract.

## Scope

- Source code.
- Tests only where needed to preserve or clarify behavior.
- Documentation only for small corrections caused by code cleanup.

## Constraints

- Follow the shared [task contract](../../docs/quality-model.md#task-contract).
- Do not change documented CLI/config/Docker behavior unless tests and docs prove
  the old behavior was stale or broken.
- Do not remove tests or weaken assertions.
- Do not add compatibility layers unless explicitly required.
- Prefer clean removal of obsolete or dead code.
- Avoid broad rewrites.

## Tasks

- [ ] Check for dead code and unused imports.
- [ ] Check for duplicated logic and redundant branches.
- [ ] Check for over-abstracted helpers and unclear names.
- [ ] Check for unnecessary compatibility paths.
- [ ] Check for unnecessarily complicated control flow.
- [ ] Check for tests that no longer test meaningful behavior.

## Acceptance Criteria

- [ ] Public CLI/config/Docker behavior is preserved unless explicitly documented
      and tested.
- [ ] Simplifications are focused and reviewable.
- [ ] Existing tests remain meaningful.

## Checks

- [ ] `just lint`
- [ ] `just typecheck`
- [ ] `just test-unit`
- [ ] `just test-contract`
- [ ] `just docs`
- [ ] `just test-e2e-smoke`, if Docker/runtime behavior changed.

## Final Report

- Simplifications made:
- Public behavior preserved:
- Tests changed:
- Commands run:
- Remaining risks or follow-up:
