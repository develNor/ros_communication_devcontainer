# AGENTS.md

These instructions are for agents working in this repository. They add local
operating details; they do not replace the shared contributor workflow.

Read first:

- `README.md` for user-facing behavior.
- `CONTRIBUTING.md` for setup, checks, CI, PR workflow, and releases.
- `DEVELOPMENT_PRINCIPLES.md` for quality rules.
- `docs/work-items.md` for issue-driven work tracking.
- `.github/ISSUE_TEMPLATE/` for reusable task recipes.

Shared workflow:

- Follow `CONTRIBUTING.md`.
- Check `git status --short --branch` before editing.
- Treat unrelated local changes as user-owned and do not revert them.
- Update tests and docs when CLI, config, package, Docker, or public runtime behavior changes.
- Keep docs reachable: every doc must be linkable from `README.md` (directly, or via a doc already linked there — e.g. a new RFC goes in `docs/rfcs/README.md`). Link new docs in the same change, and link any orphan you notice.
- Do not skip, weaken, or delete tests/CI to make a change pass.
- Default to a draft review PR unless autonomous merge is explicitly requested.

Working with RFCs:

- Before implementation, make sure the RFC contains actionable checkbox tasks.
  Add, rephrase, or deduce them when the design is only prose.
- Keep the RFC open while implementing. Record relevant discoveries, reality
  checks, and necessary design changes as they arise.
- Check off completed work immediately and leave unfinished work explicit.
- Treat the RFC as the resumable source of truth: progress and replans must be
  current and self-contained enough for another contributor to continue from
  the exact stopping point.
