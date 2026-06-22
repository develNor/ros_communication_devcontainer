# RFCs — design records

Design rationale for rosotacom's test and measurement architecture. Each RFC is
self-contained; later ones extend earlier ones.

| RFC | Title | Status |
|---|---|---|
| [0001](0001-expectation-driven-test-suite.md) | Expectation-driven, OTA-first test suite | Implemented |
| [0002](0002-expectation-concepts.md) | Richer expectation concepts (presence / mode / completeness / overhead) | Implemented |
| [0003](0003-metric-backbone.md) | Metric backbone (per-message transit records, offset-aware latency, real loss) | Draft |
| [0004](0004-network-profiles.md) | Network profiles & the fidelity ladder (per-direction netem, profile-bound expectations) | Draft |

Add a new RFC as `NNNN-title.md` and list it here, so it stays reachable from the
README (see the documentation-traceability rule in `AGENTS.md`).
