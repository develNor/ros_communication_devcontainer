# Work Items

Use GitHub Issues for actionable work, repo files for reusable knowledge, and
local files only for private scratch.

## Where Work Lives

- Real actionable work lives in GitHub Issues.
- Reusable work recipes live in `.github/ISSUE_TEMPLATE/`.
- In-flight PR checklists live in the PR body.
- Project policy and definition of done live in committed repository docs.

## Issue-Driven Workflow

Use the matching issue template when one applies:

```bash
gh issue create --repo develNor/rosotacom --template work-item.md
gh issue create --repo develNor/rosotacom --template test-ci-audit.md
gh issue create --repo develNor/rosotacom --template documentation-audit.md
```

Link PRs to issues with `Fixes #<number>`, `Closes #<number>`, or
`Refs #<number>`.

## Triage Labels

- `ready`: issue is accepted and ready for a contributor.
- `backlog`: issue is tracked for later.
- `maybe`: issue is saved for later consideration.
- `documentation`: documentation-only or documentation-heavy work.
- `bug`: incorrect behavior.
- `enhancement`: new feature or request.
- `dependencies`: dependency updates.
