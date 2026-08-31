# Neither A Queue Nor A Burst Tilts The Size Law

## Claim

The field fit of `1-(1-p)^N` carries a residual that is systematic in size: the
smallest class loses about half of what the fit predicts and the largest about
2.9× more, a spread of roughly six across the ladder. Three candidate causes
were emulated against the same measurement chain, and none of them produces it.

| | 300 B | 1.1 kB | 3.1 kB | 6.1 kB | 12.1 kB | 38.1 kB |
|---|---:|---:|---:|---:|---:|---:|
| independent per packet | 1.05 % | 1.47 % | 3.47 % | 6.28 % | 11.23 % | 31.92 % |
| + a token bucket a burst can fill (4 Mbit/s) | 1.14 % | 1.33 % | 2.57 % | 4.99 % | 13.43 % | 32.92 % |
| a serialisation constraint (1.5 Mbit/s) | — | 1.62 % | — | — | 11.60 % | **97.54 %** |

- **The chain is not the cause.** Under independent per-packet loss the fitted
  law is reproduced with residuals of 1.20, 0.85, 1.01, 1.06, 0.97 and 1.00 —
  flat over three decades of size. Whatever tilts the field, it is not the
  wrapper, the join, or the way the fit is taken.
- **A burst meeting a queue is not the cause.** A token bucket at 4 Mbit/s —
  above the largest class's mean offered rate of 3.0 Mbit/s and far below its
  instantaneous one, so a 38-packet message meets a queue and a 1-packet
  message does not — changes nothing systematic: 1.09, 0.90, 0.74, 0.80, 1.20,
  1.03 against the unqueued arm. The queue defers rather than drops, which is
  the same statement as
  [oversubscription queues, it does not lose](oversubscription-queues-not-losses.md),
  seen per size.
- **A serialisation constraint does not tilt it either — it removes the class.**
  At 1.5 Mbit/s the one-packet message pays 10 % more than unshaped and the
  13-packet message 3 % more, while the 38-packet message goes from 31.9 % to
  **97.5 %**. That is not a slope, it is a threshold: below the constraint
  nothing changes, above it the stream is gone.

The last row is worth naming for what it is *shaped* like. A link whose
transmission opportunities are a fraction of the wall clock presents from above
as exactly this — a small probe crossing unharmed while a large burst does not
— which is the signature an uplink moved onto a duty-cycled carrier would leave
in an application's own numbers. This reproduces the signature; it does not
demonstrate the mechanism, because a sustained rate cap and a duty cycle are
not the same thing and this cell is oversubscribed on the mean as well as on
the burst.

## Setup

- Host pair / topology: one host, the packaged local benchmark rig — two
  communication containers on their own Docker network, `tc` inside each
  container's own netns (`--sudo-mode container`).
- Session: `bench_1_1_capacity` from the packaged example project, one
  `a->b:/bench_capacity` stream at 10 Hz, CycloneDDS, OTA QoS best_effort /
  KEEP_LAST depth 1.
- rosotacom SHA: measured at `7bb420c`; needs 2.5.dev74 or later for the fitted
  delay table to arm at all.
- Profiles: `tilt-iid`, `tilt-iid-queued` and `tilt-iid-tight` in
  [`src/rosotacom/resources/examples/profiles.yaml`](../../src/rosotacom/resources/examples/profiles.yaml).
  All three carry the 2026-08-17 drive's own delay table at 25 ms ± 18 ms and
  1 % independent per-packet loss; they differ only in the rate cap in front of
  it — none, 4 Mbit/s, 1.5 Mbit/s.
- Seed policy: netem `loss` is unseeded, `n=1` per cell, about 2100 messages
  inside the measured window of each.

## Evidence

Evidence grade: per-message counts from the receiving peer's own RFC 0003
transit records, windowed in sequence numbers (first 300 and last 50 dropped)
so that the unshaped messages before the shaper is armed leave both the
delivered and the lost class at once.

The independent arm, with one `(p, F)` fitted by maximum likelihood over the
whole ladder (`p = 0.870 %` per packet, `F = 872 B`):

| wrapped | N | offered | lost | observed | 95 % CI | obs/pred |
|---:|---:|---:|---:|---:|---:|---:|
| 300 B | 1 | 2101 | 22 | 1.047 % | 0.69–1.58 | 1.20 |
| 1.1 kB | 2 | 2102 | 31 | 1.475 % | 1.04–2.09 | 0.85 |
| 3.1 kB | 4 | 2105 | 73 | 3.468 % | 2.77–4.34 | 1.01 |
| 6.1 kB | 7 | 2103 | 132 | 6.277 % | 5.32–7.40 | 1.06 |
| 12.1 kB | 14 | 2102 | 236 | 11.227 % | 9.95–12.65 | 0.97 |
| 38.1 kB | 44 | 2102 | 671 | 31.922 % | 29.96–33.95 | 1.00 |

The queued arm on the same ladder: 24, 28, 54, 105, 283 and 693 losses of about
2100 offered, giving the second row of the claim table. The tight arm: 34 of
2104, 244 of 2103, and 1826 of 1872.

**A two-state loss process could not be measured on this ladder, and the reason
is worth recording.** `netem`'s Gilbert–Elliott model advances its chain once
per *packet*, not once per second, so a size ladder at a fixed message rate runs
the chain at a different speed in every cell — 10 packets/s for a one-fragment
message against 380 for a 38-fragment one. The cells then do not share a loss
process and cannot be compared; the arm measured at a fixed 10 Hz duly reported
a per-packet mean of 0.33 % in its smallest cell and 0.69 % fitted over the
ladder, against a stationary 1.00 %, because the small cells never sampled
enough of the chain. Holding the offered *packet* rate constant is what makes
such a ladder one experiment, and the profiles file says so beside the fitted
parameters.

Verification: manual, from a source checkout with Docker:

```bash
for prof in tilt-iid tilt-iid-queued; do
  for size in 200 1000 3000 6000 12000 38000; do
    rosotacom benchmark probe \
      --project src/rosotacom/resources/examples/rosotacom.yaml \
      --profile "$prof" --size "$size" --rate-hz 10 --duration 240 \
      --repeats 1 --sudo-mode container --no-plot
  done
done
rosotacom benchmark probe --project src/rosotacom/resources/examples/rosotacom.yaml \
  --profile tilt-iid-tight --size 38000 --rate-hz 10 --duration 240 \
  --repeats 1 --sudo-mode container --no-plot
```

The independent ladder must fit one `(p, F)` to within about ten per cent at
every point; the queued ladder must not differ from it systematically; and the
tight cell at 38 kB must be near-total loss while its 1 kB cell is not.

## Status

confirmed, 2026-08-31.

## Publication notes

This is a set of negative results and the negative is the point: a residual
that survives three plausible explanations is worth more than one that has not
been attacked. What it licenses is a narrower claim than "we do not know why" —
the tilt is not an artefact of the accounting, it is not what a queue does to a
burst, and it is not a serialisation limit, because a serialisation limit does
not tilt a curve, it truncates it.

What is left to attack is correlation *inside* one message's own burst on a
real radio — loss events shorter than the time a large message spends on the
wire, which `netem`'s per-packet chain cannot express at all. The
Gilbert–Elliott note above is the reason: the tool models a process in packets,
and the candidate mechanism lives in milliseconds.

The serialisation row deserves one sentence wherever a duty-cycled uplink is
discussed. It shows that the observable signature of such a link — small
messages unaffected, large messages gone, round-trip time unchanged — is
reproducible from a rate constraint alone, which means the signature on its own
does not identify the mechanism.
