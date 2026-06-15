---
name: Quality workflow
about: Run the multi-PR repository quality workflow.
title: "Run repository quality workflow"
labels: ready
assignees: ""
---

## Goal

Execute a multi-goal repository quality workflow with focused PRs.

## Execution Plan

- [ ] Create and complete an issue from `test-ci-audit.md`.
- [ ] Create and complete an issue from `documentation-audit.md`.
- [ ] After both are merged, create and complete an issue from `implementation-cleanup.md`.
- [ ] After cleanup, create and complete an issue from `maintainer-review.md`.

## Acceptance Criteria

- [ ] Each goal has a separate issue and PR.
- [ ] Each PR links its issue and reports checks run.
- [ ] Dependent goals start only after prerequisite PRs merge.
