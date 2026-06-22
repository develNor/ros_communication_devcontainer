# RFCs — design records

Design rationale for rosotacom's test and measurement architecture. Each RFC is
self-contained; later ones extend earlier ones.

| RFC | Title | Status |
|---|---|---|
| [0001](0001-expectation-driven-test-suite.md) | Expectation-driven, OTA-first test suite | Implemented |
| [0002](0002-expectation-concepts.md) | Richer expectation concepts (presence / mode / completeness / overhead) | Implemented |
| [0003](0003-metric-backbone.md) | Metric backbone (per-message transit records, offset-aware latency, real loss) | Implemented |
| [0004](0004-network-profiles.md) | Network profiles & the fidelity ladder (per-direction netem, profile-bound expectations) | Draft |
| [0005](0005-benchmark-genres-and-ci.md) | Benchmark genres & CI distribution (sweep/capacity + recovery, budgets, gate vs monitor) | Draft |
| [0006](0006-dynamic-qos.md) | Dynamic QoS (mirror correctness durability, shape bandwidth reliability/depth) | Draft |

Add a new RFC as `NNNN-title.md` and list it here, so it stays reachable from the
README (see the documentation-traceability rule in `AGENTS.md`).

## Required sections

Beyond the design narrative, every RFC carries two checklists:

- **Implementation checklist** — what to build, as actionable checkboxes.
- **Validation checklist** — *how each capability is proven*. One entry per
  capability the RFC introduces, naming the test, example session, or CI lane that
  exercises it. Where automation is genuinely impossible or disproportionate, name
  a referenced **manual check** instead (e.g. "operator confirmed the MCAP is
  written under `logs/<peer>/metrics/`"), so the gap is explicit rather than
  silent.

Prefer automated verification — roughly host unit/contract test > an example run in
the CI smoke matrix > a scripted check > a documented manual check. Add validation
in the same change as the implementation, and check a box only once that
verification actually exists and runs. The Validation checklist is what lets a
reviewer confirm an "Implemented" RFC is genuinely covered, not merely asserted.
