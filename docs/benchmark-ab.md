# A/B tuning experiments (`rosotacom benchmark ab`)

`rosotacom` is a tuning suite as much as a measurement one: the *fix* step of the
closed-loop workflow needs a first-class answer to **"does candidate config B
beat baseline config A on the same load and profile — what improved, what
regressed, what is unchanged?"** `benchmark ab` is that verdict.

It is the sibling of the regression gate (RFC 0005 / RFC 0007). The gate asks
"is this run within a committed envelope?"; A/B asks "is B better or worse than
A, right now, on the same load?". Both are two-sided and both speak the same
**IMPROVED / WITHIN / REGRESSED** language — an A/B cell is exactly a band
verdict on the ephemeral `[baseline ± tolerance]` envelope.

## What varies, what is held constant

| Held constant across every run | Varied between configs |
|---|---|
| the load — the synthetic `sized_publisher` stream on `a_to_b` (`--size`/`--size-pattern`, `--rate-hz`, `--streams`) | the **session config**: the OTA pipeline knobs (`throttle_hz`, `drop`, QoS reliability/depth/lifespan, compression, ffmpeg settings, …) |
| the network profile and its seed policy (`--profile`) | |
| the RMW and any run-wide QoS override (`--rmw`, `--qos-*`) | |

Each `--baseline` and `--candidate` is a **whole session config** — a directory
or a `session-definition.yaml`, no patch format. Every config must carry the same
`a_to_b` load topic(s) (that is what the synthetic publisher drives); they should
differ *only* in the pipeline knob under test. The run records the YAML diff of
each candidate against the baseline (`configs/<label>.diff`) so what actually
changed is explicit and reviewable — and so the "same load" assumption is
auditable rather than assumed.

## Running it

```bash
rosotacom benchmark ab \
  --profile cellular-4g-degraded \
  --baseline configs/gop30 \
  --candidate gop15=configs/gop15 \
  --candidate gop15-crf28=configs/gop15_crf28 \
  --size 18000 --rate-hz 20 --duration 20 --repeats 5
```

`--candidate` is repeatable; the first (implicit) config is always the
`baseline`. Useful flags:

- `--repeats N` — runs per config (default 3). Higher N narrows the spread and
  lets smaller effects clear the tolerance band.
- `--metrics` — comma list of watched metrics (default
  `completeness_pct,loss_pct,latency_p95_ms,jitter_p95_ms`). Full set:
  `completeness_pct`, `loss_pct`, `latency_p50_ms`, `latency_p95_ms`,
  `jitter_p50_ms`, `jitter_p95_ms`.
- `--rel-tolerance` / `--abs-tolerance` — the unchanged band half-width around the
  baseline median is `max(abs, rel · |baseline|)`. Defaults: `rel = 0.10`; a
  small absolute floor on the latency/jitter tails (2 ms on p95) so sub-floor
  noise on a tiny baseline never reads as a change. Loss and completeness are
  exact percentages and get no floor.
- `--topic` — restrict the verdict to one topic.
- `--fail-on-regression` — exit non-zero when any candidate regressed (default:
  the verdict lives in `result.json`, exit 0), so a fix playbook can gate on it.

## Interleaving: guarding the verdict against drift

Runs are **interleaved**, not "all of A then all of B": each repeat runs every
config once, and the starting config rotates each repeat (`baseline, cand,
cand, baseline, …`). Interleaving spreads any slow host/thermal drift across
every config instead of loading it onto whichever ran last; the rotation removes
the residual first-slot warm-up bias. The schedule is deterministic, so a verdict
is reproducible from the same runs. The exact execution order is recorded in
`configuration.schedule`.

## The verdict

Per candidate, per topic, per metric, the candidate's **median** across repeats
is compared to the baseline's median (a median so one unlucky repeat cannot flip
a cell). The cell is `IMPROVED`, `WITHIN`, or `REGRESSED` by the metric's
better-direction and the tolerance band. A candidate **passes** iff nothing
regressed beyond tolerance and it did not drop a topic the baseline delivered;
the overall experiment passes iff every candidate passes.

Both spreads (min/median/max) travel with each cell, plus a `separated` flag —
`yes` when the baseline and candidate spreads do **not** overlap, i.e. the effect
is distinguishable at this repeat count.

## Statistical power — read small N honestly

A/B here is a **directional screen**, not a significance test. With a handful of
repeats you can only claim **large** effects: a candidate whose median moves by
less than the run-to-run spread is not really separated from the baseline
(`separated: no`), no matter which side of the tolerance band its median lands
on. Two practical rules:

- **Prefer the boundary regime.** Effects are sharpest where the system is near
  a cliff — a rate-limited profile at the capacity breakpoint, or one of the
  documented boundary pairs (RFC 0007) — so a real knob change produces a big,
  clearly-separated delta instead of a wiggle inside the noise. On a fat,
  unstressed link most knobs look "unchanged" simply because nothing is
  constrained.
- **Raise `--repeats` before you trust a marginal call.** A cell that is
  `IMPROVED`/`REGRESSED` but `separated: no` is a "maybe"; more repeats either
  separate it or reveal it as noise.

Latency/jitter tails are host-timing-dominated and noisier than the
bottleneck-dominated `loss_pct`/`completeness_pct` (which are counted off exact
sequence numbers) — weight the completeness/loss verdict accordingly, exactly as
the regression gate does.

## Outputs (self-describing — the publication byproduct)

Under the run directory:

- `result.json` — `genre: "ab"`: the full per-cell verdict (`result`), the
  per-run measurements (`measurements.runs`), the resolved
  `configuration` (baseline, configs, profile, load, tolerances, schedule), the
  rosotacom `sha` and runner fingerprint, and the overall `verdict`.
- `ab.md` — a markdown table per candidate (topic × metric, both spreads, Δ,
  `sep?`, verdict) — a paper-grade figure with no rework.
- `configs/<label>/…` + `configs/<label>.diff` — each materialized config and its
  YAML diff against the baseline.
- `ab.jsonl` — one row per run.

## Non-goals

- **No automatic parameter search / optimization.** The agent (or human) picks
  the candidate configs; `benchmark ab` only renders the verdict between them.
- **No live-drive A/B.** Replay / emulated-profile conditions only, so the
  comparison is reproducible and the only thing that changed is the config.

## Owner smoke

Two configs that differ only in `throttle_hz`, on the packaged example project,
under a tight uplink where the heavier-throttle candidate offers ~5× the load
into the same pipe and must therefore regress:

```bash
rosotacom examples create /tmp/ab-demo && cd /tmp/ab-demo
printf 'profiles:\n  ab-ci:\n    uplink: { rate: 1mbit }\n    downlink: { rate: 10mbit }\n' > profiles.yaml
for hz in 4 20; do d="cfg_$hz"; mkdir -p "$d"; cat > "$d/session-definition.yaml" <<YAML
peers: { a: {}, b: {} }
peer_settings: { a: { domain_id: 50 }, b: { domain_id: 51 } }
shared: { use_status_overview: true, ota_domain_id: 52, rmw: cyclone }
topics:
  a_to_b:
    - topic: "/bench_capacity"
      type: "com_msgs/msg/SizedPayload"
      processing: { use_ota_wrapper: true, throttle_hz: $hz }
YAML
done
rosotacom benchmark ab --profile ab-ci --baseline cfg_4 --candidate unthrottled=cfg_20 \
  --size 18000 --rate-hz 20 --duration 15 --repeats 1
```

Expected: overall verdict **FAIL** — `unthrottled` regresses on `loss_pct` /
`completeness_pct` (it congests the 1 Mbit/s uplink while the throttled baseline
stays clean); the printed `ab.md` table shows the two REGRESSED cells.

See also: [RFC 0005](rfcs/0005-benchmark-genres-and-ci.md) (benchmark genres),
[RFC 0007](rfcs/0007-regression-gate.md) (the two-sided band / verdict language),
and [docs/performance-bands.md](performance-bands.md) (the committed-band gate
this shares its verdict vocabulary with).
