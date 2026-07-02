---
name: Test and CI audit
about: Audit tests, CI wiring, and coverage of documented behavior.
title: "Audit test and CI coverage"
labels: ready
assignees: ""
---

## Goal

Audit and improve test coverage and test execution wiring.

## Scope

- tests/.
- pyproject.toml.
- .pre-commit-config.yaml.
- .github/workflows/*.
- README and docs that describe testing.
- Minimal source changes only if required to make behavior testable.

## Constraints

- Follow the shared [task contract](../../docs/quality-model.md#task-contract).
- Do not refactor implementation except where needed to make tests possible.

## Tasks

- [ ] Map public README-documented commands, features, and CLI behavior to tests
      where feasible.
- [ ] Add missing tests for feasible documented behavior.
- [ ] Record manual verification for behavior that cannot sensibly be automated.
- [ ] Verify that tests are collected and actually executed.
- [ ] Check that tests are not accidentally skipped, deselected, ignored, or
      excluded by config.
- [ ] Ensure skipped/xfail tests have explicit reasons.
- [ ] Verify that local commands, pre-commit, CI, and release validation agree.

## Acceptance Criteria

- [ ] Documented public behavior has automated coverage or a recorded manual
      verification reason.
- [ ] Local test commands and CI workflows agree on the intended gates.
- [ ] Changed behavior includes tests before the PR is marked ready.

## Checks

- [ ] `just lint`
- [ ] `just typecheck`
- [ ] `just test-unit`
- [ ] `just test-contract`
- [ ] `just docs`
- [ ] `just check`
- [ ] `just test-e2e-smoke`

## Final Report

- Changed files:
- Added or fixed tests:
- Remaining untested behavior and why:
- Commands run:
