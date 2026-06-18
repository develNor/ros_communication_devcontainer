"""Unit tests for the rosotacom status / debugging overview.

Covers three layers:
  * pipeline_spec.yaml generation (generate_session_files._build_status_pipeline_spec
    exercised end-to-end via func()),
  * state classification + rollup (status_overview_core, ROS-independent),
  * the `rosotacom status` host CLI rendering.

The in-container modules live under resources/ws and are loaded by file path so
the host test suite (which has no rclpy) can import the pure parts.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import rosotacom.cli as rosotacom

REPO_ROOT = Path(__file__).resolve().parents[2]
WS = REPO_ROOT / "src" / "rosotacom" / "resources" / "ws"
GENERATOR_PY = WS / "session" / "creation" / "generate_session_files.py"
STATUS_CORE_PY = WS / "ros2src" / "com_py" / "com_py" / "status_overview_core.py"
STATUS_NODE_PY = WS / "ros2src" / "com_py" / "com_py" / "status_overview.py"


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve the module's namespace.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_module(GENERATOR_PY, "rosotacom_generate_session_files")
core = _load_module(STATUS_CORE_PY, "rosotacom_status_overview_core")


# ---------------------------------------------------------------------------
# pipeline_spec generation
# ---------------------------------------------------------------------------


def _heartbeat_cfg() -> dict:
    return {
        "peers": {"a": {"address": "127.0.0.1"}, "b": {"address": "127.0.0.1"}},
        "peer_settings": {"a": {"domain_id": 46}, "b": {"domain_id": 47}},
        "shared": {
            "use_heartbeat": True,
            "use_status_overview": True,
            "rmw": {
                "local": {"cyclone": {"config": "cyclonedds_minimal.xml"}},
                "ota": {"cyclone": {"config": "cyclonedds_minimal.xml"}},
            },
            "ota_domain_id": 48,
        },
    }


def test_pipeline_spec_generated_for_heartbeat(tmp_path: Path) -> None:
    import yaml

    generator.func(session_config_obj=_heartbeat_cfg(), output_dir=str(tmp_path), force=True)

    spec_path = tmp_path / "a" / "pipeline_spec.yaml"
    assert spec_path.exists(), "pipeline_spec.yaml should be generated for peer a"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert spec["peer"] == "a"
    assert spec["remote"] == "b"
    assert spec["local_domain_id"] == 46
    assert spec["ota_domain_id"] == 48
    assert spec["uses_domain_bridge"] is True

    by_dir = {(t["base"], t["direction"]): t for t in spec["topics"]}
    out = by_dir[("/heartbeat_a", "outbound")]
    out_stages = {s["stage"]: s for s in out["stages"]}
    assert out_stages["native"]["topic"] == "/heartbeat_a"
    assert out_stages["native"]["domain"] == "local"
    assert out_stages["com_out"]["topic"] == "/com/out/a/heartbeat_a"
    assert out_stages["ota_sent"]["topic"] == "/ota/a/heartbeat_a"
    assert out_stages["ota_sent"]["domain"] == "ota"

    inb = by_dir[("/heartbeat_b", "inbound")]
    in_stages = {s["stage"]: s for s in inb["stages"]}
    assert in_stages["ota_recv"]["topic"] == "/ota/b/heartbeat_b"
    assert in_stages["ota_recv"]["domain"] == "ota"
    assert in_stages["com_in"]["topic"] == "/com/in/b/heartbeat_b"
    assert in_stages["app_in"]["topic"] == "/heartbeat_b"


def test_pipeline_spec_absent_when_flag_off(tmp_path: Path) -> None:
    cfg = _heartbeat_cfg()
    cfg["shared"]["use_status_overview"] = False
    generator.func(session_config_obj=cfg, output_dir=str(tmp_path), force=True)
    assert not (tmp_path / "a" / "pipeline_spec.yaml").exists()


def test_use_status_overview_must_be_bool() -> None:
    cfg = _heartbeat_cfg()
    cfg["shared"]["use_status_overview"] = "yes"
    with pytest.raises(RuntimeError):
        generator._validate_session_template_cfg(cfg)


def test_ota_observer_is_graph_only() -> None:
    """Regression: status monitoring must never create an OTA DataReader."""
    source = STATUS_NODE_PY.read_text(encoding="utf-8")
    assert "subscribe_to_messages=False" in source
    assert "if not self._subscribe_to_messages:" in source
    assert 'local_topics.update(by_domain.get("ota"' not in source


# ---------------------------------------------------------------------------
# state classification + rollup
# ---------------------------------------------------------------------------


class _FakeObserver:
    def __init__(self) -> None:
        self.observations: dict = {}


def _outbound_spec() -> dict:
    return {
        "peer": "a",
        "remote": "b",
        "local_domain_id": 46,
        "ota_domain_id": 48,
        "uses_domain_bridge": True,
        "topics": [
            {
                "base": "/heartbeat_a",
                "direction": "outbound",
                "source": "a",
                "target": "b",
                "type": "com_msgs/msg/Heartbeat",
                "expected_hz": None,
                "stages": [
                    {"stage": "native", "topic": "/heartbeat_a", "domain": "local", "produced_by": "application"},
                    {
                        "stage": "com_out",
                        "topic": "/com/out/a/heartbeat_a",
                        "domain": "local",
                        "produced_by": "relay_out",
                    },
                    {"stage": "ota_sent", "topic": "/ota/a/heartbeat_a", "domain": "ota", "produced_by": "bridge_out"},
                ],
            }
        ],
    }


def _build(observers: dict, output_dir: Path) -> core.StatusAggregator:
    return core.StatusAggregator(
        logger=None,
        spec=_outbound_spec(),
        output_dir=str(output_dir),
        observers_by_domain=observers,
        liveness_window_s=3.0,
        stale_after_s=3.0,
    )


def _obs(
    pub: int = 0,
    last_msg_at: float | None = None,
    *,
    graph_only: bool = False,
) -> core.StageObservation:
    o = core.StageObservation(graph_only=graph_only)
    o.pub_count = pub
    o.sub_count = 1 if pub else 0
    if last_msg_at is not None:
        o.record(size=64, delay_s=0.01, now_mono=last_msg_at, now_wall=1_700_000_000.0)
    return o


def test_rollup_ok_when_all_stages_flow(tmp_path: Path) -> None:
    now = 100.0
    local = _FakeObserver()
    ota = _FakeObserver()
    local.observations["/heartbeat_a"] = _obs(pub=1, last_msg_at=now)
    local.observations["/com/out/a/heartbeat_a"] = _obs(pub=1, last_msg_at=now)
    ota.observations["/ota/a/heartbeat_a"] = _obs(pub=1, graph_only=True)

    agg = _build({"local": local, "ota": ota}, tmp_path)
    snap = agg.build_snapshot(now_mono=now)
    topic = snap["topics"][0]
    assert topic["overall"] == core.OK
    assert topic["reached_stage"] == "ota_sent"
    assert topic["blocked_at"] is None
    assert "Phase 1" in topic["diagnosis"]
    ota_stage = {s["stage"]: s for s in topic["stages"]}["ota_sent"]
    assert ota_stage["state"] == core.FLOWING
    assert ota_stage["observation"] == "graph"
    assert ota_stage["inferred_from"] == "/com/out/a/heartbeat_a"
    assert ota_stage["messages_total"] == 1
    assert snap["summary"]["OK"] == 1


def test_inbound_ota_receipt_is_inferred_from_com_in(tmp_path: Path) -> None:
    now = 100.0
    spec = {
        "peer": "a",
        "remote": "b",
        "topics": [
            {
                "base": "/heartbeat_b",
                "direction": "inbound",
                "stages": [
                    {
                        "stage": "ota_recv",
                        "topic": "/ota/b/heartbeat_b",
                        "domain": "ota",
                    },
                    {
                        "stage": "com_in",
                        "topic": "/com/in/b/heartbeat_b",
                        "domain": "local",
                    },
                    {
                        "stage": "app_in",
                        "topic": "/heartbeat_b",
                        "domain": "local",
                    },
                ],
            }
        ],
    }
    local = _FakeObserver()
    ota = _FakeObserver()
    ota.observations["/ota/b/heartbeat_b"] = _obs(pub=1, graph_only=True)
    local.observations["/com/in/b/heartbeat_b"] = _obs(pub=1, last_msg_at=now)
    local.observations["/heartbeat_b"] = _obs(pub=1, last_msg_at=now)

    agg = core.StatusAggregator(
        logger=None,
        spec=spec,
        output_dir=str(tmp_path),
        observers_by_domain={"local": local, "ota": ota},
    )
    topic = agg.build_snapshot(now_mono=now)["topics"][0]
    ota_stage = {s["stage"]: s for s in topic["stages"]}["ota_recv"]
    assert topic["overall"] == core.OK
    assert ota_stage["state"] == core.FLOWING
    assert ota_stage["inferred_from"] == "/com/in/b/heartbeat_b"


def test_rollup_partial_identifies_first_broken_stage(tmp_path: Path) -> None:
    now = 100.0
    local = _FakeObserver()
    ota = _FakeObserver()
    local.observations["/heartbeat_a"] = _obs(pub=1, last_msg_at=now)
    # com_out has a publisher but no messages observed -> IDLE
    local.observations["/com/out/a/heartbeat_a"] = _obs(pub=1, last_msg_at=None)
    ota.observations["/ota/a/heartbeat_a"] = _obs(pub=0, last_msg_at=None)

    agg = _build({"local": local, "ota": ota}, tmp_path)
    snap = agg.build_snapshot(now_mono=now)
    topic = snap["topics"][0]
    assert topic["overall"] == core.PARTIAL
    assert topic["reached_stage"] == "native"
    assert topic["blocked_at"] == "com_out"
    stages = {s["stage"]: s for s in topic["stages"]}
    assert stages["com_out"]["state"] == core.IDLE
    assert stages["ota_sent"]["state"] == core.ABSENT


def test_rollup_absent_when_nothing_publishes(tmp_path: Path) -> None:
    now = 100.0
    local = _FakeObserver()
    ota = _FakeObserver()
    local.observations["/heartbeat_a"] = _obs(pub=0)
    local.observations["/com/out/a/heartbeat_a"] = _obs(pub=0)
    ota.observations["/ota/a/heartbeat_a"] = _obs(pub=0)

    agg = _build({"local": local, "ota": ota}, tmp_path)
    snap = agg.build_snapshot(now_mono=now)
    topic = snap["topics"][0]
    assert topic["overall"] == core.ABSENT
    assert topic["reached_stage"] is None
    assert topic["blocked_at"] == "native"


def test_rollup_stalled_when_last_stage_goes_stale(tmp_path: Path) -> None:
    now = 100.0
    local = _FakeObserver()
    ota = _FakeObserver()
    local.observations["/heartbeat_a"] = _obs(pub=1, last_msg_at=now)
    local.observations["/com/out/a/heartbeat_a"] = _obs(pub=1, last_msg_at=now)
    # ota stage last received 10s ago -> STALE (stale_after=3)
    ota.observations["/ota/a/heartbeat_a"] = _obs(pub=1, last_msg_at=now - 10.0)

    agg = _build({"local": local, "ota": ota}, tmp_path)
    snap = agg.build_snapshot(now_mono=now)
    topic = snap["topics"][0]
    assert topic["overall"] == core.STALLED
    assert topic["reached_stage"] == "ota_sent"
    stages = {s["stage"]: s for s in topic["stages"]}
    assert stages["ota_sent"]["state"] == core.STALE


def test_write_produces_artifacts_and_transition_events(tmp_path: Path) -> None:
    now = 100.0
    local = _FakeObserver()
    ota = _FakeObserver()
    local.observations["/heartbeat_a"] = _obs(pub=1, last_msg_at=now)
    local.observations["/com/out/a/heartbeat_a"] = _obs(pub=1, last_msg_at=now)
    ota.observations["/ota/a/heartbeat_a"] = _obs(pub=0, graph_only=True)

    agg = _build({"local": local, "ota": ota}, tmp_path)
    # First write establishes baseline (no transition events emitted).
    assert agg.write(now_mono=now) == 0
    assert (tmp_path / "status.json").exists()
    assert (tmp_path / "status.txt").exists()
    assert not (tmp_path / "events.jsonl").exists()

    # Now the ota stage starts flowing -> overall changes -> one event row.
    ota.observations["/ota/a/heartbeat_a"] = _obs(pub=1, graph_only=True)
    assert agg.write(now_mono=now) == 1
    events_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(events_text) == 1


# ---------------------------------------------------------------------------
# `rosotacom status` CLI rendering
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path) -> tuple[argparse.Namespace, Path]:
    (tmp_path / "ros2docker.json").write_text('{"image_name": "test"}\n', encoding="utf-8")
    sessions = tmp_path / "sessions" / "mysess"
    sessions.mkdir(parents=True)
    (sessions / "session-definition.yaml").write_text("peers: {}\n", encoding="utf-8")
    (tmp_path / "session-instances").mkdir()
    config = tmp_path / "rosotacom.yaml"
    config.write_text(
        "ros2docker_config: ros2docker.json\nsession_configs_dir: sessions\nsession_instances_dir: session-instances\n",
        encoding="utf-8",
    )
    instance = tmp_path / "session-instances" / "2026-01-01" / "mysess_2026-01-01_00-00-00_abcd1234"
    status_dir = instance / "logs" / "a" / "status"
    status_dir.mkdir(parents=True)
    import json

    snapshot = {
        "schema_version": 1,
        "phase": 1,
        "generated_at": "2026-01-01T00:00:01.000",
        "peer": "a",
        "remote": "b",
        "summary": {"OK": 1, "PARTIAL": 0, "STALLED": 0, "ABSENT": 0},
        "topics": [
            {
                "base": "/heartbeat_a",
                "direction": "outbound",
                "overall": "OK",
                "reached_stage": "ota_sent",
                "blocked_at": None,
                "diagnosis": "all observable stages flowing",
                "stages": [],
            }
        ],
    }
    (status_dir / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (status_dir / "status.txt").write_text("RENDERED STATUS TEXT\n", encoding="utf-8")

    args = argparse.Namespace(
        rosotacom_config=str(config),
        ros2docker_config=None,
        session_configs_dir=None,
        session_instances_dir=None,
        data_dict=None,
        session_dir="mysess",
        identity=None,
        instance_id=None,
        json=False,
        watch=False,
        watch_interval=2.0,
    )
    return args, instance


def test_status_cli_human_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args, _ = _make_project(tmp_path)
    rc = rosotacom.status(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "RENDERED STATUS TEXT" in out


def test_status_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    args, _ = _make_project(tmp_path)
    args.json = True
    rc = rosotacom.status(args)
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "a" in payload
    assert payload["a"]["topics"][0]["base"] == "/heartbeat_a"


# ---------------------------------------------------------------------------
# per-topic `expect` -> live classification + spec
# ---------------------------------------------------------------------------


def _quality(tmp_path: Path, m: dict, expected_hz, expect) -> tuple:
    agg = _build({"local": _FakeObserver(), "ota": _FakeObserver()}, tmp_path)
    return agg._classify_quality(m, expected_hz, expect)


def test_classify_quality_hz_outside_expect_range_is_bad(tmp_path: Path) -> None:
    assert _quality(tmp_path, {"hz": 3.0, "last_delay_s": 0.01}, None, {"hz": {"min": 5, "max": 20}}) == (
        core.BAD,
        "hz",
    )
    assert _quality(tmp_path, {"hz": 25.0, "last_delay_s": 0.01}, None, {"hz": {"max": 20}}) == (core.BAD, "hz")


def test_classify_quality_latency_over_expect_max_is_bad(tmp_path: Path) -> None:
    assert _quality(tmp_path, {"hz": 10.0, "last_delay_s": 0.25}, None, {"latency_ms": {"max": 200}}) == (
        core.BAD,
        "latency",
    )


def test_classify_quality_within_expect_is_good(tmp_path: Path) -> None:
    expect = {"hz": {"min": 5, "max": 20}, "latency_ms": {"max": 200}}
    assert _quality(tmp_path, {"hz": 10.0, "last_delay_s": 0.01}, None, expect) == (core.GOOD, None)


def test_classify_quality_without_expect_uses_expected_hz_heuristic(tmp_path: Path) -> None:
    assert _quality(tmp_path, {"hz": 4.0, "last_delay_s": 0.01}, 10.0, None) == (core.BAD, "hz")


def test_build_snapshot_applies_per_topic_expect(tmp_path: Path) -> None:
    # Full node path: a spec topic carrying `expect` is classified against it and
    # the contract is surfaced in the snapshot.
    now = 100.0
    spec = _outbound_spec()
    spec["topics"][0]["expect"] = {"hz": {"min": 5, "max": 20}}
    local = _FakeObserver()
    ota = _FakeObserver()
    local.observations["/heartbeat_a"] = _obs(pub=1, last_msg_at=now)  # ~0.33 Hz, below min 5
    local.observations["/com/out/a/heartbeat_a"] = _obs(pub=1, last_msg_at=now)
    ota.observations["/ota/a/heartbeat_a"] = _obs(pub=1, graph_only=True)

    agg = core.StatusAggregator(
        logger=None,
        spec=spec,
        output_dir=str(tmp_path),
        observers_by_domain={"local": local, "ota": ota},
        liveness_window_s=3.0,
        stale_after_s=3.0,
    )
    snap = agg.build_snapshot(now_mono=now)
    assert snap["topics"][0]["expect"] == {"hz": {"min": 5, "max": 20}}
    native = {s["stage"]: s for s in snap["topics"][0]["stages"]}["native"]
    assert native["quality"] == core.BAD
    assert native["quality_reason"] == "hz"


def test_pipeline_spec_carries_per_topic_expect(tmp_path: Path) -> None:
    import yaml

    cfg = {
        "peers": {"a": {"address": "127.0.0.1"}, "b": {"address": "127.0.0.1"}},
        "peer_settings": {"a": {"domain_id": 46}, "b": {"domain_id": 47}},
        "shared": {
            "use_status_overview": True,
            "rmw": {
                "local": {"cyclone": {"config": "cyclonedds_minimal.xml"}},
                "ota": {"cyclone": {"config": "cyclonedds_minimal.xml"}},
            },
            "ota_domain_id": 48,
        },
        "topics": {
            "b_to_a": [
                {
                    "topic": "/costmap",
                    "type": "nav_msgs/msg/OccupancyGrid",
                    "expect": {"hz": {"min": 1, "max": 5}, "latency_ms": {"max": 500}},
                }
            ]
        },
    }
    generator.func(session_config_obj=cfg, output_dir=str(tmp_path), force=True)
    spec = yaml.safe_load((tmp_path / "a" / "pipeline_spec.yaml").read_text(encoding="utf-8"))
    by_dir = {(t["base"], t["direction"]): t for t in spec["topics"]}
    assert by_dir[("/costmap", "inbound")]["expect"] == {"hz": {"min": 1, "max": 5}, "latency_ms": {"max": 500}}


def test_pipeline_spec_carries_heartbeat_expect(tmp_path: Path) -> None:
    import yaml

    cfg = _heartbeat_cfg()
    contract = {"hz": {"min": 8, "max": 12}, "latency_ms": {"max": 200}}
    cfg["shared"]["heartbeat"] = {"expect": contract}
    generator.func(session_config_obj=cfg, output_dir=str(tmp_path), force=True)
    spec = yaml.safe_load((tmp_path / "a" / "pipeline_spec.yaml").read_text(encoding="utf-8"))
    by_dir = {(t["base"], t["direction"]): t for t in spec["topics"]}
    # The contract applies to both the locally-published and the received heartbeat.
    assert by_dir[("/heartbeat_a", "outbound")]["expect"] == contract
    assert by_dir[("/heartbeat_b", "inbound")]["expect"] == contract


def test_heartbeat_monitor_thresholds_overridden_from_expect(tmp_path: Path) -> None:
    # latency/loss from shared.heartbeat.expect override the heartbeat_in_monitor
    # status thresholds (delay_bad_ms / loss3_bad_pct).
    cfg = _heartbeat_cfg()
    cfg["shared"]["heartbeat"] = {"expect": {"latency_ms": {"max": 150}, "loss_pct": {"max": 3}}}
    generator.func(session_config_obj=cfg, output_dir=str(tmp_path), force=True)
    plugin = (tmp_path / "a" / "plugin.yaml").read_text(encoding="utf-8")
    assert "heartbeat_delay_bad_ms: 150.0" in plugin
    assert "heartbeat_loss3_bad_pct: 3.0" in plugin


def test_heartbeat_monitor_thresholds_default_without_expect(tmp_path: Path) -> None:
    generator.func(session_config_obj=_heartbeat_cfg(), output_dir=str(tmp_path), force=True)
    plugin = (tmp_path / "a" / "plugin.yaml").read_text(encoding="utf-8")
    # No override emitted -> the plugin base template defaults apply.
    assert "heartbeat_delay_bad_ms" not in plugin
    assert "heartbeat_loss3_bad_pct" not in plugin
