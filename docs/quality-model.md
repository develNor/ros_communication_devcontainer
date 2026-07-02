# Quality Model

`rosotacom` is issue-driven and often developed through small autonomous PRs. That
keeps work moving, but it also creates predictable drift: names lag behind the
code, local fixes pile up, public docs fall out of date, and process files can
grow without changing an owner-visible outcome.

The repository handles that with two kinds of checks.

## Hard Checks

Hard checks are deterministic and block merges. They live in CI, the `justfile`,
pre-commit, and contract tests. If a rule can be expressed objectively, prefer a
hard check over reviewer judgement.

Hard checks include:

- Python linting, formatting, type checking, and host tests.
- Contract tests for docs reachability, public examples, packaging, workflow
  wiring, and repository policy.
- Package validation.
- Docker-backed smoke tests for the public merge gate.
- Release validation from a deliberate `vX.Y.Z` tag.

See [ci.md](ci.md) and [testing.md](testing.md) for the current lanes.

## Soft Checks

Some questions are not reliably algorithmic: whether a module boundary still
fits, whether a document earns its place, whether a public claim is still useful,
or whether a process artifact is ritual rather than load-bearing. Those are
handled as soft checks through issue templates.

Soft checks are not CI gates. They are focused issue-driven audits and cleanup
passes that produce one PR per actionable finding. A healthy repository may
produce no PRs after diagnosis; that is a successful result.

## Task Hierarchy

GitHub issue templates are flat files, but they form a task hierarchy:

```text
owner-runbook
+-- quality-workflow          orchestrator: diagnose first, then execute findings
|   +-- repository-diagnosis   read-only keystone; triages what, if anything, to do
|   +-- test-ci-audit          leaf: tests, CI wiring, and coverage
|   +-- documentation-audit    leaf: public docs, ownership, and drift
|   +-- implementation-cleanup leaf: small implementation simplification
|   +-- redesign               leaf: one diagnosed cross-cutting concern
+-- release-process           leaf: prepare, validate, describe, and publish
+-- work-item                 leaf: ordinary actionable work
```

| Template | Role |
| --- | --- |
| [quality-workflow](../.github/ISSUE_TEMPLATE/quality-workflow.md) | Owner-facing orchestrator for a quality pass. |
| [repository-diagnosis](../.github/ISSUE_TEMPLATE/repository-diagnosis.md) | Read-only diagnosis and triage. |
| [test-ci-audit](../.github/ISSUE_TEMPLATE/test-ci-audit.md) | Audits tests, CI wiring, and executable coverage. |
| [documentation-audit](../.github/ISSUE_TEMPLATE/documentation-audit.md) | Removes or fixes stale, duplicated, misplaced, or disproportionate docs. |
| [implementation-cleanup](../.github/ISSUE_TEMPLATE/implementation-cleanup.md) | Simplifies code while preserving the public contract. |
| [redesign](../.github/ISSUE_TEMPLATE/redesign.md) | Handles one diagnosed concern that needs a coherent vertical slice. |
| [release-process](../.github/ISSUE_TEMPLATE/release-process.md) | Prepares and validates a release. |
| [work-item](../.github/ISSUE_TEMPLATE/work-item.md) | Tracks any other coherent issue and PR. |

## Task Contract

Every issue template shares one contract. Templates link here instead of
restating policy differently:

- **Workflow**: [CONTRIBUTING.md](../CONTRIBUTING.md) defines setup, branches,
  local checks, CI, PR flow, merge policy, and releases.
- **Quality**: [DEVELOPMENT_PRINCIPLES.md](../DEVELOPMENT_PRINCIPLES.md) defines
  the definition of done, compatibility, testing, Docker, and dependency policy.
- **Tracking**: [work-items.md](work-items.md) defines issue ownership, labels,
  PR linkage, and status reporting.
- **Test integrity**: never skip, weaken, delete, or hide a check to make a
  change pass.
- **Documentation traceability**: every public Markdown document must be
  reachable by following links from the README, unless it is a narrow UI template
  or release-note artifact covered by the contract test allowlist.

A read-only task, such as repository diagnosis, follows only the parts that
apply: quality rules, tracking, and final reporting. It does not create a branch
or PR.

## Owner And Agent Layers

The owner layer is intentionally small: [owner-runbook.md](owner-runbook.md)
lists the entrypoints an owner normally invokes.

The agent layer contains the issue templates and supporting docs used to execute
those entrypoints. Each agent-layer artifact must be load-bearing. Test that with
the deletion counterfactual: if this file or section were removed, which
owner-visible outcome would change? Valid answers include a different PR being
created, a merge being gated, a release being blocked, a test being run, or a
published artifact changing. If the answer is "none", route the artifact for
removal or make it load-bearing.

## Diagnosis First

The quality pass starts with
[repository-diagnosis](../.github/ISSUE_TEMPLATE/repository-diagnosis.md). It
reads the repository and classifies findings along two axes.

Altitude:

- **Vision**: the README and stated purpose still match the repository.
- **Architecture**: package structure, major boundaries, and naming in the
  large.
- **Module / interface**: cohesion, naming, and public surface of one unit.
- **Line / local**: dead code, duplication, or local complexity.
- **Meta / governance**: docs, templates, and policies earn their size.

Disposition:

- **None**: healthy; leave it.
- **Inline**: small enough to fold into a matching leaf task.
- **Scoped**: create a focused audit, cleanup, or work-item issue.
- **Redesign**: create a vertical-slice redesign issue for one diagnosed concern.

Apply these lenses at every altitude:

- **Value / proportionality**: each artifact earns its place and is stated in the
  canonical location.
- **Generality**: public repository docs describe roles and policy, not private
  hostnames, local credentials, or operator-specific setup.
- **Drift**: documented commands, config, CI, and release steps match the repo.

## RFC Validation

RFCs are design records and resumable implementation trackers. Each RFC must
carry a validation checklist beside its implementation checklist. Every
capability introduced by the RFC needs a named test, example, CI lane, or
explicit manual check. Check a validation item only after that verification
exists and runs.

Soft checks may route work into RFC updates when the implemented behavior and the
design record diverge. They must not weaken the RFC validation rule.

## Public CI And External OTA Gates

The public GitHub merge gate proves the local host behavior, package integrity,
and Docker-backed single-machine smoke matrix. It deliberately does not prove the
operator's full two-host OTA environment.

Promotion to `main` or downstream synchronization may require an external OTA
gate supplied by the repository operator. Public docs should describe that gate
generically and link to [testing.md](testing.md); private hostnames, credentials,
and deployment details stay outside this repository.

## Decomposition

Do not collapse a quality pass into one broad PR. Depth comes from fresh context
per goal:

- Start with read-only diagnosis.
- Open one focused issue per actionable finding.
- Start each implementation from a fresh `origin/develop` branch unless the
  issue explicitly targets `main` or release work.
- Wait for prerequisite PRs to merge before starting dependent work.
- If CI fails, fix the current PR before moving on.

## Document Ownership

Keep each public-facing document focused:

- **README**: user-facing purpose, installation, quick start, common usage, and
  links into the docs graph.
- **CONTRIBUTING**: contributor setup, checks, branch policy, PR workflow, CI,
  and releases.
- **DEVELOPMENT_PRINCIPLES**: definition of done and engineering policy.
- **docs/ci.md**: public GitHub CI and branch model.
- **docs/testing.md**: test taxonomy, Docker smoke, and external OTA gate.
- **docs/release.md**: release mechanics.
- **docs/work-items.md**: issue-driven work tracking.
- **docs/owner-runbook.md**: owner entrypoints and owner-only gates.
- **docs/quality-model.md**: hard vs. soft checks, task hierarchy, and document
  ownership.
- **Issue templates**: executable task recipes.

Link instead of restating when a document needs another document's concern.
