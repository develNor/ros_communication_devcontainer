# AGENTS.md

These instructions are for agents working in this repository. They add local
operating details; they do not replace the shared contributor workflow.

Read first:

- `README.md` for user-facing behavior.
- `CONTRIBUTING.md` for setup, checks, CI, PR workflow, and releases.
- `DEVELOPMENT_PRINCIPLES.md` for quality rules.
- `docs/work-items.md` for issue-driven work tracking.
- `docs/quality-model.md` for hard vs. soft checks, the task hierarchy, and the
  shared task contract.
- `docs/owner-runbook.md` for owner-facing entrypoints and gates.
- `.github/ISSUE_TEMPLATE/` for reusable task recipes.

Shared workflow:

- Follow `CONTRIBUTING.md`.
- Check `git status --short --branch` before editing.
- For every repository-changing task, create or identify a GitHub issue first.
  Fetch current remote state with `git fetch origin --prune`, create a fresh
  sibling worktree from `origin/develop` unless the issue explicitly requires a
  different base, implement there, then push a branch and open a PR.
- Keep the original checkout untouched. Treat unrelated dirty state in any
  checkout or worktree as user-owned; do not clean, reset, or reuse it for
  implementation work.
- Name branches so the issue and purpose are obvious, for example
  `issue-65-agent-workflow`.
- Treat unrelated local changes as user-owned and do not revert them.
- Update tests and docs when CLI, config, package, Docker, or public runtime behavior changes.
- Validate what you build: every behaviour you implement should be verified by something that runs, wherever possible and sensible. Prefer automation (unit/contract test > an example exercised in CI smoke > a scripted check); fall back to a documented manual check only when automation is genuinely impossible, and say why. Add the verification in the same change as the behaviour.
- Keep docs reachable: every doc must be linkable from `README.md` (directly, or via a doc already linked there — e.g. a new RFC goes in `docs/rfcs/README.md`). Link new docs in the same change, and link any orphan you notice.
- Run quality workflow issues diagnosis-first: use
  `.github/ISSUE_TEMPLATE/repository-diagnosis.md` before opening audit,
  cleanup, or redesign work, and create only the follow-up issues that diagnosis
  names.
- Do not skip, weaken, or delete tests/CI to make a change pass.
- Enable GitHub auto-merge after opening the PR whenever repository policy
  allows it. Use this repository's allowed/default merge method; currently only
  squash merge is enabled, so use `gh pr merge --auto --squash`. Do not bypass
  protected-branch rules, required approvals, or failing checks; if auto-merge
  is unavailable, record why in the PR and final status.
- Keep the PR description current with the issue link, validation commands,
  important SHAs, CI status, and any dependency on another PR or harness MR.
- Before reporting completion, check the remote PR state and CI results with
  `gh pr view` / `gh pr checks`; report the actual status, not just that a
  branch was pushed.

Practical notes:

- If a task is requested from the harness but the change is in this repository,
  create the GitHub issue/PR here and mention it from the harness issue/MR.
  Update the harness submodule pointer only when the harness branch must pin
  this exact commit.
- When testing this repo through a shared virtualenv from another checkout, make
  the import path explicit, e.g. `PYTHONPATH=/path/to/worktree/src ...`, or run
  pytest from this worktree so `pyproject.toml` sets `pythonpath = ["src"]`.
- Docker-backed checks can leave containers or networks behind after hard
  timeouts. If a smoke or benchmark run fails with `network with name
  rosotacom-smoke already exists`, inspect and remove attached containers before
  removing the network.
- GitHub `fast-e2e` may take tens of minutes because it runs Docker-backed smoke
  and benchmark lanes. Do not call a PR ready until that check resolves or its
  failure log has been inspected.

Working with RFCs:

- Before implementation, make sure the RFC contains actionable checkbox tasks.
  Add, rephrase, or deduce them when the design is only prose.
- Keep the RFC open while implementing. Record relevant discoveries, reality
  checks, and necessary design changes as they arise.
- Check off completed work immediately and leave unfinished work explicit.
- Treat the RFC as the resumable source of truth: progress and replans must be
  current and self-contained enough for another contributor to continue from
  the exact stopping point.
- Every RFC carries a **Validation checklist** next to its implementation
  checklist: one entry per capability the RFC introduces, naming the test,
  example, or CI lane that proves it — or, where automation is genuinely
  impossible, a referenced manual check (e.g. "operator confirmed the MCAP is
  written under `logs/<peer>/metrics/`"). Prefer automated checks; treat manual
  ones as the explicit fallback, not the default. Check a validation item only
  once that verification actually exists and runs, and add it in the same change
  as the implementation it covers. See `docs/rfcs/README.md` for the required
  RFC sections.
