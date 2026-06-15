Quality audit rules:

- Prefer deterministic checks over subjective judgement.
- Public-facing claims must be backed by code, tests, CI, or a manual verification note.
- Do not broaden the task beyond the assigned scope.
- Do not weaken or remove existing tests.
- Do not hide missing coverage with broad skips or xfails.
- If behavior cannot sensibly be tested automatically, document a manual check.
- Keep changes small, reviewable, and aligned with the current public contract.
- Prefer simple implementation over abstraction unless the abstraction is clearly justified.
