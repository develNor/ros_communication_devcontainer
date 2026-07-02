---
name: Quality workflow
about: Run the multi-PR repository quality workflow.
title: "Run repository quality workflow"
labels: ready
assignees: ""
---

## Goal

Run the repository quality pass: diagnose first, then create one focused PR for
each actionable finding the diagnosis triages. A healthy repository may yield no
PRs at all; that is a successful outcome.

## Autonomous Mode

- [ ] This issue explicitly requests autonomous PR creation and auto-merge.

If autonomous mode is not checked, open draft review PRs by default and stop
after each PR is ready for review.

## Constraints

- Follow the shared [task contract](../../docs/quality-model.md#task-contract).
- Create one focused PR per goal.
- Do not combine unrelated goals into one PR.
- Start each implementation goal from latest `origin/develop` unless the
  diagnosis names a release or `main`-promotion task.
- Do not start a dependent goal until prerequisite PRs merge.
- If CI fails, fix the current PR instead of starting a new goal.
- If a PR cannot be merged automatically, stop and report the blocker.

## Execution Plan

- [ ] Create and complete an issue from
      [repository-diagnosis.md](repository-diagnosis.md). This read-only pass
      triages findings by altitude and disposition and proposes the issues to
      open. If it finds nothing actionable, report that and stop here.
- [ ] For each actionable finding, in the diagnosis's priority order, create and
      complete one focused issue using the leaf template it names:
      [test-ci-audit.md](test-ci-audit.md),
      [documentation-audit.md](documentation-audit.md),
      [implementation-cleanup.md](implementation-cleanup.md),
      [redesign.md](redesign.md),
      [release-process.md](release-process.md), or [work-item.md](work-item.md).

## Acceptance Criteria

- [ ] The diagnosis ran first and its triage is recorded.
- [ ] Each actionable goal has a separate issue and PR.
- [ ] Each PR links its issue and reports checks run.
- [ ] Dependent goals start only after prerequisite PRs merge.
- [ ] If the diagnosis found no action, the final report records that outcome and
      the probes that support it.

## Checks

Use the checks listed in each generated issue template.

## Final Report

- Diagnosis summary or no-action result:
- Issues created:
- PRs created:
- PR URLs and CI status:
- Merged PRs:
- Net line delta for product vs. governance/process changes:
- Blockers:
