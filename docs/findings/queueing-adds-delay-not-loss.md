# A Filling Queue Adds Delay And No Loss Of Its Own

## Claim

Under oversubscription a queue converts the excess into delay and contributes
**nothing** to the loss rate until it overflows. Capping an otherwise clean link
15 % below the offer ramps one-way delay from 0.5 s to 5.6 s across eighteen
consecutive one-second bins with **0 of 558** offered messages lost, and drops
97 % of a bin only once the buffer is full. Repeating the same cap on a link
that also drops 0.6 % of packets independently reproduces the identical ramp and
loses 15 of 558 during it — 2.7 %, which is the unshaped link's own 2.51 % and
not a milligram more.

So "the queue lost it" is never the explanation. Whatever a filling queue loses,
the link was losing anyway; what the queue adds is age. The corollary is the
useful one: **loss and delay have to be read as two measurements, because the
queue moves only one of them**, and a loss-based health verdict is blind to the
whole of the second signal.

## Setup

- Host pair / topology, arm 1: `tks-lamborghini`, rosotacom local benchmark mode
  using the packaged example project; two peer containers on a private bridge,
  uplink shaping via `tc` inside the peers' own netns (no host privileges).
- Host pair / topology, arm 2: `tks-seat` -> `tks-majestic` over the TKS tunnel,
  orchestrated from `tks-lamborghini` through `remote-ota` and
  `--install-mode checkout`; project `session/bench/rosotacom.yaml` in
  remote-assist, shaping through `--sudo-mode container`. Runbook and the
  interface constraint that decides which alias may carry the data plane:
  `remote-assist/session/bench/README.md`.
- rosotacom SHA: `c5ce52f` (harness), submodule `c2d2467`; arm 2 on the
  published `rosotacom-dev 2.5.dev78` on both peers.
- Profile: four conditions in one file, offer held at 1.035 Mbit/s throughout so
  only the shaping differs.

  | name | uplink |
  |---|---|
  | `q-clean` | `delay: 25ms` |
  | `q-loss` | `delay: 25ms, loss: 0.6%` |
  | `q-squeeze` | 10 s clean, 25 s at `rate: 900kbit`, 10 s clean |
  | `q-squeeze-loss` | the same three steps, `loss: 0.6%` throughout |

- Load: `--size-pattern 6x200B+1x28KB --rate-hz 31`, so one stream carries a
  1-fragment and a 24-fragment class together and the size signature of the loss
  is readable inside a single run.
- Seed policy: `loss` is netem's own stochastic element; `n=1` per condition,
  reported as the four-cell comparison rather than as a distribution.

## Evidence

Evidence grade: packaged-profile benchmark runs plus their per-bin timelines.

```bash
rosotacom benchmark probe --project src/rosotacom/resources/examples/rosotacom.yaml \
  --profiles-file <profiles.yaml> --profile q-squeeze \
  --size-pattern 6x200B+1x28KB --rate-hz 31 --duration 45 --repeats 1 \
  --bin-s 1 --no-plot
```

| condition | ramp | delay across it | offered | lost in ramp | whole run |
|---|---:|---|---:|---:|---:|
| `q-clean` | none | 29.9 ms flat | — | — | 0 / 1398 = 0.00 % |
| `q-loss` | none | 29.2 ms flat | — | — | 35 / 1397 = 2.51 % |
| `q-squeeze` | 18 bins | 532 → 5623 ms | 558 | **0** | 84 / 1317 = 6.38 % |
| `q-squeeze-loss` | 18 bins | 538 → 5490 ms | 558 | **15 = 2.7 %** | 119 / 1314 = 9.06 % |

The ramp is cut by the delay's own slope and never by its loss — a bin belongs
to it when the median delay gained at least 250 ms over the bin before it. The
shaped bins gain about 295 ms each; the first bin to lose gains 221 and falls
outside by that rule rather than by its outcome. Without that discipline the
measurement defines its own answer.

Three readings:

1. **Queueing is not a loss process.** Eighteen bins, 558 messages, zero. At the
   0.6 % per-packet rate the same load carries in `q-loss`, 558 messages of mean
   4.2 fragments would carry ~14 losses; the shaped-but-clean arm has none,
   because there is nothing to lose them to.
2. **Queueing does not amplify the link's loss either.** `q-squeeze-loss` loses
   2.7 % during a ramp whose delay reaches 5.5 s, against 2.51 % on the same link
   with no queue at all. The queue defers packets; it does not damage them.
3. **What ends it is the buffer, and it ends abruptly.** 97 % of a single bin,
   not a gradual rise. The lossless window is therefore bounded by buffer depth
   over excess rate and can be computed in advance, which is the only part of
   this a deployment can design against.

Verification: manual, needs Docker and about three minutes per condition. Run
the command above for each profile and read `time-bins.jsonl` beside
`result.json`: the `q-squeeze` bins under the cap must show monotonically
climbing `latency_p50_ms` with `lost = 0`, and `q-squeeze-loss` must show the
same climb with `lost` at the unshaped arm's rate.

The two-host arm is the same reading, on machines instead of containers, and
takes about four minutes per condition:

```bash
cd ~/dev/remote-assist
rosotacom ota-benchmark probe --project session/bench/rosotacom.yaml \
  --target bench_1_1_capacity --target-type session \
  --peer a=seat_tks --peer b=majestic_tks \
  --peer-exec 'a=remote-ota seat run remote-assist' \
  --peer-exec 'b=remote-ota majestic run remote-assist' \
  --install-mode checkout \
  --peer-checkout a=/home/go914_local/dev/remote-assist \
  --peer-checkout b=/home/go914/dev/remote-assist \
  --sudo-mode container --profile q-squeeze \
  --size 4173 --rate-hz 31 --duration 45 --bin-s 1 --no-attach --no-plot
```

Both peers must be current (`remote-sync <host> check remote-assist`): checkout
mode installs nothing, so a stale peer measures the old code and nothing warns.

## Status

confirmed, 2026-09-01, on one host and independently on a two-machine pair.

## Publication notes

This is the control the field could not supply. On a real cellular uplink the
two signals never separate — in the KAMO August campaign, thirty-six
pre-registered filling episodes over 168 s (delay rising for three or more
consecutive seconds, gaining at least 150 ms) lose 2.34 % against the 2.27 %
their own seconds' rate predicts, P(at most this many) = 0.71 — because whatever reduces
the grant also raises the error rate. Reading that field result alone, one would
conclude a filling queue loses. This finding shows it does not: the loss belongs
to the link and arrives whether or not the queue is filling.

**It reproduces on two physical machines** (2026-09-01, added after the
single-host arm above). seat publishing to majestic over the TKS tunnel,
orchestrated from lamborghini, which is on neither end of the data plane. Same
four profiles, same 1.035 Mbit/s offer, a constant 4173 B at 31 Hz instead of the
size pattern:

| condition | one-way delay p50 | filling | lost while filling | whole run |
|---|---:|---:|---:|---:|
| `q-clean` | 32.3 ms flat | — | — | 0 / 1432 |
| `q-loss` | 32.5 ms flat | — | — | 43 / 1357 = 3.17 % |
| `q-squeeze` | 111 -> 6250 ms | 21 bins | **0 / 651** | 195 / 1610 |
| `q-squeeze-loss` | 94 -> 6021 ms | 21 bins | **22 / 650 = 3.38 %** | 225 / 1574 |

Twenty-one consecutive one-second bins of monotone delay growth over six
seconds, and not one message lost, where the same link without a queue loses
3.17 % per message: 20.6 expected, P(0) = 1.1e-9, and the filling phase's own
rate is below 0.46 % at 95 %. The lossy arm loses 3.38 % while filling against
that link's queue-free 3.17 % — +0.22 pp, 95 % CI [-1.4, +1.9]. Then both knee
into the buffer's limit within one bin, as on one host.

The filling phase is cut here as every bin from the first above the floor up to
the last before the delay peaks. That is still delay-only, and it is the cut to
state on this pair: the 250 ms slope rule swallows `q-squeeze`'s knee bin, whose
gain is 423 ms, and reports 14 of 682 for a phase whose first twenty-one bins
lose nothing. Both readings are above; the slope rule is the one that was
written down first and it is reported first for that reason.

Two machines is the stronger setting for network realism -- a real NIC, a real
tunnel, two kernels, two DDS participants that have to discover each other over
it -- and the weaker one for attribution, because the tunnel is a second queue
the shaper does not own. One host is the reverse. Both give the same answer,
which is the useful part.

The two-host arm uses a constant `--size` rather than the mixed
`--size-pattern` of the single-host arm because the pattern did not survive the
OTA hop: `parse_size_pattern_load` produces one `size_<label>` per distinct
size and only `size_a` crossed, so the peer's publisher died on `Pattern
references size 'b' but it was not provided` inside a detached `docker exec`
whose output was discarded, and the orchestrator could only report that the
topic never advertised. Fixed in #344. Until a release carries it, a mixed load
is single-host only.

It supersedes the reading in
[oversubscription-queues-not-losses.md](oversubscription-queues-not-losses.md),
which reported the same lossless ramp but attributed the boundary losses to the
timeline step destroying the queue. Here the buffer overflows *inside* an
unchanged shaping step, so the overflow is the queue's own limit rather than an
artefact of `tc qdisc replace`.

For papers: the pair `q-squeeze` / `q-squeeze-loss` is the figure. One ramp, one
loss axis, two arms — it shows in a single panel that the delay is the queue's
and the loss is the link's.
