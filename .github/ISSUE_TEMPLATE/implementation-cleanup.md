---
name: Implementation cleanup
about: Simplify implementation while preserving the public contract.
title: "Clean up implementation"
labels: ready
assignees: ""
---

## Goal

Improve implementation quality while preserving the current public contract.

## Constraints

- Do not change documented CLI/config/Docker behavior unless tests and docs prove the old behavior was stale or broken.
- Do not remove tests or weaken assertions.
- Avoid broad rewrites.

## Tasks

- [ ] Check for dead code and unused imports.
- [ ] Check for duplicated logic.
- [ ] Check for unnecessarily complicated control flow.
- [ ] Check for tests that no longer test meaningful behavior.

## Checks

- [ ] `just lint`
- [ ] `just typecheck`
- [ ] `just test-unit`
- [ ] `just test-contract`
- [ ] `just docs`
- [ ] `just test-e2e-smoke`, if Docker/runtime behavior changed.
