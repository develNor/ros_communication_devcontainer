"""Ground truth from a recorded rosbag2, for replay (rosbag-driven) OTA tests.

A live test only knows what it observes; a *replay* test additionally knows its
source exactly -- the bag records, per topic, how many messages exist, over what
duration, at what QoS. That known-future lets a replay test make assertions a
live test cannot (see docs/rfcs/0002 "Live vs replay"): here we read the bag's
own ``metadata.yaml`` (pure YAML -- no rosbag2/ROS, no mcap decode) to extract
per-topic ground truth, and validate a session's declared ``expect`` against it.

The most common authoring bug this catches: an ``hz.min`` floor above the topic's
native publish rate -- the OTA link can only ever thin a stream, never speed it
up, so such a floor is unsatisfiable no matter how healthy the link.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _native_hz(count: int, duration_s: float) -> float | None:
    if duration_s <= 0 or count <= 0:
        return None
    # N messages span N-1 intervals over the duration; for large N this ~= count/duration.
    return (count - 1) / duration_s if count > 1 else None


def _first_qos(offered: Any) -> dict[str, Any]:
    """rosbag2 stores offered_qos_profiles as a YAML string or an already-parsed
    list of per-publisher profiles. Return the first profile as a dict."""
    if isinstance(offered, str):
        try:
            offered = yaml.safe_load(offered)
        except yaml.YAMLError:
            return {}
    if isinstance(offered, list) and offered and isinstance(offered[0], dict):
        return offered[0]
    if isinstance(offered, dict):
        return offered
    return {}


def bag_ground_truth(metadata_path: str | Path) -> dict[str, dict[str, Any]]:
    """Parse a rosbag2 ``metadata.yaml`` into ``{topic: ground-truth}``.

    Accepts either the metadata.yaml path or its containing bag directory. Each
    value has: ``count``, ``duration_s``, ``native_hz`` (None if not derivable),
    ``msg_type``, ``durability``, ``reliability``.
    """
    path = Path(metadata_path)
    if path.is_dir():
        path = path / "metadata.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    info = doc.get("rosbag2_bagfile_information") or {}
    duration_s = float((info.get("duration") or {}).get("nanoseconds", 0)) / 1e9

    out: dict[str, dict[str, Any]] = {}
    for entry in info.get("topics_with_message_count") or []:
        meta = entry.get("topic_metadata") or {}
        name = meta.get("name")
        if not name:
            continue
        count = int(entry.get("message_count") or 0)
        qos = _first_qos(meta.get("offered_qos_profiles"))
        out[str(name)] = {
            "count": count,
            "duration_s": round(duration_s, 3),
            "native_hz": _native_hz(count, duration_s),
            "msg_type": meta.get("type"),
            "durability": qos.get("durability"),
            "reliability": qos.get("reliability"),
        }
    return out


def validate_expect_against_bag(
    expect_by_topic: dict[str, dict[str, Any]], ground_truth: dict[str, dict[str, Any]]
) -> list[str]:
    """Warnings where a session's per-topic ``expect`` contradicts the bag.

    Pure and order-stable. Only emits a warning when the contradiction is certain
    from the ground truth alone (the OTA link can thin but never amplify a rate)."""
    warnings: list[str] = []
    for topic, expect in expect_by_topic.items():
        gt = ground_truth.get(topic)
        if not gt or not isinstance(expect, dict):
            continue
        native_hz = gt.get("native_hz")
        hz = expect.get("hz") or {}
        hz_min = hz.get("min") if isinstance(hz, dict) else None
        if hz_min is not None and native_hz is not None and float(hz_min) > native_hz:
            warnings.append(
                f"{topic}: expect.hz.min {hz_min} exceeds the bag's native rate "
                f"{native_hz:.1f} Hz ({gt['count']} msgs / {gt['duration_s']}s) -- the OTA "
                f"link can only thin a stream, so this floor is unsatisfiable."
            )
        # A bag topic published essentially once and held (transient_local) is a
        # static/latched topic; a stream contract (hz floor) misdescribes it.
        if (
            gt.get("durability") == "transient_local"
            and (gt.get("count") or 0) <= 3
            and hz_min is not None
            and str(expect.get("mode", "stream")).strip().lower() == "stream"
        ):
            warnings.append(
                f"{topic}: bag publishes it {gt['count']}x as transient_local (static/held), "
                f"but expect is a stream with an hz floor -- consider `mode: latched`."
            )
    return warnings
