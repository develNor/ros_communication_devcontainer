# GitHub Repository Settings

These settings live outside git and must be verified separately in every
repository using this shared codebase. The GitHub UI, CLI, and API remain the
live source of truth.

## Branch Protection

Expected shared state:

- `main` is the protected release line.
- The repository's active development/default branch is protected.
- Pull requests are required.
- The required status check is `ci-success`.
- Squash merges are allowed.
- CodeQL default setup is enabled for Actions and Python.
- The `release` environment and `PYPI_PUBLISH_URL` variable point at this
  repository's intended package index.

Verify:

```bash
REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
DEFAULT_BRANCH="$(gh repo view --repo "$REPO" --json defaultBranchRef --jq .defaultBranchRef.name)"
gh ruleset list --repo "$REPO" --parents
gh ruleset check "$DEFAULT_BRANCH" --repo "$REPO"
gh ruleset check main --repo "$REPO"
gh api "repos/$REPO/code-scanning/default-setup"
gh variable get PYPI_PUBLISH_URL --repo "$REPO"
```

`gh ruleset check` reports which rules apply, not which status checks they
require. A ruleset can be named for CI and still carry no
`required_status_checks` rule, in which case auto-merge will merge a pull
request whose checks are red. Read the rule types out directly:

```bash
gh api "repos/$REPO/rulesets" --jq '.[].id' \
  | xargs -I{} gh api "repos/$REPO/rulesets/{}" \
      --jq '{name, rules: [.rules[] | select(.type=="required_status_checks")
             | .parameters.required_status_checks[].context]}'
```

Expected output names `ci-success`. An empty list is the drift this section
exists to catch.

## Trusted Publishers

PyPI authorizes an upload by matching the OIDC claims of the *workflow file*
that requests it, so a publisher registration is per filename, not per
repository. This codebase publishes from two:

| Workflow | Publishes | Registration |
|---|---|---|
| `release.yml` | stable `vX.Y.Z` tags | required |
| `dev-release.yml` | `X.Y.devN` from the development branch | required for the development channel |

A missing registration fails at upload with `invalid-publisher: valid token, but
no corresponding publisher`, after a completely successful build. The message
prints the claims it presented; `workflow_ref` is the filename to register.

Register on PyPI under the project's *Publishing* settings: owner, repository,
the workflow filename, and an empty environment (this codebase deliberately uses
no GitHub environment, so that no repository sharing it needs environment-admin
rights).

## Repository Metadata

Expected topics include `ros2`, `docker`, `robotics`, `teleoperation`, and
`communication`.

Verify:

```bash
gh repo view --json description,homepageUrl,repositoryTopics
```

## Drift Handling

If GitHub state differs from this document, either update the live setting to
match the documented contract or update this document in a PR if policy changed.
