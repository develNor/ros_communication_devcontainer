# Identifying keyframes in ffmpeg camera streams

An ffmpeg-transported camera topic
(`ffmpeg_image_transport_msgs/msg/FFMPEGPacket`) is not a uniform stream: the
encoder emits one **keyframe** (I-frame, self-contained) followed by
`gop_size - 1` small **delta frames** (P-frames), then repeats. The two
classes differ in size by one to two orders of magnitude — measured on the
remote-assist processed handoff trace (gop_size 5, 10 Mbit/s cap, 10 Hz):
keyframes 21–89 KiB (mean ~43 KiB), delta frames 0.3–15 KiB (mean ~4.5 KiB),
with per-GOP contrast up to ~180×. For network analysis this periodic burst
pattern *is* the stream: a link must absorb the keyframe spikes, not the
average bitrate. Frame classification is therefore the first step of any
GOP-aware bandwidth, loss, or latency analysis.

## Primary algorithm: the `flags` field

`FFMPEGPacket.flags` mirrors libav's packet flags; **bit 0 is
`AV_PKT_FLAG_KEY` and is set exactly on keyframes**:

```python
is_keyframe = bool(msg.flags & 0x1)   # AV_PKT_FLAG_KEY
```

This works on live subscriptions, recorded handoff traces, and anonymized
replay bags alike: `rosotacom anonymize` deliberately preserves `flags` and
`pts` when it zeroes FFMPEGPacket payloads (see
`src/rosotacom/resources/ws/session/creation/anonymize_bag.py`), because the
keyframe/delta structure is stream shape — like the sizes and timing the
anonymizer already keeps — not scene content.

Offline tooling can classify frames straight from serialized CDR bytes (as
stored in mcap files) without a ROS runtime via
[`rosotacom.ffmpeg_packet.parse_ffmpeg_packet`](../src/rosotacom/ffmpeg_packet.py):

```python
from rosotacom.ffmpeg_packet import parse_ffmpeg_packet

info = parse_ffmpeg_packet(serialized_message_bytes)
info.is_keyframe   # flags bit 0
info.pts           # encode order, also preserved by anonymization
info.payload_size  # encoded frame bytes
```

Grouping a stream into GOPs is then just splitting at keyframes. Note the
encoder may emit keyframes *early* (scene cuts), so GOPs can be shorter than
`gop_size` — assert spacing `<= gop_size`, not `== gop_size`.

## Fallback: size bimodality

If `flags` is unavailable (foreign recordings, or bags anonymized before
flag preservation existed), the size distribution still separates the two
classes: delta frames dominate the stream (a GOP of N contributes N−1), so
the median frame size sits on the delta mode, while keyframes are typically
5–100× larger. `rosotacom.ffmpeg_packet.keyframes_by_size` marks frames
above `3 × median` as keyframes. This misclassifies only when delta frames
get unusually large (heavy motion) or keyframes unusually small (static
scene at a very low bitrate) — prefer `flags` whenever present.

## Validation

- `tests/unit/test_ffmpeg_packet.py` — CDR parsing (alignment across
  odd-length strings), flag semantics, and the size fallback on a synthetic
  GOP pattern.
- `tests/unit/test_anonymize.py::test_anonymize_msg_preserves_ffmpeg_keyframe_structure`
  — anonymization keeps `flags`/`pts` while zeroing payloads.
- `tests/e2e/test_anonymize_e2e.py` — headless end-to-end: a trace bag written
  in the container, anonymized via the CLI, and re-read with keyframe flags
  intact.
