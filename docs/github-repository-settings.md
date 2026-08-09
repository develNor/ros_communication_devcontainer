# GitHub Repository Settings

These settings live outside git and must be verified separately in every
repository using this shared codebase. The GitHub UI, CLI, and API remain the
live source of truth.

## Branch Protection

Expected shared state:

- `main` is the protected release line.
- The repository's active development/default branch is protected.
- Pull requests are required.
- The required status check is `ci-success` — the aggregate gate job in
  `pr-merge-gate.yml`, not the individual lanes. It `needs` all of them and runs
  under `if: always()`, so it turns red rather than vanishing when one fails.
  Requiring the lanes individually would mean editing this ruleset every time
  CI gains or renames a job; requiring the aggregate means CI can change shape
  without the ruleset drifting.
- "Require branches to be up to date before merging" (`strict`) is **off**. With
  auto-merge enabled it would make every merge invalidate all other open PRs,
  each then waiting for a rebase.
- The bypass list contains the Repository role `admin` and nothing else, so the
  owner is not locked out by a stuck check while the agent account still cannot
  merge around one. See "Identities" below.
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

# the two rules most likely to drift back, read straight from the ruleset
gh api "repos/$REPO/rulesets/$(gh api "repos/$REPO/rulesets" --jq '.[0].id')" \
  --jq '{bypass: [.bypass_actors[].actor_type],
         checks: [.rules[] | select(.type=="required_status_checks")
                  | .parameters.required_status_checks[].context],
         strict: [.rules[] | select(.type=="required_status_checks")
                  | .parameters.strict_required_status_checks_policy]}'

# who acts on the repository, and with what
gh api "repos/$REPO/collaborators" --jq '.[] | "\(.login) \(.role_name)"'
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

## Identities

Two accounts act on this repository, and the difference between them is the
point: work done by an agent must be attributable and must not be able to
change the rules it runs under.

| | `develNor` | `develNor-agent` |
|---|---|---|
| Relation | owner | collaborator |
| Repository permission | admin | write |
| Ruleset bypass | yes (Repository role `admin`) | no |
| Credential | interactive login | classic PAT in `.agents/github.env` |

**A personal repository has no role picker.** Adding a collaborator to a
repository owned by a user — as opposed to an organization — grants write, with
no read/triage/maintain/admin choice offered. So the owner/agent boundary is not
drawn by the collaborator entry; it is drawn by two things that *are*
configurable: the agent is not admin, and the ruleset's bypass list names the
`admin` role only. An agent therefore cannot merge without `ci-success`, cannot
force-push, and cannot edit the ruleset that says so.

**Pushing must use HTTPS.** `origin` is an SSH URL, and SSH authenticates by
key, not by token: with an SSH remote every push lands under whoever owns the
key, no matter which `GH_TOKEN` is set. An agent push must therefore name an
HTTPS URL carrying its own PAT. Getting this wrong is silent — the push
succeeds, under the wrong name.

```bash
set -a; . .agents/github.env; set +a
git push "https://${GITHUB_USER}:${GITHUB_PAT_CLASSIC}@github.com/develNor/ros_communication_devcontainer.git" HEAD
```

**What the agent can verify for itself.** A write collaborator reads more repo
settings than expected, which is what makes autonomous advice about them
possible. Measured against this repository:

| Endpoint | agent | owner |
|---|---|---|
| `rulesets` | yes | yes |
| `actions/variables`, `actions/secrets` (names) | yes | yes |
| `environments` | yes | yes |
| merge settings (`allow_auto_merge`, …) | yes | yes |
| `actions/permissions/workflow` | **403** | yes |
| `vulnerability-alerts` | **404** | yes |
| `hooks` | **404** | yes |
| `security_and_analysis` on the repo object | **null** | yes |

Scope is not the limit here — the PAT already carries `read:repo_hook` and still
gets 404 on `hooks`, because repository role decides, not token scope. The four
denied items are all owner-only checks; an agent asked to audit them should say
so rather than report an absent value as a finding.

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
