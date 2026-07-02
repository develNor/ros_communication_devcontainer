---
name: Repository diagnosis
about: Read-only diagnosis and triage that decides what, if anything, the repo needs.
title: "Diagnose repository and triage follow-up work"
labels: ready
assignees: ""
---

## Goal

Take in the whole repository and produce a prioritized diagnosis that decides
what work, if any, the repository needs. This is the planning keystone of the
quality pass: it routes work, it does not edit files.

## How This Fits

This runs first in the soft-check pass described by
[docs/quality-model.md](../../docs/quality-model.md). The
[quality-workflow](quality-workflow.md) orchestrator then creates and completes
exactly the issues this triage names. It may name none.

## Constraints

- Read-only: do not change code, docs, tests, or config in this task.
- Follow the shared [task contract](../../docs/quality-model.md#task-contract);
  only the quality, tracking, and reporting parts apply because this task opens
  no PR.
- Classify by what a finding needs, not by the smallest patch available.
- "No action needed" is valid only when it names the probes that would have
  caught a problem.
- Keep public findings generic. Do not include private hostnames, credentials,
  or operator-specific deployment details.

## Diagnose Along Two Axes

Altitude:

- Vision.
- Architecture.
- Module / interface.
- Line / local.
- Meta / governance.

Disposition:

- None.
- Inline.
- Scoped.
- Redesign.

Use [docs/quality-model.md](../../docs/quality-model.md#diagnosis-first) for the
meaning of each axis and disposition.

## Lenses

- Value / proportionality: every doc, template, and process step must be
  load-bearing for an owner-visible outcome.
- Generality: public docs use roles and generic external-gate language, not
  private deployment details.
- Drift: documented commands, checks, CI, release steps, and RFC state match the
  repository.
- Runtime relevance: Docker smoke, packaged examples, ROS/catmux resources, and
  external OTA gates are distinct and should not be conflated.

## Tasks

- [ ] Read top-down: README, CONTRIBUTING, development principles, docs, issue
      templates, tests, package config, source, and workflows.
- [ ] Record findings with altitude, disposition, target template, and rationale.
- [ ] Include an explicit "no action" section with the probes that found healthy
      areas.
- [ ] Check product vs. governance/process size and whether the governance layer
      earns its current size.
- [ ] For each reusable issue template, state the owner-visible outcome that
      would change if it were deleted.
- [ ] Check RFC implementation and validation checklists for drift.
- [ ] Order actionable findings by priority and dependencies.
- [ ] Propose one issue per actionable finding, naming the template to use.
- [ ] If nothing is actionable, say so explicitly.

## Acceptance Criteria

- [ ] Every finding has an altitude and disposition.
- [ ] Every actionable finding maps to one proposed issue and template.
- [ ] Every `None` disposition cites a meaningful probe.
- [ ] The report distinguishes public GitHub checks from external OTA promotion
      gates.
- [ ] The report includes product-vs-governance proportionality and issue-template
      reachability/load-bearing notes.
- [ ] No files were changed by this task.

## Final Report

Triage table:

| Finding | Altitude | Disposition | Target template | Rationale |
| --- | --- | --- | --- | --- |

- No action needed, with probes:
- Product vs. governance/process size verdict:
- Issue-template load-bearing notes:
- RFC validation drift:
- Proposed issues, in priority order:
- Dependencies:
