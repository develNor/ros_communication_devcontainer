---
name: Work item
about: Track actionable project work that should be handled through an issue and PR.
title: ""
labels: ready
assignees: ""
---

## Goal

Describe the outcome this issue should achieve.

## Scope

- In scope:
- Out of scope:

## Constraints

- Follow `CONTRIBUTING.md`, `DEVELOPMENT_PRINCIPLES.md`, and `docs/work-items.md`.
- Keep the change small and coherent.
- Do not skip, weaken, or delete tests/CI to make this pass.

## Acceptance Criteria

- [ ] The requested behavior or documentation exists.
- [ ] Public behavior changes update README, docs, examples, packaging, and tests where applicable.
- [ ] The PR links this issue.
- [ ] The PR body records local checks and manual verification.

## Checks

- [ ] `just lint`
- [ ] `just typecheck`
- [ ] `just test-unit`
- [ ] `just test-contract`
- [ ] `just docs`
- [ ] `just check`
- [ ] `just test-e2e-smoke`, if Docker/runtime behavior changed.
