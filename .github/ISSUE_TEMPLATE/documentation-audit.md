---
name: Documentation audit
about: Align public-facing documentation with the repository and remove drift.
title: "Audit public-facing documentation"
labels: ready, documentation
assignees: ""
---

## Goal

Make public-facing documentation consistent, non-redundant, and aligned with
the repository.

## Scope

- README
- CONTRIBUTING
- DEVELOPMENT_PRINCIPLES
- docs/
- .github/ISSUE_TEMPLATE/
- Package metadata

## Tasks

- [ ] Find duplicated, stale, contradictory, or misplaced information.
- [ ] Ensure documented workflows match actual repo configuration.
- [ ] Ensure links resolve.
- [ ] Keep public docs focused on their document ownership.

## Checks

- [ ] `just docs`
- [ ] `just lint`
