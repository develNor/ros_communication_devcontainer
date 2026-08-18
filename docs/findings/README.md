# Public Findings Ledger

This ledger records one measured communication effect per file when the effect is
reproducible from public material: packaged sessions, public example profiles,
synthetic loads, anonymized replay fixtures, or deterministic emulation. Findings
that require private bags, private hosts, real cellular/WLAN links, or operator
calibration data stay in the operator harness ledger.

Each finding keeps the export-oriented schema:

- Claim
- Setup
- Evidence
- `Verification:` entry
- Status
- Publication notes

Run the local schema and index check with:

```bash
python3 tests/contract/test_findings.py
python -m pytest -q tests/contract/test_findings.py
```

The check is intentionally stdlib-only when run as a script, so docs and
contract lanes can call the same logic. RFC 0007 defines why findings need a
living verification hook.

## Findings

- [Best-effort reordering becomes reader-side loss, independent of history depth](reorder-becomes-reader-loss.md) - under pure delay jitter a Cyclone best-effort reader discards every overtaken sample (57% at 100 Hz under 50+-45 ms), delivery stays strictly monotonic, and depth 50 loses the same as depth 1.
- [Jitter causes loss while bandwidth shortage builds latency](jitter-loss-bandwidth-latency.md) - 18 KB at 20 Hz separates jitter-induced loss from bandwidth-induced queueing.
- [Lighter alternating messages can lose more than steady messages](lighter-message-loss.md) - a 1x18KB+1x0KB pattern lost under a tight emulated profile while the steady 18 KB comparison did not.
- [Delay alone was not the loss boundary for the 18 KB at 20 Hz probe](delay-alone-no-loss.md) - pure delay profiles showed no material loss, and the 300 ms delay-only probe delivered all messages.
- [Zero-loss boundaries for 18 KB at 20 Hz](zero-loss-boundaries-18kb-20hz.md) - 3.2 Mbit/s and 18 ms jitter were good boundaries; 3.1 Mbit/s and 27 ms jitter were bad.
- [Head-of-line blocking makes irregularity the dominant failure mode](head-of-line-irregularity.md) - alternating heavy/light payloads can lose more than a steady heavier stream.
- [Cyclone DDS SPDP discovery bursts can distort tight-link tail latency](cyclone-spdp-discovery-bursts.md) - default 30 s discovery traffic shares the OTA link with payload and is annotated in benchmark diagnostics.
