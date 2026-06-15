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

- tests/
- pyproject.toml
- .pre-commit-config.yaml
- .github/workflows/*
- README and docs that describe testing

## Tasks

- [ ] Map public README-documented commands to tests where feasible.
- [ ] Add missing tests for feasible documented behavior.
- [ ] Record manual verification for behavior that cannot sensibly be automated.
- [ ] Verify that local commands, pre-commit, CI, and release validation agree.

## Checks

- [ ] `just lint`
- [ ] `just typecheck`
- [ ] `just test-unit`
- [ ] `just test-contract`
- [ ] `just docs`
- [ ] `just check`
- [ ] `just test-e2e-fast`
