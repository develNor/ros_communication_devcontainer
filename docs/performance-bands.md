# Performance bands & the ratchet

The deterministic slice of the benchmark suite gates against **committed
two-sided bands** ([RFC 0007](rfcs/0007-regression-gate.md)). This page is the
day-to-day workflow; the RFC records the design and its rejected alternatives.

## The band store: `budgets.jsonl` (schema v2)

One JSON line per `(row, profile, metric)`:

```json
{"schema": 2, "row": "capacity-size", "profile": "rate-limited-capacity-ci",
 "metric": "capacity_size", "lo": 4900.0, "hi": 5100.0, "better": "higher",
 "provenance": {"fingerprint": "github-hosted-linux-x86_64", "window_s": 60.0,
                "repeats": 5, "sigma": 33.0, "floor": 100.0, "k": 3.0,
                "source_sha": "c44d4c5", "ratcheted_at": "2026-07-08T12:00:00",
                "note": "initial calibration"}}
```

- `[lo, hi]` is the accepted envelope; `better` says which way out of it is an
  improvement. The interval is closed: values on the edge are `WITHIN`.
- `provenance` is why the band is exactly this wide: the runner class that
  calibrated it, the measurement window, the K repeats behind `sigma`, and the
  half-width formula `max(k·sigma, floor)`.
- **Bands are never hand-edited.** They change only through
  `rosotacom benchmark ratchet`, so every move is a reviewed diff with a cause
  note. The file is written in a stable order — a ratchet is a one-line diff.

## Gating a run: `benchmark compare`

```bash
rosotacom benchmark compare <run-dir-or-result.json ...> --budgets budgets.jsonl
```

Per banded metric the verdict is `WITHIN`, `REGRESSED` (out on the worse side),
or `IMPROVED` (out on the better side). Exit codes:

| exit | meaning |
|---|---|
| 0 | all `WITHIN` (or `--monitor`, which reports without blocking) |
| 1 | `REGRESSED`, or a refusal (fingerprint mismatch, missing band/metric) |
| 2 | `IMPROVED` beyond the band — red too, but the fix is the printed ratchet command, not a revert |

`IMPROVED` is red so improvements get **banked**: the failure text prints the
exact `benchmark ratchet` invocation; run it and commit the tightened
`budgets.jsonl` in the same change. Without this, a later regression back to
the old level would sit invisibly inside a slack band.

`compare` **refuses** to judge a run against a band calibrated on a different
runner class (the `fingerprint` in every `result.json` vs the band's
provenance): a runner change must force a visible recalibration, never a
silent shift. Multiple result files are treated as repeats of one row: the
per-metric median is gated.

## Moving a band: `benchmark ratchet`

```bash
# bank an improvement (keeps the calibrated width, re-centers on the run):
rosotacom benchmark ratchet <run ...> --budgets budgets.jsonl --note "gop default changed"

# calibrate a new band, or deliberately move/widen one, from K fresh repeats:
rosotacom benchmark ratchet <run1> <run2> <run3> --budgets budgets.jsonl \
  --recalibrate --note "new runner class"
```

- A plain ratchet only turns one way: the worse edge may move toward better,
  never toward worse, and the calibrated width and provenance are preserved.
  Anything else — first bands, widening, accepting a worse level as a
  trade-off, a new runner class — is `--recalibrate`, which recomputes the
  half-width as `max(k·σ, floor)` from the given runs (`--k`, `--floor`,
  `--floor-frac`; defaults k=3, floor 2 % of the center — starting points
  until the runner-class calibration settles them per metric).
- Every move takes a one-line `--note`; a band diff without an explanation is
  a review red flag.

Banded metrics today come from `capacity` runs (`capacity_<knob>`) and
`recovery` runs (`t_recover_s`, `t_steady_s`, `recovery_burst`,
`lost_during_outage_total`); the benched-set registry (RFC 0007 checklist)
extends this to probe/replay rows.

The packaged `budgets.jsonl` currently carries the v1 entries rewritten to
schema v2 with the fingerprint `uncalibrated` — they refuse to gate until the
runner-class calibration mints measured bands, by design.
