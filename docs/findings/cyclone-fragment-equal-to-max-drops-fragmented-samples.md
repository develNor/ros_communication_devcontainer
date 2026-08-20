# A Fragment Sized Exactly Like the Datagram Cap Stops Every Fragmented Sample

## Claim

Setting CycloneDDS's `FragmentSize` **equal to** `MaxMessageSize` makes every
sample large enough to fragment **never arrive** on CycloneDDS 11.0.1, while the
identical profile works on 0.10.5. Samples small enough to fit in one fragment
keep flowing on the same link, which is what makes it dangerous — the endpoints
match, no error appears on either side, and the reader simply never sees a
sample.

An RTPS message carries headers as well as the fragment, so a fragment of
exactly `MaxMessageSize` cannot be put into a message of `MaxMessageSize`.
0.10.5 evidently coped with the arithmetic; 11.0.1 does not, and drops silently.

This is the CycloneDDS sibling of
[the Fast DDS datagram cap](fastdds-datagram-cap-drops-large-samples.md), and it
arrived from the opposite direction: there the cap was a value copied *from* this
profile, here the profile's own two values were equal to each other.

## Setup

- Host pair / topology: two hosts joined by a point-to-point VPN (`/24`,
  1.3 ms RTT, 0.0% loss), one container per host from the packaged
  communication image, `--network host --ipc host`, one ROS domain.
- Profile: the shipped OTA profile — `AllowMulticast false`, the peer's unicast
  address, a pinned interface, `SPDPInterval 30s`,
  `EnableTopicDiscoveryEndpoints false` — with only the three size elements
  varied.
- Traffic: `ros2 topic pub -r 1` of a 64 kB `std_msgs/msg/String`, and a
  subscriber given the type explicitly (with topic-discovery endpoints off, a
  bare `ros2 topic echo <topic>` resolves no type and reports nothing, in both
  versions).
- Stacks: `ros-kilted-cyclonedds` 0.10.5 against `ros-lyrical-cyclonedds`
  11.0.1.

## Evidence

| profile | 0.10.5 | 11.0.1 |
|---|---|---|
| `FragmentSize` 1200 B, `MaxMessageSize` 1200 B | arrives | **nothing** |
| no size limits at all | — | arrives |
| `FragmentSize` 1024 B, `MaxMessageSize` 1200 B | — | arrives |

A 40 B string crosses on 11.0.1 with the 1200/1200 profile, so the trigger is
fragmentation and not the link.

The discovery trace of a failing run rules out everything above the data path:
both participants discover each other across the hosts, both sides announce
their endpoints, the reading side builds proxy writers for the remote
participants, and no QoS-incompatibility or rejection line appears on either
side.

## Status

Fixed in `cyclonedds_tuned.xml.template` by making `FragmentSize` strictly
smaller than `MaxMessageSize` (1024 B against 1200 B). The datagram stays inside
the same MTU budget the 1200 B was chosen for — only the header room is added —
and the value works on both versions, so it is not a Lyrical-only branch.

The invariant is held by `test_only_cyclone_caps_the_datagram_and_fast_dds_must_not`,
which now asserts the inequality rather than a pair of equal numbers.
