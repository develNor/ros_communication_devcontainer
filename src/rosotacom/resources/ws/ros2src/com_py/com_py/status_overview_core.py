#!/usr/bin/env python3

# -- BEGIN LICENSE BLOCK ----------------------------------------------
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#
#    * Neither the name of the {copyright_holder} nor the names of its
#      contributors may be used to endorse or promote products derived from
#      this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
# -- END LICENSE BLOCK ------------------------------------------------
#
# ---------------------------------------------------------------------
# !\file
#
# \brief  Pure (ROS-independent) logic for the rosotacom status overview.
#
# This module holds the per-stage observation accumulator, the stage/topic
# classification, the rollup ("where is the topic now?") and the artifact
# rendering. It deliberately has no rclpy dependency so it can be unit-tested
# on the host. status_overview.py wires it to live ROS observers.
# ---------------------------------------------------------------------

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple


# --- State vocabulary (aligned with heartbeat_in_monitor) ---
ABSENT = "ABSENT"        # no publisher and never received a message
IDLE = "IDLE"            # publisher present but no message observed yet
FLOWING = "FLOWING"      # messages within the liveness window
STALE = "STALE"          # was flowing, last message older than stale threshold

# Quality sub-classification while FLOWING
GOOD = "GOOD"
DEGRADED = "DEGRADED"
BAD = "BAD"

# Topic-level rollup
OK = "OK"
PARTIAL = "PARTIAL"
STALLED = "STALLED"


@dataclass
class StageObservation:
    """Live observation of a single stage topic within one ROS domain."""

    type_str: Optional[str] = None
    subscribed: bool = False
    pub_count: int = 0
    sub_count: int = 0
    msg_total: int = 0
    last_recv_mono: Optional[float] = None
    last_recv_wall: Optional[float] = None
    last_delay_s: Optional[float] = None
    # (t_mono, size_bytes, delay_s_or_None)
    events: Deque[Tuple[float, int, Optional[float]]] = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, size: int, delay_s: Optional[float], now_mono: Optional[float] = None,
               now_wall: Optional[float] = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        with self.lock:
            self.msg_total += 1
            self.last_recv_mono = now
            self.last_recv_wall = time.time() if now_wall is None else now_wall
            self.last_delay_s = delay_s
            self.events.append((now, size, delay_s))

    def metrics(self, now_mono: float, window_s: float) -> Dict[str, Any]:
        with self.lock:
            cutoff = now_mono - window_s
            while self.events and self.events[0][0] < cutoff:
                self.events.popleft()
            count = len(self.events)
            total_bytes = sum(e[1] for e in self.events)
            delays = [e[2] for e in self.events if e[2] is not None]
            last_recv_mono = self.last_recv_mono
            last_recv_wall = self.last_recv_wall
            last_delay = self.last_delay_s
            msg_total = self.msg_total
        hz = count / window_s if window_s > 0 else 0.0
        mean_size = (total_bytes / count) if count > 0 else 0.0
        age = (now_mono - last_recv_mono) if last_recv_mono is not None else None
        return {
            "hz": hz,
            "mean_size_bytes": mean_size,
            "last_delay_s": last_delay,
            "delays_in_window": delays,
            "age_s": age,
            "last_recv_wall": last_recv_wall,
            "msg_total": msg_total,
        }


class StatusAggregator:
    """
    Merges observations from the per-domain observers with the static pipeline
    spec into the unified status artifacts.

    ``observers_by_domain`` maps a domain key ("local"/"ota") to any object that
    exposes an ``observations`` dict mapping topic name -> StageObservation.
    """

    def __init__(self, logger, spec: Dict[str, Any], output_dir: str,
                 observers_by_domain: Dict[str, Any],
                 *, liveness_window_s: float = 3.0, stale_after_s: float = 3.0,
                 delay_good_ms: float = 100.0, delay_bad_ms: float = 200.0):
        self._log = logger
        self._spec = spec
        self._output_dir = output_dir
        self._observers = observers_by_domain
        self._liveness_window_s = liveness_window_s
        self._stale_after_s = stale_after_s
        self._delay_good_ms = delay_good_ms
        self._delay_bad_ms = delay_bad_ms
        self._prev_states: Dict[str, Dict[str, Any]] = {}
        os.makedirs(self._output_dir, exist_ok=True)
        self._json_path = os.path.join(self._output_dir, "status.json")
        self._txt_path = os.path.join(self._output_dir, "status.txt")
        self._events_path = os.path.join(self._output_dir, "events.jsonl")

    # -- observation lookup --
    def _obs_for(self, stage: Dict[str, Any]) -> Optional[StageObservation]:
        domain = stage.get("domain", "local")
        observer = self._observers.get(domain) or self._observers.get("local")
        if observer is None:
            return None
        return observer.observations.get(stage["topic"])

    # -- classification --
    def classify_stage(self, stage: Dict[str, Any], expected_hz: Optional[float],
                       now_mono: float) -> Dict[str, Any]:
        obs = self._obs_for(stage)
        result: Dict[str, Any] = {
            "stage": stage["stage"],
            "topic": stage["topic"],
            "domain": stage.get("domain", "local"),
            "produced_by": stage.get("produced_by"),
            "publishers": 0,
            "subscribers": 0,
            "hz": 0.0,
            "mean_size_bytes": 0.0,
            "latency_ms": None,
            "age_s": None,
            "last_message_wall": None,
            "messages_total": 0,
            "state": ABSENT,
            "quality": None,
            "quality_reason": None,
        }
        if obs is None:
            return result

        m = obs.metrics(now_mono, self._liveness_window_s)
        result["publishers"] = obs.pub_count
        result["subscribers"] = obs.sub_count
        result["hz"] = round(m["hz"], 3)
        result["mean_size_bytes"] = round(m["mean_size_bytes"], 1)
        result["messages_total"] = m["msg_total"]
        result["age_s"] = round(m["age_s"], 3) if m["age_s"] is not None else None
        if m["last_delay_s"] is not None:
            result["latency_ms"] = round(m["last_delay_s"] * 1000.0, 1)
        if m["last_recv_wall"] is not None:
            result["last_message_wall"] = datetime.datetime.fromtimestamp(
                m["last_recv_wall"]
            ).isoformat(timespec="milliseconds")

        if m["msg_total"] == 0:
            result["state"] = IDLE if obs.pub_count > 0 else ABSENT
            return result

        age = m["age_s"]
        if age is not None and age > self._stale_after_s:
            result["state"] = STALE
            return result

        result["state"] = FLOWING
        quality, reason = self._classify_quality(m, expected_hz)
        result["quality"] = quality
        result["quality_reason"] = reason
        return result

    def _classify_quality(self, m: Dict[str, Any], expected_hz: Optional[float]) -> Tuple[str, Optional[str]]:
        delay_ms = (m["last_delay_s"] * 1000.0) if m["last_delay_s"] is not None else None
        if delay_ms is not None:
            if delay_ms >= self._delay_bad_ms:
                return BAD, "latency"
            if delay_ms > self._delay_good_ms:
                return DEGRADED, "latency"
        if expected_hz and expected_hz > 0:
            hz = m["hz"]
            if hz < 0.5 * expected_hz:
                return BAD, "hz"
            if hz < 0.8 * expected_hz:
                return DEGRADED, "hz"
        return GOOD, None

    # -- rollup --
    def rollup(self, topic_spec: Dict[str, Any], stage_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        states = [s["state"] for s in stage_results]
        n = len(stage_results)
        seen_idx = [i for i, st in enumerate(states) if st in (FLOWING, STALE)]
        last_seen = max(seen_idx) if seen_idx else -1
        any_publisher = any(s["publishers"] > 0 for s in stage_results)

        reached_stage = stage_results[last_seen]["stage"] if last_seen >= 0 else None
        blocked_at = None
        next_topic = None
        diagnosis = ""

        final_idx = n - 1
        if n == 0:
            overall = ABSENT
        elif last_seen == final_idx and states[final_idx] == FLOWING:
            overall = OK
            diagnosis = f"all observable stages flowing (reached '{reached_stage}')"
        elif last_seen == -1:
            blocked = stage_results[0]
            blocked_at = blocked["stage"]
            next_topic = blocked["topic"]
            overall = ABSENT if not any_publisher else STALLED
            diagnosis = self._stage_diagnosis(blocked)
        else:
            reached = stage_results[last_seen]
            if reached["state"] == STALE:
                overall = STALLED
                age = reached.get("age_s")
                diagnosis = (
                    f"'{reached_stage}' ({reached['topic']}) stopped"
                    + (f" {age:.1f}s ago" if isinstance(age, (int, float)) else "")
                )
                blocked_at = reached["stage"]
                next_topic = reached["topic"]
            else:
                overall = PARTIAL
                blocked = stage_results[last_seen + 1]
                blocked_at = blocked["stage"]
                next_topic = blocked["topic"]
                diagnosis = (
                    f"reached '{reached_stage}', but '{blocked_at}' is not getting it: "
                    + self._stage_diagnosis(blocked)
                )

        if topic_spec.get("direction") == "outbound" and overall == OK:
            diagnosis += " | sent to transport; remote delivery not observed locally (Phase 1)"

        return {
            "overall": overall,
            "reached_stage": reached_stage,
            "blocked_at": blocked_at,
            "next_missing_topic": next_topic,
            "diagnosis": diagnosis,
        }

    def _stage_diagnosis(self, stage_result: Dict[str, Any]) -> str:
        st = stage_result["state"]
        topic = stage_result["topic"]
        producer = stage_result.get("produced_by") or "producer"
        if st == ABSENT:
            return f"{producer} is not producing {topic} (no publisher)"
        if st == IDLE:
            return f"{topic} has a publisher but no messages observed yet"
        if st == STALE:
            return f"{topic} stopped receiving messages"
        return f"{topic}: {st}"

    # -- snapshot + write --
    def build_snapshot(self, now_mono: Optional[float] = None) -> Dict[str, Any]:
        if now_mono is None:
            now_mono = time.monotonic()
        now_wall = datetime.datetime.now().isoformat(timespec="milliseconds")
        topics_out: List[Dict[str, Any]] = []
        counts = {OK: 0, PARTIAL: 0, STALLED: 0, ABSENT: 0}

        for topic_spec in self._spec.get("topics", []):
            expected_hz = topic_spec.get("expected_hz")
            stage_results = [
                self.classify_stage(stage, expected_hz, now_mono)
                for stage in topic_spec.get("stages", [])
            ]
            roll = self.rollup(topic_spec, stage_results)
            counts[roll["overall"]] = counts.get(roll["overall"], 0) + 1
            topics_out.append(
                {
                    "base": topic_spec.get("base"),
                    "direction": topic_spec.get("direction"),
                    "source": topic_spec.get("source"),
                    "target": topic_spec.get("target"),
                    "type": topic_spec.get("type"),
                    "expected_hz": expected_hz,
                    "overall": roll["overall"],
                    "reached_stage": roll["reached_stage"],
                    "blocked_at": roll["blocked_at"],
                    "next_missing_topic": roll["next_missing_topic"],
                    "diagnosis": roll["diagnosis"],
                    "remote_observation": None,  # Phase 1: not observed
                    "stages": stage_results,
                }
            )

        return {
            "schema_version": 1,
            "phase": 1,
            "generated_at": now_wall,
            "peer": self._spec.get("peer"),
            "remote": self._spec.get("remote"),
            "local_domain_id": self._spec.get("local_domain_id"),
            "ota_domain_id": self._spec.get("ota_domain_id"),
            "uses_domain_bridge": self._spec.get("uses_domain_bridge"),
            "summary": counts,
            "topics": topics_out,
        }

    def detect_transitions(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for t in snapshot["topics"]:
            key = f"{t['direction']}:{t['base']}"
            cur = {
                "overall": t["overall"],
                "reached_stage": t["reached_stage"],
                "blocked_at": t["blocked_at"],
                "stages": {s["stage"]: s["state"] for s in t["stages"]},
            }
            prev = self._prev_states.get(key)
            if prev != cur:
                if prev is not None:
                    events.append(
                        {
                            "at": snapshot["generated_at"],
                            "peer": snapshot["peer"],
                            "topic": t["base"],
                            "direction": t["direction"],
                            "from": {
                                "overall": prev.get("overall"),
                                "reached_stage": prev.get("reached_stage"),
                                "blocked_at": prev.get("blocked_at"),
                            },
                            "to": {
                                "overall": cur["overall"],
                                "reached_stage": cur["reached_stage"],
                                "blocked_at": cur["blocked_at"],
                            },
                            "diagnosis": t["diagnosis"],
                        }
                    )
                self._prev_states[key] = cur
        return events

    def render_text(self, snapshot: Dict[str, Any]) -> str:
        lines: List[str] = []
        s = snapshot["summary"]
        lines.append(
            f"rosotacom status  peer={snapshot['peer']} remote={snapshot['remote']}  "
            f"{snapshot['generated_at']}"
        )
        lines.append(
            f"summary: OK={s.get('OK', 0)} PARTIAL={s.get('PARTIAL', 0)} "
            f"STALLED={s.get('STALLED', 0)} ABSENT={s.get('ABSENT', 0)}   (Phase 1: local observation)"
        )
        lines.append("")
        for t in snapshot["topics"]:
            arrow = "->" if t["direction"] == "outbound" else "<-"
            lines.append(
                f"[{t['overall']:<7}] {arrow} {t['base']}  "
                f"(reached: {t['reached_stage']}, blocked: {t['blocked_at']})"
            )
            for st in t["stages"]:
                state = st["state"]
                if state == FLOWING and st.get("quality") and st["quality"] != GOOD:
                    state = f"{state}/{st['quality']}"
                metric = ""
                if st["state"] in (FLOWING, STALE):
                    parts = [f"{st['hz']:.1f}Hz"]
                    if st["latency_ms"] is not None:
                        parts.append(f"{st['latency_ms']:.0f}ms")
                    if st["mean_size_bytes"]:
                        parts.append(f"{st['mean_size_bytes']:.0f}B")
                    if st["age_s"] is not None:
                        parts.append(f"age {st['age_s']:.1f}s")
                    metric = "  " + " ".join(parts)
                lines.append(
                    f"    {st['stage']:<10} {state:<14} "
                    f"pub={st['publishers']} sub={st['subscribers']}  {st['topic']}{metric}"
                )
            if t["diagnosis"]:
                lines.append(f"    -> {t['diagnosis']}")
            lines.append("")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _atomic_write(path: str, text: str) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(text)
        os.replace(tmp, path)

    def write(self, now_mono: Optional[float] = None) -> int:
        """Build a snapshot, write artifacts, and return number of transition events."""
        snapshot = self.build_snapshot(now_mono)
        events = self.detect_transitions(snapshot)
        try:
            self._atomic_write(self._json_path, json.dumps(snapshot, indent=2) + "\n")
            self._atomic_write(self._txt_path, self.render_text(snapshot))
            if events:
                with open(self._events_path, "a", encoding="utf-8") as fp:
                    for ev in events:
                        fp.write(json.dumps(ev) + "\n")
        except Exception as exc:  # pragma: no cover - defensive
            if self._log is not None:
                self._log.warning(f"status_overview: failed to write artifacts: {exc}")
        return len(events)


def collect_stage_topics(spec: Dict[str, Any]) -> Dict[str, Dict[str, Optional[str]]]:
    """Return {domain: {topic: type_hint}} from the pipeline spec."""
    by_domain: Dict[str, Dict[str, Optional[str]]] = {"local": {}, "ota": {}}
    for topic_spec in spec.get("topics", []):
        type_hint = topic_spec.get("type")
        for stage in topic_spec.get("stages", []):
            domain = stage.get("domain", "local")
            by_domain.setdefault(domain, {})
            existing = by_domain[domain].get(stage["topic"])
            by_domain[domain][stage["topic"]] = existing or type_hint
    return by_domain
