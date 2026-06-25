"""RFC 0005 — ``rosotacom benchmark`` subcommand: live driver + CLI glue.

This wires :mod:`rosotacom.benchmark`'s pure, host-tested logic to real runs.
The pattern mirrors :func:`rosotacom.cli._start_noninteractive_ota_smoke`: a
probe starts a session under a profile, waits, collects ``events.jsonl``, and
feeds the transit-record pipeline. What lives here is the *benchmark-specific*
orchestration; the underlying arming, collection, and session lifecycle are all
reused from the existing OTA smoke path.

The live runs themselves are *non-deterministic* (monitor-only, never gated).
The glue below is host-tested via a **stubbed** ``run_point`` in
``tests/unit/test_cli_benchmark.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shlex
import shutil
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

DEFAULT_BENCHMARK_RMW = "cyclone"
BENCHMARK_RESULT_FILE = "result.json"


class TeeStream:
    def __init__(self, original_stream: Any, file_path: Path):
        self.original_stream: Any = original_stream
        self.file_path: Path = file_path
        self.file: Any = None

    def write(self, data: Any) -> None:
        self.original_stream.write(data)
        self.original_stream.flush()
        if self.file is None:
            try:
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                self.file = open(self.file_path, "a", encoding="utf-8")
            except Exception:
                pass
        if self.file is not None:
            try:
                self.file.write(data)
                self.file.flush()
            except Exception:
                pass

    def flush(self) -> None:
        self.original_stream.flush()
        if self.file is not None:
            try:
                self.file.flush()
            except Exception:
                pass

    def close(self) -> None:
        if self.file is not None:
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None


@contextlib.contextmanager
def log_stdout_stderr_to_file(file_path: Path) -> Iterator[None]:
    import subprocess

    tee_stdout = TeeStream(sys.stdout, file_path)
    tee_stderr = TeeStream(sys.stderr, file_path)
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = tee_stdout
    sys.stderr = tee_stderr

    original_run = subprocess.run

    def hooked_run(*args, **kwargs):
        capture_stdout = kwargs.get("stdout") is None and not kwargs.get("capture_output")
        capture_stderr = kwargs.get("stderr") is None and not kwargs.get("capture_output")

        if capture_stdout:
            kwargs["stdout"] = subprocess.PIPE
        if capture_stderr:
            kwargs["stderr"] = subprocess.PIPE

        try:
            res = original_run(*args, **kwargs)
            if capture_stdout and res.stdout:
                val = res.stdout
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                sys.stdout.write(val)
                sys.stdout.flush()
            if capture_stderr and res.stderr:
                val = res.stderr
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                sys.stderr.write(val)
                sys.stderr.flush()
            return res
        except subprocess.CalledProcessError as exc:
            if capture_stdout and exc.stdout:
                val = exc.stdout
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                sys.stdout.write(val)
                sys.stdout.flush()
            if capture_stderr and exc.stderr:
                val = exc.stderr
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                sys.stderr.write(val)
                sys.stderr.flush()
            raise

    subprocess.run = hooked_run

    try:
        yield
    finally:
        subprocess.run = original_run
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        tee_stdout.close()
        tee_stderr.close()


# --------------------------------------------------------------------------- #
# Transit-record collection
# --------------------------------------------------------------------------- #


def _find_events_files(instance_dir: Path) -> list[Path]:
    """Find all ``events.jsonl`` files under an instance's log tree.

    Confirmed path: ``<instance>/logs/<peer>/status/events.jsonl``, written
    next to ``status.json`` by :mod:`rosotacom.status_overview_core`.
    """
    return sorted(instance_dir.rglob("status/events.jsonl"))


def collect_transit_summary(instance_dir: Path) -> dict[str, Any]:
    """Load and summarize transit records from an instance's artifact tree."""
    from .transit import load_transit_records, summarize_transit_records

    events_files = _find_events_files(instance_dir)
    if not events_files:
        raise FileNotFoundError(
            f"No status/events.jsonl files found under {instance_dir}. "
            "Did the benchmark point run long enough to produce transit records?"
        )
    records = load_transit_records(events_files)
    return summarize_transit_records(records)


def collect_transit_records(instance_dir: Path) -> list[dict[str, Any]]:
    """Load raw joined transit records from an instance's artifact tree."""
    from .transit import join_transit_records, load_transit_records

    events_files = _find_events_files(instance_dir)
    if not events_files:
        raise FileNotFoundError(f"No status/events.jsonl files found under {instance_dir}.")
    return join_transit_records(load_transit_records(events_files))


# --------------------------------------------------------------------------- #
# Run-point primitive (the live probe, injected for testing)
# --------------------------------------------------------------------------- #

# The default probe callable is replaced in production by the CLI wiring;
# tests inject a stub.  ``RunPointFn`` is the signature.
RunPointFn = Callable[..., dict[str, Any]]


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def _benchmark_rmw(args: argparse.Namespace) -> str:
    return str(getattr(args, "rmw", None) or DEFAULT_BENCHMARK_RMW)


def _rmw_runtime_implementation(rmw: str) -> str:
    from .cli import session_gen

    return str(session_gen.RMW_ALIASES.get(rmw, rmw))


def _prepare_benchmark_session_config(args: argparse.Namespace, session_name: str, run_dir: Path) -> dict[str, Any]:
    """Copy the benchmark session into the run artifacts and pin ``shared.rmw``."""
    from .cli import _load_runtime_config, _resolve_session

    rmw = _benchmark_rmw(args)
    runtime = _load_runtime_config(args)
    source_session = _resolve_session(session_name, runtime)
    dest_root = run_dir / "session-configs"
    dest_session = dest_root / session_name
    shutil.copytree(source_session.host_dir, dest_session)

    config_path = dest_session / "session-definition.yaml"
    if not config_path.is_file():
        config_path = dest_session / "session-parametrization.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(f"Benchmark session {session_name!r} has no session YAML: {source_session.host_dir}")

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(cfg, dict):
        raise RuntimeError(f"Benchmark session config must be a mapping: {config_path}")
    shared = cfg.setdefault("shared", {})
    if not isinstance(shared, dict):
        raise RuntimeError(f"Benchmark session config 'shared' must be a mapping: {config_path}")
    shared["rmw"] = rmw
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    existing_raw = getattr(args, "session_configs_dir", None)
    if existing_raw is None:
        existing: list[Any] = []
    elif isinstance(existing_raw, list | tuple):
        existing = list(existing_raw)
    else:
        existing = [existing_raw]
    args.session_configs_dir = [str(dest_root), *[str(path) for path in existing]]
    args._benchmark_session_dir = str(dest_session)
    args._benchmark_session_source = str(source_session.host_dir)
    args._benchmark_session_config = str(config_path)

    return {
        "name": session_name,
        "source_dir": source_session.host_dir,
        "artifact_dir": dest_session,
        "config_path": config_path,
        "rmw": rmw,
        "runtime_implementation": _rmw_runtime_implementation(rmw),
    }


def _profile_context(profile: str | None, profiles_file: Path | None) -> dict[str, Any]:
    if not profile:
        return {"name": None, "kind": "none", "configured": None}
    context: dict[str, Any] = {"name": profile, "profiles_file": profiles_file}
    if profiles_file is None:
        context["configured"] = None
        return cast(dict[str, Any], _jsonable(context))
    try:
        from .network_profiles import load_profiles_file

        configured = load_profiles_file(profiles_file).get(profile)
    except Exception as exc:
        context["configured"] = None
        context["load_error"] = str(exc)
    else:
        context["configured"] = configured
    return cast(dict[str, Any], _jsonable(context))


def _benchmark_result_context(
    args: argparse.Namespace,
    *,
    genre: str,
    profile: str | None,
    run_dir: Path,
    session: dict[str, Any] | None,
) -> dict[str, Any]:
    from .cli import _load_runtime_config

    runtime = _load_runtime_config(args)
    rmw = _benchmark_rmw(args)
    return cast(
        dict[str, Any],
        _jsonable(
            {
                "command": " ".join(shlex.quote(arg) for arg in sys.argv),
                "genre": genre,
                "execution": {
                    "mode": "ota" if getattr(args, "deployment", None) else "local",
                    "dry_run": bool(getattr(args, "dry_run", False)),
                },
                "rmw": {
                    "requested": rmw,
                    "runtime_implementation": _rmw_runtime_implementation(rmw),
                },
                "profile": _profile_context(profile, runtime.profiles_file),
                "session": session,
                "paths": {
                    "run_dir": run_dir,
                    "rosotacom_config": runtime.rosotacom_config,
                    "ros2docker_config": runtime.ros2docker_config,
                    "profiles_file": runtime.profiles_file,
                    "benchmarks_dir": runtime.benchmarks_dir,
                },
            }
        ),
    )


def _mean_payload_bytes(load: dict[str, Any]) -> float | None:
    size_a_raw = load.get("size_a", load.get("size"))
    if size_a_raw is None:
        return None
    try:
        size_a = int(size_a_raw)
        size_b = int(load["size_b"]) if load.get("size_b") is not None else None
        pattern = str(load.get("pattern") or load.get("size_pattern") or "")
        if pattern:
            from .benchmark import pattern_mean_bytes

            return float(pattern_mean_bytes(pattern, size_a, size_b))
        return float(size_a)
    except Exception:
        return None


def _load_context(load: dict[str, Any]) -> dict[str, Any]:
    rate_hz = float(load.get("rate", load.get("rate_hz", 0.0)) or 0.0)
    streams = int(load.get("streams", 1) or 1)
    mean_payload_bytes = _mean_payload_bytes(load)
    offered_bandwidth_bps = None
    if mean_payload_bytes is not None:
        offered_bandwidth_bps = mean_payload_bytes * 8.0 * rate_hz * streams
    return cast(
        dict[str, Any],
        _jsonable(
            {
                "parameters": dict(load),
                "rate_hz": rate_hz,
                "streams": streams,
                "mean_payload_bytes": mean_payload_bytes,
                "offered_bandwidth_bps": offered_bandwidth_bps,
            }
        ),
    )


def _format_bps(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.3g} Mbit/s"
    if value >= 1_000:
        return f"{value / 1_000:.3g} kbit/s"
    return f"{value:.0f} bit/s"


def _topic_rows(summary: dict[str, Any], topic: str = "") -> list[dict[str, Any]]:
    topics = summary.get("topics", {})
    if not isinstance(topics, dict):
        return []
    selected = [(topic, topics.get(topic, {}))] if topic else list(topics.items())
    rows: list[dict[str, Any]] = []
    for topic_name, topic_data in selected:
        topic_data = topic_data if isinstance(topic_data, dict) else {}
        ota = topic_data.get("ota_hop_ms") or {}
        jitter = topic_data.get("jitter_ms") or {}
        rows.append(
            {
                "topic": str(topic_name),
                "expected": topic_data.get("expected"),
                "delivered": topic_data.get("delivered"),
                "lost": topic_data.get("lost"),
                "loss_pct": topic_data.get("loss_pct"),
                "reordered": topic_data.get("reordered"),
                "latency_ms": {
                    "p50": ota.get("p50") if isinstance(ota, dict) else None,
                    "p95": ota.get("p95") if isinstance(ota, dict) else None,
                },
                "jitter_ms": {
                    "p50": jitter.get("p50") if isinstance(jitter, dict) else None,
                    "p95": jitter.get("p95") if isinstance(jitter, dict) else None,
                },
            }
        )
    return rows


def _format_topic_rows(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return "no topic metrics"
    parts = []
    for row in rows:
        loss = row.get("loss_pct")
        latency_p95 = (row.get("latency_ms") or {}).get("p95")
        jitter_p95 = (row.get("jitter_ms") or {}).get("p95")
        parts.append(
            f"{row.get('topic')}: delivered={row.get('delivered')}/{row.get('expected')} "
            f"lost={row.get('lost')} loss={loss}% p95={latency_p95}ms jitter_p95={jitter_p95}ms"
        )
    return "; ".join(parts)


def _write_benchmark_result(
    out_dir: Path,
    *,
    genre: str,
    configuration: dict[str, Any],
    result: dict[str, Any] | list[dict[str, Any]],
    measurements: dict[str, Any],
    verdict: dict[str, Any],
    context: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
) -> Path:
    doc = _jsonable(
        {
            "schema_version": 1,
            "created_at": datetime.now().isoformat(),
            "genre": genre,
            "context": context or {},
            "configuration": configuration,
            "measurements": measurements,
            "result": result,
            "verdict": verdict,
            "artifacts": artifacts or {},
        }
    )
    path = out_dir / BENCHMARK_RESULT_FILE
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Benchmark result saved to {path}")
    return path


def _default_run_point(
    *,
    profile: str | None,
    load: dict[str, Any],
    duration_s: float,
    out_dir: Path,
) -> dict[str, Any]:
    """Placeholder probe — the live implementation is wired by the CLI.

    In production, this is never called directly; the benchmark subcommands
    inject the real probe via ``_make_live_run_point``. For host-deterministic
    tests, a stubbed probe replaces this entirely.
    """
    raise NotImplementedError(
        "run_point requires a live rosotacom session. This should be called through the CLI benchmark subcommands."
    )


# --------------------------------------------------------------------------- #
# Benchmark driver: capacity
# --------------------------------------------------------------------------- #


def drive_capacity(
    run_point: RunPointFn,
    *,
    profile: str,
    knob: str,
    low: int,
    high: int,
    max_loss_pct: float,
    max_latency_ms: float,
    rate_hz: float = 20.0,
    topic: str = "",
    repeats: int = 1,
    duration_s: float = 60.0,
    out_dir: Path,
    result_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive a capacity binary search using real runs.

    Returns the capacity result dict (slice + capacity value + budget metrics).
    """
    from .benchmark import (
        BudgetEntry,
        BudgetKey,
        CapacitySlice,
        OracleThresholds,
        find_capacity,
        oracle_passes_topic,
        save_budget,
    )

    thresholds = OracleThresholds(max_loss_pct=max_loss_pct, max_latency_ms=max_latency_ms)
    slice_ = CapacitySlice(profile=profile, knob=knob, fixed={"rate": rate_hz})
    probe_results: list[dict[str, Any]] = []

    def probe(value: int) -> bool:
        """Run a live point at the given knob value and check the oracle."""
        load: dict[str, Any] = {"rate": rate_hz}
        if knob == "size":
            load["size_a"] = value
        elif knob == "rate":
            load["rate"] = float(value)
        elif knob == "bandwidth":
            load["size_a"] = int(value / (8.0 * rate_hz)) if rate_hz > 0 else value
        else:
            load[knob] = value

        results: list[bool] = []
        for attempt in range(repeats):
            summary = run_point(profile=profile, load=load, duration_s=duration_s, out_dir=out_dir)
            topics = summary.get("topics", {})
            rows = _topic_rows(summary, topic)
            if topic:
                passes = oracle_passes_topic(topics.get(topic, {}), thresholds)
            else:
                passes = all(oracle_passes_topic(t, thresholds) for t in topics.values()) if topics else False
            results.append(passes)
            load_info = _load_context(load)
            probe_results.append(
                {
                    "knob": knob,
                    "value": value,
                    "attempt": attempt + 1,
                    "passed": passes,
                    "load": load_info,
                    "topics": rows,
                    "summary": summary,
                }
            )
            print(
                f"  probe({knob}={value}, attempt={attempt + 1}/{repeats}): "
                f"{'PASS' if passes else 'FAIL'} "
                f"offered_bw={_format_bps(load_info.get('offered_bandwidth_bps'))} "
                f"{_format_topic_rows(rows)}"
            )

        # Median vote: passes if more than half the repeats pass.
        return sum(results) > len(results) // 2

    result = find_capacity(slice_, low, high, probe)
    capacity_load: dict[str, Any] | None = None
    if result.capacity is not None:
        capacity_load_raw: dict[str, Any] = {"rate": rate_hz}
        if knob == "size":
            capacity_load_raw["size_a"] = result.capacity
        elif knob == "rate":
            capacity_load_raw["rate"] = float(result.capacity)
        elif knob == "bandwidth":
            capacity_load_raw["size_a"] = int(result.capacity / (8.0 * rate_hz)) if rate_hz > 0 else result.capacity
        else:
            capacity_load_raw[knob] = result.capacity
        capacity_load = _load_context(capacity_load_raw)
    capacity_result: dict[str, Any] = {
        "slice": {"profile": result.slice.profile, "knob": result.slice.knob, "fixed": result.slice.fixed},
        "capacity": result.capacity,
        "capacity_load": capacity_load,
    }

    # Save budget entry.
    budget_path = out_dir / "budgets.jsonl"
    metrics: dict[str, float] = {}
    if result.capacity is not None:
        metrics[f"capacity_{knob}"] = float(result.capacity)
    budget_entry = BudgetEntry(
        key=BudgetKey(sha=_current_sha(), profile=profile, genre="capacity"),
        metrics=metrics,
    )
    save_budget(budget_path, [budget_entry])
    result_path = _write_benchmark_result(
        out_dir,
        genre="capacity",
        context=result_context,
        configuration={
            "profile": profile,
            "knob": knob,
            "bounds": {"low": low, "high": high},
            "load": _load_context({"rate": rate_hz}),
            "thresholds": {
                "max_loss_pct": max_loss_pct,
                "max_latency_ms": max_latency_ms,
                "latency_quantile": thresholds.latency_quantile,
            },
            "topic": topic or None,
            "repeats": repeats,
            "duration_s": duration_s,
        },
        result=capacity_result,
        measurements={"probes": probe_results},
        verdict={
            "passed": result.capacity is not None,
            "status": "capacity_found" if result.capacity is not None else "no_passing_probe",
        },
        artifacts={"budget": budget_path.name, "stdout": "stdout.txt", "probes_dir": "probes"},
    )
    capacity_result["result_file"] = str(result_path)
    print(f"Capacity result: {knob}={result.capacity} (budget {budget_path}, result {result_path})")
    return capacity_result


# --------------------------------------------------------------------------- #
# Benchmark driver: ramp
# --------------------------------------------------------------------------- #


def drive_ramp(
    run_point: RunPointFn,
    *,
    profile: str,
    values: Sequence[float],
    knob: str = "size",
    rate_hz: float = 20.0,
    topic: str = "",
    duration_s: float = 60.0,
    out_dir: Path,
    result_context: dict[str, Any] | None = None,
) -> list[dict[str, float]]:
    """Drive a linear ramp: measure latency at each load value.

    Returns the response curve as a list of ``{value, metric}`` dicts.
    """
    curve: list[dict[str, float]] = []
    for value in values:
        load: dict[str, Any] = {"rate": rate_hz}
        if knob == "size":
            load["size_a"] = int(value)
        elif knob == "rate":
            load["rate"] = value
        else:
            load[knob] = value

        summary = run_point(profile=profile, load=load, duration_s=duration_s, out_dir=out_dir)
        rows = _topic_rows(summary, topic)
        topics = summary.get("topics", {})
        # Use the first topic or the specified one.
        target_topic = topic if topic else next(iter(topics), "")
        topic_data = topics.get(target_topic, {})
        latency_p95 = (topic_data.get("ota_hop_ms") or {}).get("p95")
        loss_pct = topic_data.get("loss_pct", 100.0)
        load_info = _load_context(load)
        point = {
            "value": float(value),
            "metric": float(latency_p95 or 0.0),
            "loss_pct": float(loss_pct),
            "offered_bw_bps": float(load_info["offered_bandwidth_bps"] or 0.0),
        }
        curve.append(point)
        print(
            f"  ramp({knob}={value}): offered_bw={_format_bps(load_info.get('offered_bandwidth_bps'))} "
            f"{_format_topic_rows(rows)}"
        )

    curve_path = out_dir / "curve.jsonl"
    curve_path.write_text(
        "\n".join(json.dumps(point, sort_keys=True) for point in curve) + "\n",
        encoding="utf-8",
    )
    _write_benchmark_result(
        out_dir,
        genre="ramp",
        context=result_context,
        configuration={
            "profile": profile,
            "knob": knob,
            "values": list(values),
            "load": _load_context({"rate": rate_hz}),
            "topic": topic or None,
            "duration_s": duration_s,
        },
        result=curve,
        measurements={"points": curve},
        verdict={"passed": True, "status": "completed"},
        artifacts={"curve": curve_path.name, "stdout": "stdout.txt", "probes_dir": "probes"},
    )
    print(f"Ramp curve saved to {curve_path}")
    return curve


# --------------------------------------------------------------------------- #
# Benchmark driver: recovery
# --------------------------------------------------------------------------- #


def drive_recovery(
    run_point: RunPointFn,
    *,
    profile: str,
    duration_s: float = 90.0,
    nominal_period_s: float = 0.05,
    latched_topics: Sequence[str] = (),
    out_dir: Path,
    profiles_file: Path | None = None,
    result_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive a recovery test: run under a timeline profile, extract recovery metrics.

    The ``run_point`` for recovery must handle timeline profiles (stepping them
    live). The ``OutageWindow`` is discovered from the timeline.
    """
    from .benchmark import OutageWindow, recovery_metrics
    from .network_profiles import load_profiles_file

    # Load the timeline profile to find the outage window.
    profiles = load_profiles_file(_find_profiles_file(profiles_file))
    profile_obj = profiles.get(profile)
    if profile_obj is None:
        raise ValueError(f"Profile {profile!r} not found in profiles file.")
    if not profile_obj.is_timeline:
        raise ValueError(f"Profile {profile!r} is not a timeline profile; recovery requires a timeline.")

    outage_start: float | None = None
    outage_end: float | None = None
    clock = 0.0
    for segment in profile_obj.timeline:
        if segment.outage is not None:
            outage_start = clock
            outage_end = clock + segment.for_s
        clock += segment.for_s

    if outage_start is None or outage_end is None:
        raise ValueError(f"Profile {profile!r} has no outage segment; recovery needs one.")

    # Run the point (the live probe handles timeline stepping).
    summary = run_point(profile=profile, load={}, duration_s=duration_s, out_dir=out_dir)

    # Collect raw records for recovery analysis.
    records = _load_raw_records_from_out(out_dir)

    metrics = recovery_metrics(
        records,
        OutageWindow(start=outage_start, end=outage_end),
        nominal_period_s=nominal_period_s,
        latched_topics=latched_topics,
    )
    result = {
        "t_recover": metrics.t_recover,
        "t_steady": metrics.t_steady,
        "recovery_burst": metrics.recovery_burst,
        "lost_during_outage": metrics.lost_during_outage,
        "latched_rearrival": metrics.latched_rearrival,
        "outage": {"start": outage_start, "end": outage_end},
    }

    rec_path = out_dir / "recovery.json"
    rec_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_benchmark_result(
        out_dir,
        genre="recovery",
        context=result_context,
        configuration={
            "profile": profile,
            "duration_s": duration_s,
            "nominal_period_s": nominal_period_s,
            "latched_topics": list(latched_topics),
            "outage": {"start": outage_start, "end": outage_end},
        },
        result=result,
        measurements={"summary": summary, "raw_record_count": len(records)},
        verdict={"passed": True, "status": "completed"},
        artifacts={"recovery": rec_path.name, "stdout": "stdout.txt", "probes_dir": "probes"},
    )
    print(f"Recovery metrics saved to {rec_path}")
    return result


def _load_raw_records_from_out(out_dir: Path) -> list[dict[str, Any]]:
    """Load raw transit records from the most recent run's events files."""
    from .transit import join_transit_records, load_transit_records

    events = sorted(out_dir.rglob("status/events.jsonl"))
    if not events:
        events = sorted(out_dir.rglob("events.jsonl"))
    if not events:
        return []
    return join_transit_records(load_transit_records(events))


# --------------------------------------------------------------------------- #
# Benchmark driver: sweep
# --------------------------------------------------------------------------- #


def drive_sweep(
    run_point: RunPointFn,
    *,
    profile_grid: Sequence[str],
    max_loss_pct: float = 5.0,
    max_latency_ms: float = 300.0,
    rate_hz: float = 20.0,
    size: int = 60000,
    topic: str = "",
    duration_s: float = 60.0,
    out_dir: Path,
    result_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Drive a profile-grid sweep (tests 1.1/2.1): run one point per profile,
    report oracle pass/fail for each.

    Returns the frontier as a list of per-profile result dicts.
    """
    from .benchmark import OracleThresholds, oracle_passes_topic

    thresholds = OracleThresholds(max_loss_pct=max_loss_pct, max_latency_ms=max_latency_ms)
    frontier: list[dict[str, Any]] = []
    point_measurements: list[dict[str, Any]] = []

    for profile in profile_grid:
        load = {"size_a": size, "rate": rate_hz}
        summary = run_point(profile=profile, load=load, duration_s=duration_s, out_dir=out_dir)
        rows = _topic_rows(summary, topic)
        topics = summary.get("topics", {})
        target_topic = topic if topic else next(iter(topics), "")
        topic_data = topics.get(target_topic, {})
        passes = oracle_passes_topic(topic_data, thresholds)
        loss_pct = topic_data.get("loss_pct", 100.0)
        latency_p95 = (topic_data.get("ota_hop_ms") or {}).get("p95")
        result = {
            "profile": profile,
            "passes": passes,
            "loss_pct": float(loss_pct),
            "latency_p95_ms": float(latency_p95) if latency_p95 is not None else None,
        }
        frontier.append(result)
        load_info = _load_context(load)
        point_measurements.append(
            {
                "profile": profile,
                "passed": passes,
                "load": load_info,
                "topics": rows,
                "summary": summary,
            }
        )
        print(
            f"  sweep({profile}): {'PASS' if passes else 'FAIL'} "
            f"offered_bw={_format_bps(load_info.get('offered_bandwidth_bps'))} {_format_topic_rows(rows)}"
        )

    frontier_path = out_dir / "frontier.jsonl"
    frontier_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in frontier) + "\n",
        encoding="utf-8",
    )
    _write_benchmark_result(
        out_dir,
        genre="sweep",
        context=result_context,
        configuration={
            "profile_grid": list(profile_grid),
            "load": _load_context({"size_a": size, "rate": rate_hz}),
            "thresholds": {
                "max_loss_pct": max_loss_pct,
                "max_latency_ms": max_latency_ms,
                "latency_quantile": thresholds.latency_quantile,
            },
            "topic": topic or None,
            "duration_s": duration_s,
        },
        result=frontier,
        measurements={"points": point_measurements},
        verdict={
            "passed": all(row["passes"] for row in frontier),
            "status": "all_profiles_passed" if all(row["passes"] for row in frontier) else "profile_failures",
        },
        artifacts={"frontier": frontier_path.name, "stdout": "stdout.txt", "probes_dir": "probes"},
    )
    print(f"Sweep frontier saved to {frontier_path}")
    return frontier


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _current_sha() -> str:
    """Best-effort current git SHA for budget keys."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _setup_benchmark_run_dir(args: argparse.Namespace, genre: str, profile: str | None) -> Path:
    """Setup a unique run directory under benchmarks_dir and write command/metadata/configs."""
    from datetime import datetime

    from . import __version__
    from .cli import _load_runtime_config, _resolve_project_config_source

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir
    if not artifacts_dir:
        artifacts_dir = Path.cwd() / "artifacts" / "benchmarks"

    now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    profile_part = f"_{profile}" if profile else ""
    run_dir = artifacts_dir / f"{genre}{profile_part}_{now_str}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write command.txt
    command_line = " ".join(shlex.quote(arg) for arg in sys.argv)
    (run_dir / "command.txt").write_text(command_line + "\n", encoding="utf-8")

    # 2. Write metadata.json
    safe_args = {}
    for k, v in vars(args).items():
        if k.startswith("_"):
            continue
        try:
            json.dumps(v)
            safe_args[k] = v
        except Exception:
            safe_args[k] = str(v)

    metadata = {
        "timestamp": datetime.now().isoformat(),
        "rosotacom_version": __version__,
        "genre": genre,
        "profile": profile,
        "args": safe_args,
        "rosotacom_sha": _current_sha(),
    }
    (run_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # 3. Copy config files if present
    # rosotacom.yaml
    rosotacom_config_raw, _ = _resolve_project_config_source(args)
    if rosotacom_config_raw:
        config_path = Path(rosotacom_config_raw).resolve()
        if config_path.is_file():
            shutil.copy2(config_path, run_dir / "rosotacom.yaml")

    # profiles file
    if runtime.profiles_file:
        profiles_path = Path(runtime.profiles_file).resolve()
        if profiles_path.is_file():
            shutil.copy2(profiles_path, run_dir / "profiles.yaml")

    # ros2docker.json
    if runtime.ros2docker_config:
        ros2docker_path = Path(runtime.ros2docker_config).resolve()
        if ros2docker_path.is_file():
            shutil.copy2(ros2docker_path, run_dir / "ros2docker.json")

    return run_dir


def _find_profiles_file(profiles_file: Path | None = None) -> Path:
    """Locate the profiles file from the project config."""
    if profiles_file is not None and profiles_file.is_file():
        return profiles_file
    # Walk up from the package to find profiles.yaml or rosotacom.yaml.
    for candidate in (
        Path.cwd() / "profiles.yaml",
        Path.cwd() / "profiles" / "benchmark-profiles.yaml",
        Path(__file__).parent / "resources" / "examples" / "profiles.yaml",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("No profiles file found. Provide one via --profiles-file or 'profiles:' in rosotacom.yaml.")


def _parse_values(raw: str) -> list[float]:
    """Parse a comma-separated or range-based value list.

    Supports: ``"1000,2000,4000"`` or ``"1000:10000:1000"`` (start:stop:step)
    or ``"1000..10000/10"`` (start..stop/count, linearly spaced).
    """
    raw = raw.strip()
    if ".." in raw and "/" in raw:
        range_part, count_str = raw.split("/", 1)
        start_str, end_str = range_part.split("..", 1)
        start, end, count = float(start_str), float(end_str), int(count_str)
        if count <= 1:
            return [start]
        step = (end - start) / (count - 1)
        return [start + i * step for i in range(count)]
    if ":" in raw:
        parts = raw.split(":")
        start, stop = float(parts[0]), float(parts[1])
        step = float(parts[2]) if len(parts) > 2 else 1.0
        values: list[float] = []
        v = start
        while v <= stop + 1e-9:
            values.append(v)
            v += step
        return values
    return [float(v.strip()) for v in raw.split(",") if v.strip()]


# --------------------------------------------------------------------------- #
# CLI subcommand handlers
# --------------------------------------------------------------------------- #


def _add_benchmark_common_args(parser: argparse.ArgumentParser) -> None:
    from .cli import _add_common_config_args, _add_peer_address_arg, _add_peer_arg, _add_peer_ssh_arg

    _add_common_config_args(parser)
    _add_peer_arg(parser)
    _add_peer_ssh_arg(parser)
    _add_peer_address_arg(parser)
    parser.add_argument("--skip-preflight", action="store_true", help="Skip SSH/Docker readiness checks.")
    parser.add_argument("--keep-workdir", action="store_true", help="Keep temporary checkout directories.")
    parser.add_argument("--dry-run", action="store_true", help="Show commands but do not run them.")
    parser.add_argument("--artifacts-dir", help="Output directory for all benchmark artifacts.")
    parser.add_argument(
        "--rmw",
        default=DEFAULT_BENCHMARK_RMW,
        help=f"RMW implementation or rosotacom RMW alias for benchmark sessions (default: {DEFAULT_BENCHMARK_RMW}).",
    )


def _make_live_run_point(args: argparse.Namespace, session_name: str) -> RunPointFn:
    """Create a live run_point probe from the parsed CLI context."""
    import shlex
    import subprocess
    from dataclasses import replace

    from .cli import (
        _PROFILE_SAFETY_MAX_S,
        _effective_session_config,
        _ensure_smoke_network,
        _load_runtime_config,
        _new_instance_id,
        _noninteractive_smoke_network_config,
        _ota_arm_profile,
        _ota_cleanup_hosts,
        _ota_collect_logs,
        _ota_preflight,
        _ota_prepare_hosts,
        _ota_resolve_interfaces,
        _ota_start_peers,
        _ota_start_session_publishers,
        _ota_stop_peers,
        _ota_teardown_profile,
        _ota_write_manifest,
        _ota_write_state,
        _peer_command_runner,
        _peer_watchdog_launcher,
        _profile_directions,
        _remove_smoke_network,
        _resolve_ota_profile,
        _resolve_ota_smoke_context,
        _resolve_session,
        _resolve_session_instance,
        _smoke_peer_address_args,
        _smoke_ros_setup,
        _stop_container_name,
        _write_docker_log,
        start_session,
    )
    from .network_profiles import expand_timeline
    from .network_shaper import ProfileShaper

    is_ota = bool(getattr(args, "deployment", None))

    def run_point(
        *,
        profile: str | None,
        load: dict[str, Any],
        duration_s: float,
        out_dir: Path,
    ) -> dict[str, Any]:
        if is_ota:
            # --- Real-near (ota-smoke) path ---
            target_name = "remote_assist"
            # Default project to fzi_projects/remote_assist/rosotacom.yaml if not set.
            rosotacom_config = getattr(args, "rosotacom_config", None)
            if not rosotacom_config:
                candidate = Path.cwd() / "fzi_projects" / "remote_assist" / "rosotacom.yaml"
                if candidate.is_file():
                    args.rosotacom_config = str(candidate)

            smoke_args = argparse.Namespace(
                rosotacom_config=getattr(args, "rosotacom_config", None),
                ros2docker_config=getattr(args, "ros2docker_config", None),
                session_configs_dir=getattr(args, "session_configs_dir", None),
                scenario_configs_dir=getattr(args, "scenario_configs_dir", None),
                session_instances_dir=getattr(args, "session_instances_dir", None),
                deployment=getattr(args, "deployment", None),
                profiles_file=getattr(args, "profiles_file", None),
                target=target_name,
                target_type="auto",
                peer=getattr(args, "peer", None),
                peer_address=getattr(args, "peer_address", None),
                skip_preflight=getattr(args, "skip_preflight", False),
                keep_workdir=getattr(args, "keep_workdir", False),
                dry_run=getattr(args, "dry_run", False),
                instance_id=getattr(args, "instance_id", None),
                profile=profile,
                benchmark_stepping=True,
            )

            runtime, plan, target = _resolve_ota_smoke_context(smoke_args)
            dry_run = smoke_args.dry_run

            if not smoke_args.skip_preflight:
                _ota_preflight(
                    plan,
                    require_tmux=target.target_type == "scenario",
                    check_peer_reachability=False,
                    dry_run=dry_run,
                )
            _ota_prepare_hosts(smoke_args, runtime, plan)
            instance = _resolve_session_instance(
                runtime,
                target.session,
                smoke_args.instance_id or _new_instance_id(),
            )
            if dry_run:
                plan = replace(plan, state_path=instance.host_dir / "ota-deployment.yaml")
            else:
                plan = _ota_write_state(instance, plan)
            _ota_write_manifest(
                instance,
                target,
                runtime,
                plan,
                tmux_session=None,
                interactive=False,
                phase="running",
            )
            profile_name, profile_obj = _resolve_ota_profile(runtime, target, smoke_args)
            directions = _profile_directions(plan, target.cfg) if profile_obj is not None else {}
            shapers = []
            try:
                # Arm the profile (or start stepping if timeline)
                if profile_obj is not None:
                    if profile_obj.is_timeline:
                        peer_names = sorted(plan.peers)
                        peer_steps = {}
                        for peer_name in peer_names:
                            peer = plan.peers[peer_name]
                            direction = directions.get(peer_name, "uplink")
                            other_addr = next(plan.peers[name].address for name in peer_names if name != peer_name)
                            if dry_run:
                                ota_iface, control_iface = "<ota-if>", None
                            else:
                                ota_iface, control_iface = _ota_resolve_interfaces(peer, other_addr, dry_run=False)
                            shaper = ProfileShaper(
                                ota_iface,
                                _peer_command_runner(peer, dry_run=dry_run),
                                control_interface=control_iface,
                                safety_max_duration_s=_PROFILE_SAFETY_MAX_S,
                                watchdog_launcher=_peer_watchdog_launcher(peer, dry_run=dry_run),
                            )
                            shapers.append(shaper)
                            steps = expand_timeline(profile_obj, ota_iface, direction=direction)
                            peer_steps[peer_name] = (shaper, steps)

                        for shaper in shapers:
                            shaper.arm([])
                    else:
                        shapers = _ota_arm_profile(plan, profile_obj, directions, dry_run=dry_run)

                _ota_start_peers(target, plan, instance.instance_id, dry_run=dry_run)
                if not dry_run:
                    time.sleep(12)
                _ota_start_session_publishers(target, plan, dry_run=dry_run)

                if not dry_run:
                    if profile_obj is not None and profile_obj.is_timeline:
                        num_steps = len(profile_obj.timeline)
                        for i in range(num_steps):
                            step_duration = profile_obj.timeline[i].for_s
                            for peer_name in peer_names:
                                shaper, steps = peer_steps[peer_name]
                                step = steps[i]
                                print(f"Timeline step {i}: arming commands for {peer_name}:{shaper.interface}")
                                shaper.apply(step.commands)
                            time.sleep(step_duration)
                    else:
                        time.sleep(duration_s)
            finally:
                _ota_teardown_profile(shapers)
                _ota_collect_logs(instance, plan, dry_run=dry_run)
                if not getattr(smoke_args, "keep_running", False):
                    _ota_stop_peers(target, plan, instance.instance_id, dry_run=dry_run)
                    _ota_write_manifest(
                        instance,
                        target,
                        runtime,
                        plan,
                        tmux_session=None,
                        interactive=False,
                        phase="stopped",
                    )
                    if not getattr(smoke_args, "keep_workdir", False):
                        _ota_cleanup_hosts(plan, dry_run=dry_run)
            if dry_run:
                return {
                    "topics": {
                        "/bench_capacity": {
                            "expected": 100,
                            "delivered": 100,
                            "lost": 0,
                            "loss_pct": 0.0,
                            "reordered": 0,
                            "ota_hop_ms": {"p50": 50.0, "p95": 100.0},
                            "jitter_ms": {"p50": 1.0, "p95": 3.0},
                        }
                    }
                }
            if not dry_run:
                probe_parts = ["probe", instance.instance_id]
                for k, v in sorted(load.items()):
                    probe_parts.append(f"{k}_{v}")
                probe_name = "_".join(probe_parts)
                dest = out_dir / "probes" / probe_name
                shutil.copytree(instance.host_dir, dest, dirs_exist_ok=True)
            return collect_transit_summary(instance.host_dir)

        else:
            # --- Lab (smoke) path ---
            if getattr(args, "dry_run", False):
                return {
                    "topics": {
                        "/bench_capacity": {
                            "expected": 100,
                            "delivered": 100,
                            "lost": 0,
                            "loss_pct": 0.0,
                            "reordered": 0,
                            "ota_hop_ms": {"p50": 50.0, "p95": 100.0},
                            "jitter_ms": {"p50": 1.0, "p95": 3.0},
                        }
                    }
                }

            runtime = _load_runtime_config(args)
            session = _resolve_session(session_name, runtime)
            instance_id = getattr(args, "instance_id", None) or _new_instance_id()
            smoke_instance = _resolve_session_instance(runtime, session, instance_id)
            smoke_network = _noninteractive_smoke_network_config(runtime, session, smoke_instance.instance_id)

            peer_address_args = _smoke_peer_address_args(smoke_network.peer_ips)
            cfg = _effective_session_config(session.host_dir, runtime)
            common = {
                "rosotacom_config": args.rosotacom_config,
                "ros2docker_config": args.ros2docker_config,
                "session_configs_dir": args.session_configs_dir,
                "session_instances_dir": getattr(args, "session_instances_dir", None),
                "deployment": args.deployment,
                "session_dir": session_name,
                "mode": "detached",
                "force": True,
                "rewrite_formatting": False,
                "peer": [],
                "peer_address": peer_address_args,
                "instance_id": smoke_instance.instance_id,
                "network_name": smoke_network.name,
            }

            from .network_profiles import load_profiles_file, shaping_commands

            profile_name = getattr(args, "profile", None)
            profile_obj = None
            if profile_name and runtime.profiles_file:
                try:
                    profiles = load_profiles_file(runtime.profiles_file)
                    profile_obj = profiles.get(profile_name)
                except Exception as exc:
                    print(f"Warning: Failed to load profile {profile_name!r}: {exc}", file=sys.stderr)

            def make_container_runner(container_name: str) -> Callable[[Sequence[str]], None]:
                def run(argv: Sequence[str]) -> None:
                    # Execute tc command inside container namespace as root.
                    cmd = ["docker", "exec", "-u", "root", container_name] + list(argv)
                    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                    if res.returncode != 0:
                        print(
                            f"Warning: Command failed in {container_name}: "
                            f"{' '.join(cmd)} -> {res.stderr or res.stdout}",
                            file=sys.stderr,
                        )

                return run

            ros_setup_a = _smoke_ros_setup(smoke_instance.config_container_dir, cfg, "a")
            rate = load.get("rate", 20.0)
            size = load.get("size") or load.get("size_a") or 66000
            streams = load.get("streams", 1)

            topics_a = cfg.get("topics", {}).get("a_to_b", [])
            pub_cmds = []
            for topic_spec in topics_a:
                topic_name = topic_spec.get("topic")
                cmd = (
                    f"{ros_setup_a} && timeout 300 ros2 run com_py sized_publisher --ros-args "
                    f"-p topic:={shlex.quote(topic_name)} -p size:={size} -p rate:={rate} -p streams:={streams} "
                    f'> "${{ROSOTACOM_LOGS_DIR}}/a/sized_publisher.log" 2>&1'
                )
                pub_cmds.append(cmd)

            a_container = None
            b_container = None
            shapers = []
            peer_steps = {}

            try:
                _ensure_smoke_network(smoke_network.name, smoke_network.subnet)
                a_container = start_session(
                    argparse.Namespace(
                        **common,
                        identity="a",
                        auto_identity=True,
                        network_ip=smoke_network.peer_ips["a"],
                    )
                )
                b_container = start_session(
                    argparse.Namespace(
                        **common,
                        identity="b",
                        auto_identity=True,
                        network_ip=smoke_network.peer_ips["b"],
                    )
                )
                if not getattr(args, "dry_run", False):
                    time.sleep(12)

                # 1. Start the publishers
                for cmd in pub_cmds:
                    subprocess.run(
                        ["docker", "exec", "-d", a_container, "bash", "-c", cmd],
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                # 2. Wait for discovery to complete on unshaped network
                if not getattr(args, "dry_run", False):
                    topic_name = topics_a[0].get("topic") if topics_a else "/bench_capacity"
                    # A. Wait for local subscription discovery
                    info_cmd = f"{ros_setup_a} && ros2 topic info {shlex.quote(topic_name)}"
                    for _ in range(30):
                        res = subprocess.run(
                            ["docker", "exec", a_container, "bash", "-c", info_cmd],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        output = res.stdout or ""
                        if "Subscription count: 0" not in output and "Subscription count:" in output:
                            break
                        time.sleep(0.5)

                    # B. Wait for end-to-end message delivery to Peer B (status.json reports messages_total > 0)
                    status_json_path = smoke_instance.host_dir / "logs" / "b" / "status" / "status.json"
                    for _ in range(60):
                        if status_json_path.is_file():
                            try:
                                status_data = json.loads(status_json_path.read_text(encoding="utf-8"))
                                found_msg = False
                                for topic_info in status_data.get("topics", []):
                                    if topic_info.get("base") == topic_name:
                                        if any(
                                            stage.get("messages_total", 0) > 0 for stage in topic_info.get("stages", [])
                                        ):
                                            found_msg = True
                                            break
                                if found_msg:
                                    break
                            except Exception:
                                pass
                        time.sleep(0.5)
                    time.sleep(3.0)

                # 3. Arm the network shapers (after discovery is complete)
                if profile_obj is not None:
                    if profile_obj.is_timeline:
                        # Peer A shapes uplink (egress to B)
                        shaper_a = ProfileShaper("eth0", make_container_runner(a_container))
                        shapers.append(shaper_a)
                        steps_a = expand_timeline(profile_obj, "eth0", direction="uplink")
                        peer_steps["a"] = (shaper_a, steps_a)

                        # Peer B shapes downlink (egress to A)
                        shaper_b = ProfileShaper("eth0", make_container_runner(b_container))
                        shapers.append(shaper_b)
                        steps_b = expand_timeline(profile_obj, "eth0", direction="downlink")
                        peer_steps["b"] = (shaper_b, steps_b)

                        for shaper in shapers:
                            shaper.arm([])
                    else:
                        if profile_obj.uplink and not profile_obj.uplink.is_empty:
                            shaper_a = ProfileShaper("eth0", make_container_runner(a_container))
                            shapers.append(shaper_a)
                            shaper_a.arm(shaping_commands("eth0", profile_obj.uplink))
                        if profile_obj.downlink and not profile_obj.downlink.is_empty:
                            shaper_b = ProfileShaper("eth0", make_container_runner(b_container))
                            shapers.append(shaper_b)
                            shaper_b.arm(shaping_commands("eth0", profile_obj.downlink))

                # 4. Verify shaping was applied (diagnostic)
                if profile_obj is not None and not getattr(args, "dry_run", False):
                    for label, container in [("A (uplink)", a_container), ("B (downlink)", b_container)]:
                        res = subprocess.run(
                            ["docker", "exec", "-u", "root", container, "tc", "qdisc", "show", "dev", "eth0"],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        print(f"  qdisc on {label} ({container}): {(res.stdout or '').strip()}")

                # 5. Measure traffic under shaped conditions
                if profile_obj is not None and profile_obj.is_timeline:
                    num_steps = len(profile_obj.timeline)
                    for i in range(num_steps):
                        step_duration = profile_obj.timeline[i].for_s
                        for shaper, steps in peer_steps.values():
                            step = steps[i]
                            shaper.apply(step.commands)
                        time.sleep(step_duration)
                else:
                    time.sleep(duration_s)
            finally:
                for shaper in shapers:
                    shaper.teardown()

                for cleanup_container in [a_container, b_container]:
                    if cleanup_container:
                        subprocess.run(
                            ["docker", "exec", cleanup_container, "pkill", "-f", "sized_publisher"],
                            capture_output=True,
                            check=False,
                        )
                        _write_docker_log(
                            cleanup_container,
                            smoke_instance,
                            "a" if cleanup_container == a_container else "b",
                        )
                        _stop_container_name(cleanup_container, runtime)
                _remove_smoke_network(smoke_network.name)

            probe_parts = ["probe", smoke_instance.instance_id]
            for k, v in sorted(load.items()):
                probe_parts.append(f"{k}_{v}")
            probe_name = "_".join(probe_parts)
            dest = out_dir / "probes" / probe_name
            shutil.copytree(smoke_instance.host_dir, dest, dirs_exist_ok=True)

            return collect_transit_summary(smoke_instance.host_dir)

    return run_point


def benchmark_capacity(args: argparse.Namespace) -> int:
    """Handler for ``rosotacom benchmark capacity``."""
    from .cli import _load_runtime_config

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir

    run_dir = _setup_benchmark_run_dir(args, "capacity", args.profile)
    session_context = _prepare_benchmark_session_config(args, "bench_1_1_capacity", run_dir)
    result_context = _benchmark_result_context(
        args,
        genre="capacity",
        profile=args.profile,
        run_dir=run_dir,
        session=session_context,
    )

    # Use stub probe under test, or live probe in production.
    run_point = getattr(args, "_test_run_point", None) or _make_live_run_point(args, "bench_1_1_capacity")

    with log_stdout_stderr_to_file(run_dir / "stdout.txt"):
        result = drive_capacity(
            run_point,
            profile=args.profile,
            knob=args.knob,
            low=args.low,
            high=args.high,
            max_loss_pct=args.max_loss,
            max_latency_ms=args.max_latency_ms,
            rate_hz=getattr(args, "rate_hz", 20.0),
            topic=getattr(args, "topic", ""),
            repeats=getattr(args, "repeats", 1),
            duration_s=getattr(args, "duration", 60.0),
            out_dir=run_dir,
            result_context=result_context,
        )

    # Resolve aggregated file output path
    out_file_name = getattr(args, "out", "budgets.jsonl")
    if artifacts_dir:
        out_path = artifacts_dir / Path(out_file_name).name
    else:
        out_path = Path(out_file_name)

    run_budgets_file = run_dir / "budgets.jsonl"
    if run_budgets_file.is_file():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "a", encoding="utf-8") as f_out:
            f_out.write(run_budgets_file.read_text(encoding="utf-8"))
        print(f"Aggregated budget result appended to {out_path}")

    print(f"Capacity: {result['slice']['knob']}={result['capacity']} → {run_dir / BENCHMARK_RESULT_FILE}")
    return 0


def benchmark_ramp(args: argparse.Namespace) -> int:
    """Handler for ``rosotacom benchmark ramp``."""
    from .cli import _load_runtime_config

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir

    run_dir = _setup_benchmark_run_dir(args, "ramp", args.profile)
    session_context = _prepare_benchmark_session_config(args, "bench_1_3_ramp", run_dir)
    result_context = _benchmark_result_context(
        args,
        genre="ramp",
        profile=args.profile,
        run_dir=run_dir,
        session=session_context,
    )

    run_point = getattr(args, "_test_run_point", None) or _make_live_run_point(args, "bench_1_3_ramp")

    values = _parse_values(args.values)
    with log_stdout_stderr_to_file(run_dir / "stdout.txt"):
        drive_ramp(
            run_point,
            profile=args.profile,
            values=values,
            knob=getattr(args, "knob", "size"),
            rate_hz=getattr(args, "rate_hz", 20.0),
            topic=getattr(args, "topic", ""),
            duration_s=getattr(args, "duration", 60.0),
            out_dir=run_dir,
            result_context=result_context,
        )

    # Resolve aggregated file output path
    out_file_name = getattr(args, "out", "curve.jsonl")
    if artifacts_dir:
        out_path = artifacts_dir / Path(out_file_name).name
    else:
        out_path = Path(out_file_name)

    run_curve_file = run_dir / "curve.jsonl"
    if run_curve_file.is_file():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_curve_file, out_path)
        print(f"Ramp curve copied to {out_path}")

    return 0


def benchmark_recovery(args: argparse.Namespace) -> int:
    """Handler for ``rosotacom benchmark recovery``."""
    from .cli import _load_runtime_config

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir

    run_dir = _setup_benchmark_run_dir(args, "recovery", args.profile)
    session_context = _prepare_benchmark_session_config(args, "bench_1_4_recovery", run_dir)
    result_context = _benchmark_result_context(
        args,
        genre="recovery",
        profile=args.profile,
        run_dir=run_dir,
        session=session_context,
    )

    run_point = getattr(args, "_test_run_point", None) or _make_live_run_point(args, "bench_1_4_recovery")

    with log_stdout_stderr_to_file(run_dir / "stdout.txt"):
        drive_recovery(
            run_point,
            profile=args.profile,
            duration_s=getattr(args, "duration", 90.0),
            nominal_period_s=getattr(args, "nominal_period", 0.05),
            latched_topics=getattr(args, "latched_topics", "").split(",")
            if getattr(args, "latched_topics", "")
            else (),
            out_dir=run_dir,
            profiles_file=runtime.profiles_file,
            result_context=result_context,
        )

    # Resolve aggregated file output path
    out_file_name = getattr(args, "out", "recovery.json")
    if artifacts_dir:
        out_path = artifacts_dir / Path(out_file_name).name
    else:
        out_path = Path(out_file_name)

    run_rec_file = run_dir / "recovery.json"
    if run_rec_file.is_file():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_rec_file, out_path)
        print(f"Recovery metrics copied to {out_path}")

    return 0


def benchmark_sweep(args: argparse.Namespace) -> int:
    """Handler for ``rosotacom benchmark sweep``."""
    from .cli import _load_runtime_config

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir

    run_dir = _setup_benchmark_run_dir(args, "sweep", None)
    session_context = _prepare_benchmark_session_config(args, "bench_1_2_load_sweep", run_dir)
    result_context = _benchmark_result_context(
        args,
        genre="sweep",
        profile=None,
        run_dir=run_dir,
        session=session_context,
    )

    run_point = getattr(args, "_test_run_point", None) or _make_live_run_point(args, "bench_1_2_load_sweep")

    profile_grid = [p.strip() for p in args.profile_grid.split(",") if p.strip()]
    with log_stdout_stderr_to_file(run_dir / "stdout.txt"):
        drive_sweep(
            run_point,
            profile_grid=profile_grid,
            max_loss_pct=getattr(args, "max_loss", 5.0),
            max_latency_ms=getattr(args, "max_latency_ms", 300.0),
            rate_hz=getattr(args, "rate_hz", 20.0),
            size=getattr(args, "size", 60000),
            topic=getattr(args, "topic", ""),
            duration_s=getattr(args, "duration", 60.0),
            out_dir=run_dir,
            result_context=result_context,
        )

    # Resolve aggregated file output path
    out_file_name = getattr(args, "out", "frontier.jsonl")
    if artifacts_dir:
        out_path = artifacts_dir / Path(out_file_name).name
    else:
        out_path = Path(out_file_name)

    run_frontier_file = run_dir / "frontier.jsonl"
    if run_frontier_file.is_file():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_frontier_file, out_path)
        print(f"Sweep frontier copied to {out_path}")

    return 0


def benchmark_plot(args: argparse.Namespace) -> int:
    """Handler for ``rosotacom benchmark plot``."""
    from .cli import _load_runtime_config
    from .plots import (
        plot_capacity_frontier,
        plot_offered_bw,
        plot_ramp,
        plot_recovery_timeline,
        plot_topic_heatmap,
    )

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir
    input_path_raw = getattr(args, "input", None)

    if not input_path_raw and not artifacts_dir:
        print("Error: either 'input' or '--artifacts-dir' must be specified.", file=sys.stderr)
        return 1

    inputs_to_plot: list[Path] = []
    if input_path_raw:
        inputs_to_plot.append(Path(input_path_raw))
    elif artifacts_dir:
        # Search for standard files. Exclude budgets.jsonl since it has no plot format
        for name in ("curve.jsonl", "recovery.json", "frontier.jsonl"):
            candidate = artifacts_dir / name
            if candidate.is_file():
                inputs_to_plot.append(candidate)

    if not inputs_to_plot:
        print("No input files to plot found.", file=sys.stderr)
        return 0

    for input_path in inputs_to_plot:
        if not input_path.exists():
            print(f"Input file not found: {input_path}", file=sys.stderr)
            continue

        out = Path(args.out) if getattr(args, "out", None) else input_path.with_suffix(".png")
        plot_type = getattr(args, "type", "auto")

        try:
            data = _load_jsonl(input_path)
            if plot_type == "auto":
                plot_type = _infer_plot_type(data, input_path.name)

            if plot_type == "frontier":
                plot_capacity_frontier(data, out=out)
            elif plot_type == "offered_bw":
                plot_offered_bw(data, out=out)
            elif plot_type == "ramp":
                plot_ramp(data, out=out)
            elif plot_type == "recovery":
                outage_start = float(data[0].get("outage", {}).get("start", 0)) if data else 0.0
                outage_end = float(data[0].get("outage", {}).get("end", 0)) if data else 0.0
                records = _load_raw_records_from_out(input_path.parent)
                plot_recovery_timeline(records, outage_start, outage_end, out=out)
            elif plot_type == "heatmap":
                per_topic = {row.get("topic", ""): row for row in data if "topic" in row}
                plot_topic_heatmap(per_topic, out=out)
            else:
                print(
                    f"Unknown plot type: {plot_type!r}. Use: frontier, offered_bw, ramp, recovery, heatmap.",
                    file=sys.stderr,
                )
                return 1

            print(f"Plot written to {out}")
        except Exception as exc:
            print(f"Failed to plot {input_path.name}: {exc}", file=sys.stderr)

    return 0


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a JSON-lines file into a list of dicts."""
    text_stripped = path.read_text(encoding="utf-8").strip()
    if text_stripped.startswith("["):
        return json.loads(text_stripped)  # type: ignore[no-any-return]
    try:
        return [json.loads(text_stripped)]
    except json.JSONDecodeError:
        data: list[dict[str, Any]] = []
        for line in text_stripped.splitlines():
            if line.strip():
                data.append(json.loads(line))
        return data


def _infer_plot_type(data: list[dict[str, Any]], filename: str) -> str:
    """Best-effort inference of the plot type from the data shape and filename."""
    if "frontier" in filename:
        return "frontier"
    if "curve" in filename:
        return "ramp"
    if "recovery" in filename:
        return "recovery"
    if not data:
        return "frontier"
    first = data[0]
    if "genre" in first and "metrics" in first:
        raise ValueError("Budget files (e.g. budgets.jsonl) contain target thresholds and cannot be plotted directly.")
    if "capacity" in first or "bandwidth_bps" in first:
        return "frontier"
    if "value" in first and "metric" in first:
        return "ramp"
    if "t_recover" in first:
        return "recovery"
    if "offered_bw_bps" in first:
        return "offered_bw"
    return "frontier"


# --------------------------------------------------------------------------- #
# Argparse registration (called from cli.py)
# --------------------------------------------------------------------------- #


def register_benchmark_parser(subparsers: Any) -> None:
    """Register the ``benchmark`` subcommand and its sub-subcommands."""
    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Run benchmark tests (capacity, ramp, recovery, sweep) and render plots.",
    )
    benchmark_subparsers = benchmark_parser.add_subparsers(dest="benchmark_command", required=True)

    # --- capacity ---
    cap_parser = benchmark_subparsers.add_parser(
        "capacity",
        help="Binary-search for the capacity breakpoint under a profile.",
    )
    _add_benchmark_common_args(cap_parser)
    cap_parser.add_argument("--profile", required=True, help="Network profile name.")
    cap_parser.add_argument("--knob", required=True, choices=["size", "rate", "bandwidth"], help="Parameter to sweep.")
    cap_parser.add_argument("--low", type=int, required=True, help="Lower bound for the sweep.")
    cap_parser.add_argument("--high", type=int, required=True, help="Upper bound for the sweep.")
    cap_parser.add_argument("--max-loss", type=float, required=True, help="Oracle: max acceptable loss %%.")
    cap_parser.add_argument(
        "--max-latency-ms", type=float, required=True, help="Oracle: max acceptable p95 latency (ms)."
    )
    cap_parser.add_argument("--repeats", type=int, default=1, help="Repeats per probe point (median vote).")
    cap_parser.add_argument("--rate-hz", type=float, default=20.0, help="Publish rate (Hz).")
    cap_parser.add_argument("--topic", default="", help="Topic to evaluate (default: all).")
    cap_parser.add_argument("--duration", type=float, default=60.0, help="Seconds per probe point.")
    cap_parser.add_argument("--out", default="budgets.jsonl", help="Output budget JSONL file.")
    cap_parser.set_defaults(func=benchmark_capacity)

    # --- ramp ---
    ramp_parser = benchmark_subparsers.add_parser(
        "ramp",
        help="Measure latency over a linear load ramp (monitor-only trend).",
    )
    _add_benchmark_common_args(ramp_parser)
    ramp_parser.add_argument("--profile", required=True, help="Network profile name.")
    ramp_parser.add_argument(
        "--values", required=True, help="Comma-separated values, start:stop:step, or start..stop/count."
    )
    ramp_parser.add_argument("--knob", default="size", help="Parameter to ramp (default: size).")
    ramp_parser.add_argument("--rate-hz", type=float, default=20.0, help="Publish rate (Hz).")
    ramp_parser.add_argument("--topic", default="", help="Topic to evaluate.")
    ramp_parser.add_argument("--duration", type=float, default=60.0, help="Seconds per ramp point.")
    ramp_parser.add_argument("--out", default="curve.jsonl", help="Output curve JSONL file.")
    ramp_parser.set_defaults(func=benchmark_ramp)

    # --- recovery ---
    rec_parser = benchmark_subparsers.add_parser(
        "recovery",
        help="Run a recovery test under a timeline profile.",
    )
    _add_benchmark_common_args(rec_parser)
    rec_parser.add_argument("--profile", required=True, help="Timeline profile name (must have an outage segment).")
    rec_parser.add_argument("--duration", type=float, default=90.0, help="Total run duration (s).")
    rec_parser.add_argument("--nominal-period", type=float, default=0.05, help="Nominal publish period (s).")
    rec_parser.add_argument("--latched-topics", default="", help="Comma-separated latched topic names.")
    rec_parser.add_argument("--out", default="recovery.json", help="Output recovery metrics file.")
    rec_parser.set_defaults(func=benchmark_recovery)

    # --- sweep ---
    sweep_parser = benchmark_subparsers.add_parser(
        "sweep",
        help="Grid sweep over profiles; oracle pass/fail for each (frontier).",
    )
    _add_benchmark_common_args(sweep_parser)
    sweep_parser.add_argument("--profile-grid", required=True, help="Comma-separated list of profile names to sweep.")
    sweep_parser.add_argument("--max-loss", type=float, default=5.0, help="Oracle: max loss %%.")
    sweep_parser.add_argument("--max-latency-ms", type=float, default=300.0, help="Oracle: max p95 latency (ms).")
    sweep_parser.add_argument("--rate-hz", type=float, default=20.0, help="Publish rate (Hz).")
    sweep_parser.add_argument("--size", type=int, default=60000, help="Payload size (bytes).")
    sweep_parser.add_argument("--topic", default="", help="Topic to evaluate.")
    sweep_parser.add_argument("--duration", type=float, default=60.0, help="Seconds per sweep point.")
    sweep_parser.add_argument("--out", default="frontier.jsonl", help="Output frontier JSONL file.")
    sweep_parser.set_defaults(func=benchmark_sweep)

    # --- plot ---
    plot_parser = benchmark_subparsers.add_parser(
        "plot",
        help="Render a benchmark figure from a results file.",
    )
    plot_parser.add_argument("input", nargs="?", help="Input file (budgets.jsonl, curve.jsonl, recovery.json, etc.).")
    plot_parser.add_argument("--out", help="Output figure path (default: <input>.png).")
    plot_parser.add_argument("--artifacts-dir", help="Output directory for all benchmark artifacts.")
    plot_parser.add_argument(
        "--type",
        choices=["auto", "frontier", "offered_bw", "ramp", "recovery", "heatmap"],
        default="auto",
        help="Plot type (default: auto-detect from data).",
    )
    plot_parser.set_defaults(func=benchmark_plot)
