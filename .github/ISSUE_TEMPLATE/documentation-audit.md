---
name: Documentation audit
about: Align public-facing documentation with the repository and remove drift.
title: "Audit public-facing documentation"
labels: ready, documentation
assignees: ""
---

## Goal

Make public-facing documentation consistent, non-redundant, aligned with the
repository, and no larger than it needs to be. Every document and section should
earn its place or be removed or merged into its canonical home.

## Scope

- README.
- CONTRIBUTING.
- DEVELOPMENT_PRINCIPLES.
- docs/.
- `.github/ISSUE_TEMPLATE/`.
- Package metadata if it contains public-facing descriptions.

## Document Ownership

Audit against the ownership map in
[docs/quality-model.md](../../docs/quality-model.md#document-ownership). Do not
restate that map here.

## Constraints

- Follow the shared [task contract](../../docs/quality-model.md#task-contract).
- Prefer one canonical location plus links instead of repeated prose.
- Remove or merge documents or sections that do not earn their place.
- Do not change source code unless needed to correct a documented command.

## Tasks

- [ ] Find duplicated, stale, contradictory, or misplaced information.
- [ ] Justify or delete each document and major section.
- [ ] Keep public docs generic: no private hostnames, credentials, or local
      operator details.
- [ ] Ensure documented workflows match actual repo configuration.
- [ ] Ensure links resolve.
- [ ] Ensure contributor workflow instructions do not diverge between docs and
      templates.

## Acceptance Criteria

- [ ] Public docs have clear ownership and no accidental policy duplication.
- [ ] Documents or sections that did not earn their place were removed or merged.
- [ ] README, CONTRIBUTING, DEVELOPMENT_PRINCIPLES, issue templates, and docs
      links resolve.
- [ ] Remaining duplication is intentional and recorded in the PR.

## Checks

- [ ] `just docs`
- [ ] `just lint`

## Final Report

- Final responsibility of each public-facing document:
- Moved or removed duplicated content:
- Remaining intentional duplication:
- Documented behavior that still lacks implementation or tests:
- Commands run:
