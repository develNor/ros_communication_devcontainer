---
name: Maintainer review
about: Review a pull request from a strict maintainer perspective.
title: "Review PR #"
labels: ready
assignees: ""
---

## Goal

Review the referenced PR as a strict maintainer.

## PR

- PR URL or number:

## Review Focus

- [ ] Scope creep.
- [ ] Missing or weak tests.
- [ ] Documentation drift.
- [ ] Accidental public behavior changes.
- [ ] Packaging or release risks.
- [ ] CI/pre-commit mismatch.

## Acceptance Criteria

- [ ] Blocking issues are listed first with file/line references.
- [ ] Non-blocking suggestions are clearly separated.
- [ ] The final recommendation is `merge` or `request changes`.
