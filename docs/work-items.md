# Work Items

Use GitHub Issues for actionable work, repo files for reusable knowledge, and
local files only for private scratch.

## Where Work Lives

- Real actionable work lives in GitHub Issues.
- Reusable work recipes live in `.github/ISSUE_TEMPLATE/`.
- In-flight PR checklists live in the PR body.
- Project policy and definition of done live in committed repository docs.

## Issue-Driven Workflow

Use the matching issue template when one applies. For broad quality work, start
with `repository-diagnosis.md` through the `quality-workflow.md` orchestrator;
the diagnosis decides which leaf issues to open.

```bash
gh issue create --repo develNor/ros_communication_devcontainer --template work-item.md
gh issue create --repo develNor/ros_communication_devcontainer --template quality-workflow.md
gh issue create --repo develNor/ros_communication_devcontainer --template repository-diagnosis.md
```

Link PRs to issues with `Fixes #<number>`, `Closes #<number>`, or
`Refs #<number>`.

For the shared task contract and template hierarchy, see
[quality-model.md](quality-model.md).

## Working An Issue

Before implementation starts, create or choose a well-scoped issue with the
matching template when one applies.

- Assign yourself to the issue while working.
- Keep one coherent change per issue and PR.
- Link the PR with `Fixes #<number>`, `Closes #<number>`, or `Refs #<number>`.
- If implementation shows that the issue assumptions are wrong or incomplete,
  add an issue comment describing the discovery and the chosen scope.
- If the work is not possible or not sensible, add an issue comment explaining
  why and apply a fitting triage label such as `question`, `invalid`, or
  `help wanted`.
- Report the PR URL and CI status after opening or updating the PR.

## Triage Labels

- `ready`: issue is accepted and ready for a contributor.
- `backlog`: issue is tracked for later.
- `maybe`: issue is saved for later consideration.
- `documentation`: documentation-only or documentation-heavy work.
- `bug`: incorrect behavior.
- `enhancement`: new feature or request.
- `dependencies`: dependency updates.
