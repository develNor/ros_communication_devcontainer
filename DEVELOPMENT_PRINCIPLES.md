# Development Principles

## Definition Of Done

A change is complete only if:

1. Public behavior is implemented.
2. Unit or contract tests cover feasible host behavior.
3. Docker/E2E tests cover Docker runtime behavior.
4. README, docs, examples, and packaging metadata are updated when public behavior changes.
5. `just check` and relevant Docker checks pass.
6. Obsolete behavior is removed unless compatibility is explicitly requested.

## Compatibility Policy

Preserve documented CLI behavior and the legacy `start_rosotacom` and
`stop_rosotacom` entry points. Do not add silent aliases for removed behavior
unless a migration explicitly requires them.

## Testing Policy

Test public behavior and contracts.

Prefer:

- CLI parsing and dry-run-style dispatch tests,
- config path and environment precedence tests,
- packaged-resource and wheel smoke tests,
- docs and README example checks,
- Docker smoke tests for runtime behavior.

Avoid:

- broad mocks around pure path logic,
- tests that duplicate production code,
- hidden skips or xfails without an explicit reason.

## Docker Policy

Docker/runtime behavior requires `just test-e2e-fast` or a documented reason it
cannot sensibly be tested automatically.

Downloaded external binaries and source inputs should be versioned and, where
practical, checksum-verified in the lower-level `ros2docker` image layer.

## Dependency Policy

New dependencies require a clear runtime or dev-only reason, a license/security
check, and an explanation for why stdlib or existing dependencies are not enough.
