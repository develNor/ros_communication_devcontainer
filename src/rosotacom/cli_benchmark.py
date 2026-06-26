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
import math
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

DEFAULT_BENCHMARK_RMW = "cyclone"
BENCHMARK_RESULT_FILE = "result.json"
BENCHMARK_SESSIONS_BY_GENRE = {
    "capacity": "bench_1_1_capacity",
    "sweep": "bench_1_2_load_sweep",
    "ramp": "bench_1_3_ramp",
    "recovery": "bench_1_4_recovery",
    "sensitivity": "bench_1_1_capacity",
    "matrix": "bench_1_1_capacity",
    "requirements": "bench_1_1_capacity",
}


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


def _normalize_benchmark_profile(profile: str | None) -> str | None:
    if profile is None:
        return None
    normalized = profile.strip()
    if not normalized or normalized.lower() in {"none", "unshaped"}:
        return None
    return normalized


def _benchmark_profile_label(profile: str | None) -> str:
    return _normalize_benchmark_profile(profile) or "none"


def _apply_benchmark_qos_options(
    cfg: dict[str, Any],
    *,
    reliability: str | None = None,
    depth: int | None = None,
) -> dict[str, Any] | None:
    if reliability is None and depth is None:
        return None
    shared = cfg.setdefault("shared", {})
    if not isinstance(shared, dict):
        raise RuntimeError("Benchmark session config 'shared' must be a mapping.")
    qos = shared.setdefault("qos", {})
    if not isinstance(qos, dict):
        raise RuntimeError("Benchmark session config 'shared.qos' must be a mapping.")
    if depth is not None:
        if depth < 1:
            raise ValueError("QoS depth must be >= 1.")
        defaults = qos.setdefault("defaults", {})
        if not isinstance(defaults, dict):
            raise RuntimeError("Benchmark session config 'shared.qos.defaults' must be a mapping.")
        defaults["depth"] = depth
    if reliability is not None:
        if reliability not in {"best_effort", "reliable"}:
            raise ValueError("QoS reliability must be 'best_effort' or 'reliable'.")
        role_defaults = qos.setdefault("for_role", {})
        if not isinstance(role_defaults, dict):
            raise RuntimeError("Benchmark session config 'shared.qos.for_role' must be a mapping.")
        for role in ("ota_sub", "ota_pub"):
            role_cfg = role_defaults.setdefault(role, {})
            if not isinstance(role_cfg, dict):
                raise RuntimeError(f"Benchmark session config QoS role {role!r} must be a mapping.")
            role_cfg["reliability"] = reliability
    return {"reliability": reliability, "depth": depth}


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
    qos_options = _apply_benchmark_qos_options(
        cfg,
        reliability=getattr(args, "qos_reliability", None),
        depth=getattr(args, "qos_depth", None),
    )
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
        "qos": qos_options,
    }


def _activate_benchmark_session_options(
    args: argparse.Namespace,
    session_name: str,
    out_dir: Path,
    options: dict[str, Any] | None,
) -> Any:
    if not options:
        return None

    source_session = Path(getattr(args, "_benchmark_session_dir", ""))
    if not source_session.is_dir():
        raise FileNotFoundError("Row-specific benchmark session options require a prepared session copy.")

    case_token = _safe_case_token(str(options.get("case_token") or "case"))
    dest_root = out_dir / "case-session-configs" / case_token
    dest_session = dest_root / session_name
    if dest_session.exists():
        shutil.rmtree(dest_session)
    shutil.copytree(source_session, dest_session)

    config_path = dest_session / "session-definition.yaml"
    if not config_path.is_file():
        config_path = dest_session / "session-parametrization.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(f"Benchmark session {session_name!r} has no session YAML: {dest_session}")

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(cfg, dict):
        raise RuntimeError(f"Benchmark session config must be a mapping: {config_path}")
    _apply_benchmark_qos_options(
        cfg,
        reliability=options.get("qos_reliability"),
        depth=options.get("qos_depth"),
    )
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    previous = getattr(args, "session_configs_dir", None)
    if previous is None:
        existing: list[Any] = []
    elif isinstance(previous, list | tuple):
        existing = list(previous)
    else:
        existing = [previous]
    args.session_configs_dir = [str(dest_root), *[str(path) for path in existing]]
    return previous


def _restore_benchmark_session_options(args: argparse.Namespace, previous: Any) -> None:
    if previous is None:
        if hasattr(args, "session_configs_dir"):
            args.session_configs_dir = None
    else:
        args.session_configs_dir = previous


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
    profile = _normalize_benchmark_profile(profile)
    profiles_file = _benchmark_profiles_file(profile, runtime.profiles_file)
    return cast(
        dict[str, Any],
        _jsonable(
            {
                "command": " ".join(shlex.quote(arg) for arg in sys.argv),
                "genre": genre,
                "execution": {
                    "mode": "ota" if _is_ota_benchmark(args) else "local",
                    "dry_run": bool(getattr(args, "dry_run", False)),
                },
                "rmw": {
                    "requested": rmw,
                    "runtime_implementation": _rmw_runtime_implementation(rmw),
                },
                "profile": _profile_context(profile, profiles_file),
                "session": session,
                "paths": {
                    "run_dir": run_dir,
                    "rosotacom_config": runtime.rosotacom_config,
                    "ros2docker_config": runtime.ros2docker_config,
                    "profiles_file": profiles_file,
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


def _benchmark_ota_target(args: argparse.Namespace, session_name: str) -> tuple[str, str]:
    target = getattr(args, "target", None)
    target_type = getattr(args, "target_type", None)
    if target:
        return str(target), str(target_type or "auto")
    if target_type not in (None, "session"):
        raise ValueError("--target-type requires --target unless --target-type=session.")
    return session_name, "session"


def _is_ota_benchmark(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "ota_benchmark", False) or getattr(args, "deployment", None))


def _benchmark_profiles_file(profile: str | None, profiles_file: Path | None) -> Path | None:
    profile = _normalize_benchmark_profile(profile)
    if profiles_file is not None:
        return _find_profiles_file(profiles_file)
    try:
        return _find_profiles_file(None)
    except FileNotFoundError:
        if profile:
            raise
        return None


def _benchmark_artifacts_root(args: argparse.Namespace) -> Path:
    from .cli import _load_runtime_config

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir
    return artifacts_dir or Path.cwd() / "artifacts" / "benchmarks"


def _benchmark_tmux_session_name(args: argparse.Namespace, genre: str) -> str:
    from .cli import _safe_path_token

    profile = getattr(args, "profile", None) or getattr(args, "profile_grid", None) or "run"
    return _safe_path_token(f"benchmark-{genre}-{profile}")


def _strip_interactive_args(argv: Sequence[str]) -> list[str]:
    return [arg for arg in argv if arg not in {"--interactive", "--no-attach"}]


def _benchmark_child_command() -> list[str]:
    return [sys.executable, "-m", "rosotacom", *_strip_interactive_args(sys.argv[1:])]


def _initialize_interactive_log(full_log: Path, command: Sequence[str]) -> None:
    full_log.parent.mkdir(parents=True, exist_ok=True)
    full_log.write_text(
        "\n".join(
            [
                f"--- benchmark operator log initialized {datetime.now().isoformat(timespec='seconds')} ---",
                "command: " + " ".join(command),
                "",
            ]
        ),
        encoding="utf-8",
    )


def _benchmark_run_script(command: Sequence[str], full_log: Path, session_name: str) -> str:
    pattern = (
        r"probe\(|Benchmark result saved|Capacity result|Capacity:|Ramp curve copied|Recovery metrics copied|"
        r"Sweep frontier copied|ERROR|Error|Warning|benchmark exited"
    )
    script = f"""
import pathlib
import re
import subprocess
import sys

cmd = {json.dumps(list(command))}
log_path = pathlib.Path({str(full_log)!r})
log_path.parent.mkdir(parents=True, exist_ok=True)
selected = re.compile({pattern!r})

print("[INFO] benchmark operator session: {session_name}")
print("[INFO] full orchestrator log:", log_path)
print("[INFO] running:", " ".join(cmd), flush=True)
with log_path.open("a", encoding="utf-8") as log:
    log.write("\\n--- benchmark run started ---\\n")
    log.write("command: " + " ".join(cmd) + "\\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        log.write(line)
        log.flush()
        if selected.search(line):
            sys.stdout.write(line)
            sys.stdout.flush()
    rc = proc.wait()
    log.write(f"--- benchmark exited with status {{rc}} ---\\n")
print()
print("[INFO] benchmark exited with status", rc)
sys.exit(rc)
""".strip()
    return shlex.join([sys.executable, "-c", script])


def _grep_log_fragment(log_path: Path, pattern: str, *, lines: int = 40) -> str:
    return (
        f"log={shlex.quote(str(log_path))}; "
        'if [ -f "$log" ]; then '
        f'grep -E {shlex.quote(pattern)} "$log" 2>/dev/null | tail -{lines} || true; '
        "else "
        "echo '[INFO] waiting for orchestrator log:' \"$log\"; "
        "fi"
    )


def _peer_catmux_attach_script(identity: str, container_name: str, full_log: Path) -> str:
    launch_pattern = f"{container_name}|--identity {identity}|identity {identity}|rosotacom session started"
    return (
        "while true; do "
        "clear; date; "
        f"container={shlex.quote(container_name)}; identity={shlex.quote(identity)}; "
        'if ! docker inspect -f "{{.State.Running}}" "$container" >/dev/null 2>&1; then '
        'echo "[INFO] waiting for benchmark peer $identity container: $container"; '
        + _grep_log_fragment(full_log, launch_pattern)
        + "; "
        "sleep 1; continue; "
        "fi; "
        'session=$(docker exec "$container" tmux -L catmux list-sessions -F "#{session_name}" 2>/dev/null '
        "| head -1 || true); "
        'if [ -z "$session" ]; then '
        'echo "[INFO] container is running; waiting for catmux session on tmux socket catmux"; '
        'docker logs --tail 25 "$container" 2>&1 || true; '
        + _grep_log_fragment(full_log, launch_pattern, lines=25)
        + "; "
        "sleep 1; continue; "
        "fi; "
        'echo "[INFO] attaching to benchmark peer $identity catmux session: $session"; '
        'docker exec -it "$container" tmux -L catmux attach-session -t "$session"; '
        "rc=$?; echo; echo '[INFO] catmux attach exited with status' \"$rc\"; "
        "sleep 1; "
        "done"
    )


def _benchmark_peer_windows(args: argparse.Namespace, genre: str, full_log: Path) -> list[tuple[str, str, str]]:
    from .cli import (
        _container_name,
        _effective_session_config,
        _load_runtime_config,
        _remote_peer_name,
        _resolve_session,
        _safe_path_token,
    )

    if getattr(args, "deployment", None):
        return []
    session_name = BENCHMARK_SESSIONS_BY_GENRE[genre]
    runtime = _load_runtime_config(args)
    session = _resolve_session(session_name, runtime)
    cfg = _effective_session_config(session.host_dir, runtime)
    peers = cfg.get("peers")
    if not isinstance(peers, dict):
        return []
    windows: list[tuple[str, str, str]] = []
    for identity in sorted(str(peer) for peer in peers):
        container_name = _container_name(_remote_peer_name(cfg, identity), runtime)
        window_name = _safe_path_token(f"{identity}_catmux")
        windows.append((window_name, identity, _peer_catmux_attach_script(identity, container_name, full_log)))
    return windows


def _result_once_script(full_log: Path) -> str:
    script = f"""
import json
import pathlib
import re
import time

log_path = pathlib.Path({str(full_log)!r})
result_re = re.compile(r"Benchmark result saved to (.+)")
exit_re = re.compile(r"benchmark exited with status (\\d+)")

def hold_open(message):
    print(message, flush=True)
    while True:
        time.sleep(3600)

print("[INFO] waiting for final benchmark result in", log_path, flush=True)
while True:
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        matches = result_re.findall(text)
        if matches:
            result_path = pathlib.Path(matches[-1].strip())
            print("[INFO] final benchmark result:", result_path)
            if result_path.is_file():
                with result_path.open(encoding="utf-8") as handle:
                    print(json.dumps(json.load(handle), indent=2, sort_keys=True))
            else:
                print("[WARN] result path was reported but is not readable yet:", result_path)
            print()
            hold_open("[INFO] result printed once; pane stays alive for inspection")
        exit_match = exit_re.search(text)
        if exit_match:
            print("[WARN] benchmark exited with status", exit_match.group(1), "before reporting a result file")
            print("[INFO] inspect the run window or full orchestrator log:", log_path)
            hold_open("[INFO] pane stays alive for inspection")
    time.sleep(1)
""".strip()
    return shlex.join([sys.executable, "-c", script])


def _network_command_watch_script(full_log: Path, *, is_ota: bool) -> str:
    pattern = r"tc qdisc|netem|qdisc on|Timeline step|profile safety watchdog|OTA benchmark|tc/netem"
    label = "remote tc/netem commands" if is_ota else "local tc/netem commands"
    return (
        "while true; do "
        "clear; date; "
        f"echo '[INFO] {label} from orchestrator log'; " + _grep_log_fragment(full_log, pattern, lines=100) + "; "
        "sleep 2; "
        "done"
    )


def _network_watch_script(*, is_ota: bool) -> str:
    if is_ota:
        return (
            "while true; do "
            "clear; date; "
            "echo '[INFO] OTA benchmark: network shaping is applied on the remote peers.'; "
            "echo '[INFO] Watch the network:commands pane for tc/netem commands and qdisc diagnostics.'; "
            "sleep 5; "
            "done"
        )
    return (
        "while true; do "
        "clear; date; "
        "echo '[INFO] local benchmark containers'; "
        "docker ps --filter name=rosotacom --format 'table {{.Names}}\\t{{.Status}}' 2>/dev/null || true; "
        "for c in $(docker ps --filter name=rosotacom --format '{{.Names}}' 2>/dev/null); do "
        "echo; echo '[INFO]' \"$c\" 'eth0 qdisc'; "
        'docker exec -u root "$c" tc qdisc show dev eth0 2>/dev/null || true; '
        "done; "
        "sleep 2; "
        "done"
    )


def _start_interactive_benchmark(args: argparse.Namespace, genre: str) -> int:
    from .cli import (
        _attach_tmux_pipe,
        _create_tmux_split_below,
        _host_shell,
        _load_runtime_config,
        _require_tmux,
        _tmux_command,
        _tmux_session_exists,
    )

    runtime = _load_runtime_config(args)
    session_name = _benchmark_tmux_session_name(args, genre)
    command = _benchmark_child_command()
    artifacts_root = _benchmark_artifacts_root(args)
    interactive_logs = artifacts_root / "interactive" / session_name
    full_log = interactive_logs / "orchestrator.full.log"
    is_ota = _is_ota_benchmark(args)

    run_script = _benchmark_run_script(command, full_log, session_name)
    network_script = _network_watch_script(is_ota=is_ota)
    network_command_script = _network_command_watch_script(full_log, is_ota=is_ota)
    result_script = _result_once_script(full_log)
    peer_windows = _benchmark_peer_windows(args, genre, full_log) if not is_ota else []

    if getattr(args, "dry_run", False):
        print(f"Would create benchmark tmux session: {session_name}")
        print(f"Run window: high-level orchestrator (full log {full_log})")
        print("Child command: " + shlex.join(command))
        for window_name, identity, _attach_script in peer_windows:
            print(f"Peer window {identity}: {window_name} fullscreen catmux attach")
        print(
            "Network window: qdisc monitor + tc command log"
            if not is_ota
            else "Network window: OTA shaping monitor + remote tc command log"
        )
        print("Results window: final result printed once")
        return 0

    _require_tmux()
    if _tmux_session_exists(runtime, session_name):
        if not getattr(args, "no_attach", False):
            subprocess.run(_tmux_command(runtime, "attach-session", "-t", session_name), check=True)
        else:
            subcommand = sys.argv[1] if len(sys.argv) > 1 else ("ota-benchmark" if is_ota else "benchmark")
            print(f"Attach with: rosotacom {subcommand} {genre} ... --interactive")
        return 0

    _initialize_interactive_log(full_log, command)
    created = subprocess.run(
        _tmux_command(
            runtime,
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            session_name,
            "-n",
            "run",
            _host_shell(run_script),
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    run_pane = created.stdout.strip()
    _attach_tmux_pipe(runtime, run_pane, interactive_logs / "run.log")
    subprocess.run(
        _tmux_command(runtime, "set-window-option", "-g", "-t", session_name, "remain-on-exit", "on"),
        check=True,
    )
    subprocess.run(_tmux_command(runtime, "set-option", "-t", session_name, "prefix", "C-b"), check=True)
    subprocess.run(_tmux_command(runtime, "bind-key", "-T", "prefix", "C-b", "send-prefix"), check=True)
    subprocess.run(
        _tmux_command(runtime, "set-option", "-t", session_name, "status-right", " benchmark | C-b n/p "),
        check=True,
    )

    for window_name, identity, attach_script in peer_windows:
        created_window = subprocess.run(
            _tmux_command(
                runtime,
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                session_name,
                "-n",
                window_name,
                _host_shell(attach_script),
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        pane_id = created_window.stdout.strip()
        subprocess.run(_tmux_command(runtime, "select-pane", "-t", pane_id, "-T", f"{identity}:catmux"), check=True)
        _attach_tmux_pipe(runtime, pane_id, interactive_logs / f"{identity}_catmux.log")

    created_network = subprocess.run(
        _tmux_command(
            runtime,
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            session_name,
            "-n",
            "network",
            _host_shell(network_script),
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    network_pane = created_network.stdout.strip()
    subprocess.run(_tmux_command(runtime, "select-pane", "-t", network_pane, "-T", "network:status"), check=True)
    _attach_tmux_pipe(runtime, network_pane, interactive_logs / "network.log")
    _create_tmux_split_below(
        runtime,
        network_pane,
        "network:commands",
        _host_shell(network_command_script),
        log_path=interactive_logs / "network_commands.log",
    )
    subprocess.run(
        _tmux_command(runtime, "select-layout", "-t", f"{session_name}:network", "even-vertical"), check=True
    )

    created_results = subprocess.run(
        _tmux_command(
            runtime,
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            session_name,
            "-n",
            "results",
            _host_shell(result_script),
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    results_pane = created_results.stdout.strip()
    subprocess.run(_tmux_command(runtime, "select-pane", "-t", results_pane, "-T", "results:final"), check=True)
    _attach_tmux_pipe(runtime, results_pane, interactive_logs / "results.log")

    subprocess.run(_tmux_command(runtime, "select-window", "-t", f"{session_name}:run"), check=True)
    print(f"Benchmark tmux session: {session_name}")
    print("Local control tmux prefix: Ctrl-b.")
    if not getattr(args, "no_attach", False):
        subprocess.run(_tmux_command(runtime, "attach-session", "-t", session_name), check=True)
    else:
        print("Attach with: " + shlex.join(_tmux_command(runtime, "attach-session", "-t", session_name)))
    return 0


def _format_bps(value: float | None) -> str:
    if value is None:
        return "unknown"

    def decimal(scaled: float) -> str:
        return f"{scaled:.3f}".rstrip("0").rstrip(".")

    if value >= 1_000_000:
        return f"{decimal(value / 1_000_000)} Mbit/s"
    if value >= 1_000:
        return f"{decimal(value / 1_000)} kbit/s"
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
    profile: str | None,
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
    profile = _normalize_benchmark_profile(profile)
    profile_label = _benchmark_profile_label(profile)
    slice_ = CapacitySlice(profile=profile_label, knob=knob, fixed={"rate": rate_hz})
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
        key=BudgetKey(sha=_current_sha(), profile=profile_label, genre="capacity"),
        metrics=metrics,
    )
    save_budget(budget_path, [budget_entry])
    result_path = _write_benchmark_result(
        out_dir,
        genre="capacity",
        context=result_context,
        configuration={
            "profile": profile_label,
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
    profile: str | None,
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
    profile = _normalize_benchmark_profile(profile)
    profile_label = _benchmark_profile_label(profile)
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
            "profile": profile_label,
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

    for raw_profile in profile_grid:
        profile = _normalize_benchmark_profile(raw_profile)
        profile_label = _benchmark_profile_label(profile)
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
            "profile": profile_label,
            "passes": passes,
            "loss_pct": float(loss_pct),
            "latency_p95_ms": float(latency_p95) if latency_p95 is not None else None,
        }
        frontier.append(result)
        load_info = _load_context(load)
        point_measurements.append(
            {
                "profile": profile,
                "profile_label": profile_label,
                "passed": passes,
                "load": load_info,
                "topics": rows,
                "summary": summary,
            }
        )
        print(
            f"  sweep({profile_label}): {'PASS' if passes else 'FAIL'} "
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
            "profile_grid": [_benchmark_profile_label(profile) for profile in profile_grid],
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
# Benchmark driver: sensitivity
# --------------------------------------------------------------------------- #


def _safe_case_token(value: str) -> str:
    from .cli import _safe_path_token

    return _safe_path_token(
        value.replace("%", "pct")
        .replace("/", "_")
        .replace(" ", "")
        .replace(".", "p")
        .replace("-", "m")
        .replace("+", "p")
    )


def _format_yaml_ms(value: float | None) -> str | None:
    return None if value is None else f"{value:g}ms"


def _format_yaml_pct(value: float | None) -> str | None:
    return None if value is None else f"{value:g}%"


def _format_yaml_rate(value: float | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value

    def decimal(scaled: float) -> str:
        return f"{scaled:.6f}".rstrip("0").rstrip(".")

    if value >= 1_000_000_000:
        return f"{decimal(value / 1_000_000_000)}gbit"
    if value >= 1_000_000:
        return f"{decimal(value / 1_000_000)}mbit"
    if value >= 1_000:
        return f"{decimal(value / 1_000)}kbit"
    return f"{decimal(value)}bit"


def _parse_rate_values(raw: str) -> list[str]:
    return [value.strip() for value in raw.split(",") if value.strip()]


def _parse_sensitivity_axes(raw: str) -> set[str]:
    available = {"loss", "delay", "jitter", "rate", "loss_correlation"}
    aliases = {"loss-correlation": "loss_correlation"}
    axes = {aliases.get(value.strip(), value.strip()) for value in raw.split(",") if value.strip()}
    if not axes or "all" in axes:
        return available
    unknown = axes - available
    if unknown:
        raise ValueError(f"Unknown sensitivity axes: {', '.join(sorted(unknown))}")
    return axes


def _parse_matrix_axes(raw: str) -> set[str]:
    available = {"latency", "hz", "qos"}
    axes = {value.strip().lower() for value in raw.split(",") if value.strip()}
    if not axes or "all" in axes:
        return available
    unknown = axes - available
    if unknown:
        raise ValueError(f"Unknown matrix axes: {', '.join(sorted(unknown))}")
    return axes


def _parse_qos_cases(raw: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            reliability, depth = item.split(":", 1)
        else:
            reliability, depth = item, "1"
        reliability = reliability.strip()
        if reliability not in {"best_effort", "reliable"}:
            raise ValueError(f"Unknown QoS reliability in case {item!r}.")
        try:
            depth_int = int(depth.strip())
        except ValueError as exc:
            raise ValueError(f"QoS depth must be an integer in case {item!r}.") from exc
        if depth_int < 1:
            raise ValueError(f"QoS depth must be >= 1 in case {item!r}.")
        cases.append({"reliability": reliability, "depth": depth_int})
    return cases


def _duration_for_min_messages(rate_hz: float, *, min_duration_s: float, min_messages: int) -> float:
    if rate_hz <= 0:
        raise ValueError("rate_hz must be > 0.")
    return max(float(min_duration_s), math.ceil(float(min_messages) / rate_hz))


def _direction_to_yaml(
    *,
    rate: str | None = None,
    delay_ms: float | None = None,
    jitter_ms: float | None = None,
    distribution: str | None = None,
    loss_pct: float | None = None,
    loss_correlation_pct: float | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if rate is not None:
        spec["rate"] = rate
    if delay_ms is not None:
        spec["delay"] = _format_yaml_ms(delay_ms)
    if jitter_ms is not None:
        spec["jitter"] = _format_yaml_ms(jitter_ms)
        if distribution and jitter_ms > 0.0:
            spec["distribution"] = distribution
    if loss_pct is not None:
        spec["loss"] = _format_yaml_pct(loss_pct)
        if loss_correlation_pct is not None and loss_pct > 0.0:
            spec["loss_correlation"] = _format_yaml_pct(loss_correlation_pct)
    return spec


def _profile_to_yaml(profile: Any) -> dict[str, Any]:
    if getattr(profile, "is_timeline", False):
        raise ValueError("sensitivity requires a static base profile, not a timeline profile")

    def direction(direction_obj: Any) -> dict[str, Any]:
        if direction_obj is None:
            return {}
        return _direction_to_yaml(
            rate=_format_yaml_rate(direction_obj.rate_bps),
            delay_ms=direction_obj.delay_ms,
            jitter_ms=direction_obj.jitter_ms,
            distribution=direction_obj.distribution,
            loss_pct=direction_obj.loss_pct,
            loss_correlation_pct=direction_obj.loss_correlation_pct,
        )

    return {"uplink": direction(profile.uplink), "downlink": direction(profile.downlink)}


def _first_direction_attr(profile: Any, direction_name: str, attr: str, default: Any = None) -> Any:
    direction = getattr(profile, direction_name, None)
    return getattr(direction, attr, default) if direction is not None else default


def _build_sensitivity_profiles(
    *,
    base_profile: str,
    profiles_file: Path,
    ideal_rate: str,
    loss_values: Sequence[float],
    delay_values: Sequence[float],
    jitter_values: Sequence[float],
    rate_values: Sequence[str],
    correlation_values: Sequence[float],
    axes: Sequence[str] | set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from .network_profiles import load_profiles_file, parse_rate_bps

    profiles = load_profiles_file(profiles_file)
    base_obj = profiles.get(base_profile)
    if base_obj is None:
        raise ValueError(f"Base profile {base_profile!r} not found in {profiles_file}.")
    if getattr(base_obj, "is_timeline", False):
        raise ValueError(f"Base profile {base_profile!r} is a timeline; sensitivity needs a static profile.")

    # Validate rate tokens early; keep the original strings in YAML for readability.
    parse_rate_bps(ideal_rate)
    for rate in rate_values:
        parse_rate_bps(rate)

    base_distribution = (
        _first_direction_attr(base_obj, "uplink", "distribution")
        or _first_direction_attr(base_obj, "downlink", "distribution")
        or "normal"
    )
    base_uplink_delay = float(_first_direction_attr(base_obj, "uplink", "delay_ms", 0.0) or 0.0)
    base_downlink_delay = float(_first_direction_attr(base_obj, "downlink", "delay_ms", base_uplink_delay) or 0.0)
    base_uplink_loss = float(_first_direction_attr(base_obj, "uplink", "loss_pct", 0.0) or 0.0)
    base_downlink_loss = float(_first_direction_attr(base_obj, "downlink", "loss_pct", base_uplink_loss) or 0.0)

    selected_axes = set(axes) if axes is not None else _parse_sensitivity_axes("all")
    generated: dict[str, Any] = {}
    cases: list[dict[str, Any]] = [{"axis": "baseline", "profile": None, "value": "unshaped"}]
    generated[base_profile] = _profile_to_yaml(base_obj)

    def add_case(name: str, axis: str, value: str, profile_spec: dict[str, Any], description: str) -> None:
        generated[name] = profile_spec
        cases.append({"axis": axis, "profile": name, "value": value, "description": description})

    def static_profile(
        *,
        rate: str = ideal_rate,
        delay_uplink_ms: float | None = None,
        delay_downlink_ms: float | None = None,
        jitter_uplink_ms: float | None = None,
        jitter_downlink_ms: float | None = None,
        loss_uplink_pct: float | None = None,
        loss_downlink_pct: float | None = None,
        loss_correlation_pct: float | None = None,
    ) -> dict[str, Any]:
        return {
            "uplink": _direction_to_yaml(
                rate=rate,
                delay_ms=delay_uplink_ms,
                jitter_ms=jitter_uplink_ms,
                distribution=base_distribution,
                loss_pct=loss_uplink_pct,
                loss_correlation_pct=loss_correlation_pct,
            ),
            "downlink": _direction_to_yaml(
                rate=rate,
                delay_ms=delay_downlink_ms,
                jitter_ms=jitter_downlink_ms,
                distribution=base_distribution,
                loss_pct=loss_downlink_pct,
                loss_correlation_pct=loss_correlation_pct,
            ),
        }

    add_case(
        "lab_ideal_rate_only",
        "ideal",
        ideal_rate,
        static_profile(rate=ideal_rate),
        "High-rate tbf only; no netem delay/jitter/loss.",
    )
    add_case(
        f"lab_base_{_safe_case_token(base_profile)}",
        "base",
        base_profile,
        _profile_to_yaml(base_obj),
        "Original base profile copied into the generated lab file.",
    )

    if "loss" in selected_axes:
        for loss in loss_values:
            add_case(
                f"lab_loss_{_safe_case_token(_format_yaml_pct(loss) or str(loss))}",
                "loss",
                _format_yaml_pct(loss) or str(loss),
                static_profile(rate=ideal_rate, loss_uplink_pct=loss, loss_downlink_pct=loss),
                "Only random packet loss is varied; rate is high and delay/jitter are absent.",
            )

    if "delay" in selected_axes:
        for delay in delay_values:
            add_case(
                f"lab_delay_{_safe_case_token(_format_yaml_ms(delay) or str(delay))}",
                "delay",
                _format_yaml_ms(delay) or str(delay),
                static_profile(rate=ideal_rate, delay_uplink_ms=delay, delay_downlink_ms=delay),
                "Only fixed delay is varied; rate is high and loss/jitter are absent.",
            )

    if "jitter" in selected_axes:
        for jitter in jitter_values:
            add_case(
                f"lab_jitter_{_safe_case_token(_format_yaml_ms(jitter) or str(jitter))}",
                "jitter",
                _format_yaml_ms(jitter) or str(jitter),
                static_profile(
                    rate=ideal_rate,
                    delay_uplink_ms=base_uplink_delay,
                    delay_downlink_ms=base_downlink_delay,
                    jitter_uplink_ms=jitter,
                    jitter_downlink_ms=jitter,
                ),
                "Jitter is varied around the base profile's mean delay; rate is high and loss is absent.",
            )

    if "rate" in selected_axes:
        for rate in rate_values:
            add_case(
                f"lab_rate_{_safe_case_token(rate)}",
                "rate",
                rate,
                static_profile(rate=rate),
                "Only rate limiting is varied; delay/jitter/loss are absent.",
            )

    if "loss_correlation" in selected_axes:
        for correlation in correlation_values:
            add_case(
                f"lab_loss_correlation_{_safe_case_token(_format_yaml_pct(correlation) or str(correlation))}",
                "loss_correlation",
                _format_yaml_pct(correlation) or str(correlation),
                static_profile(
                    rate=ideal_rate,
                    loss_uplink_pct=base_uplink_loss,
                    loss_downlink_pct=base_downlink_loss,
                    loss_correlation_pct=correlation,
                ),
                "Loss correlation is varied at the base profile's loss rate; rate is high and delay/jitter are absent.",
            )

    doc = {
        "profiles": generated,
        "metadata": {
            "kind": "benchmark-sensitivity-generated",
            "source_profiles_file": str(profiles_file),
            "base_profile": base_profile,
            "ideal_rate": ideal_rate,
            "axes": sorted(selected_axes),
        },
    }
    return doc, cases


def _build_matrix_profiles(
    *,
    base_profile: str,
    profiles_file: Path,
    ideal_rate: str,
    jitter_ms: float,
    latency_values: Sequence[float],
    rate_hz_values: Sequence[float],
    qos_cases: Sequence[dict[str, Any]],
    axes: Sequence[str] | set[str] | None,
    size: int,
    fixed_rate_hz: float,
    min_duration_s: float,
    min_messages: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from .network_profiles import load_profiles_file, parse_rate_bps

    profiles = load_profiles_file(profiles_file)
    base_obj = profiles.get(base_profile)
    if base_obj is None:
        raise ValueError(f"Base profile {base_profile!r} not found in {profiles_file}.")
    if getattr(base_obj, "is_timeline", False):
        raise ValueError(f"Base profile {base_profile!r} is a timeline; matrix needs a static profile.")

    parse_rate_bps(ideal_rate)
    selected_axes = set(axes) if axes is not None else _parse_matrix_axes("all")
    base_distribution = (
        _first_direction_attr(base_obj, "uplink", "distribution")
        or _first_direction_attr(base_obj, "downlink", "distribution")
        or "normal"
    )
    base_uplink_delay = float(_first_direction_attr(base_obj, "uplink", "delay_ms", 0.0) or 0.0)
    base_downlink_delay = float(_first_direction_attr(base_obj, "downlink", "delay_ms", base_uplink_delay) or 0.0)
    downlink_ratio = base_downlink_delay / base_uplink_delay if base_uplink_delay > 0.0 else 1.0

    generated: dict[str, Any] = {}
    cases: list[dict[str, Any]] = []

    def jitter_profile(name: str, uplink_delay_ms: float) -> str:
        downlink_delay_ms = round(uplink_delay_ms * downlink_ratio, 6)
        generated[name] = {
            "uplink": _direction_to_yaml(
                rate=ideal_rate,
                delay_ms=uplink_delay_ms,
                jitter_ms=jitter_ms,
                distribution=base_distribution,
            ),
            "downlink": _direction_to_yaml(
                rate=ideal_rate,
                delay_ms=downlink_delay_ms,
                jitter_ms=jitter_ms,
                distribution=base_distribution,
            ),
        }
        return name

    fixed_latency_label = _format_yaml_ms(base_uplink_delay) or str(base_uplink_delay)
    fixed_profile = jitter_profile(
        f"matrix_jitter_{_safe_case_token(_format_yaml_ms(jitter_ms) or str(jitter_ms))}"
        f"_latency_{_safe_case_token(fixed_latency_label)}",
        base_uplink_delay,
    )

    def add_case(
        *,
        axis: str,
        value: str,
        profile: str,
        rate_hz: float,
        qos_reliability: str = "best_effort",
        qos_depth: int = 1,
        description: str,
    ) -> None:
        duration_s = _duration_for_min_messages(
            rate_hz,
            min_duration_s=min_duration_s,
            min_messages=min_messages,
        )
        case_token = _safe_case_token(f"{axis}_{value}_{qos_reliability}_{qos_depth}_{rate_hz:g}")
        load = {"size_a": size, "rate": rate_hz}
        cases.append(
            {
                "axis": axis,
                "value": value,
                "profile": profile,
                "description": description,
                "load": load,
                "duration_s": duration_s,
                "qos": {"reliability": qos_reliability, "depth": qos_depth},
                "session_options": {
                    "case_token": case_token,
                    "qos_reliability": qos_reliability,
                    "qos_depth": qos_depth,
                },
            }
        )

    add_case(
        axis="reference",
        value=f"jitter={_format_yaml_ms(jitter_ms)},latency={fixed_latency_label},hz={fixed_rate_hz:g}",
        profile=fixed_profile,
        rate_hz=fixed_rate_hz,
        description="Fixed 30ms-jitter reference using base-profile latency ratio and benchmark default QoS.",
    )

    if "latency" in selected_axes:
        for latency in latency_values:
            latency_label = _format_yaml_ms(latency) or str(latency)
            profile = jitter_profile(
                f"matrix_latency_{_safe_case_token(latency_label)}"
                f"_jitter_{_safe_case_token(_format_yaml_ms(jitter_ms) or str(jitter_ms))}",
                latency,
            )
            add_case(
                axis="latency",
                value=latency_label,
                profile=profile,
                rate_hz=fixed_rate_hz,
                description=(
                    "Vary mean uplink delay while preserving the base profile's uplink/downlink latency ratio; "
                    "jitter is fixed and random loss is absent."
                ),
            )

    if "hz" in selected_axes:
        for rate_hz in rate_hz_values:
            add_case(
                axis="hz",
                value=f"{rate_hz:g}hz",
                profile=fixed_profile,
                rate_hz=rate_hz,
                description=(
                    "Vary publisher frequency under the fixed 30ms-jitter profile; duration is extended to keep "
                    "at least the configured minimum message count."
                ),
            )

    if "qos" in selected_axes:
        for qos in qos_cases:
            reliability = str(qos["reliability"])
            depth = int(qos["depth"])
            add_case(
                axis="qos",
                value=f"{reliability}:depth{depth}",
                profile=fixed_profile,
                rate_hz=fixed_rate_hz,
                qos_reliability=reliability,
                qos_depth=depth,
                description="Vary OTA QoS role defaults under the fixed 30ms-jitter profile.",
            )

    doc = {
        "profiles": generated,
        "metadata": {
            "kind": "benchmark-matrix-generated",
            "source_profiles_file": str(profiles_file),
            "base_profile": base_profile,
            "ideal_rate": ideal_rate,
            "jitter_ms": jitter_ms,
            "fixed_rate_hz": fixed_rate_hz,
            "min_duration_s": min_duration_s,
            "min_messages": min_messages,
            "axes": sorted(selected_axes),
            "latency_model": {
                "mode": "base-ratio",
                "base_uplink_delay_ms": base_uplink_delay,
                "base_downlink_delay_ms": base_downlink_delay,
                "downlink_ratio": downlink_ratio,
            },
        },
    }
    return doc, cases


def _selected_topic(summary: dict[str, Any], topic: str = "") -> tuple[str, dict[str, Any]]:
    topics = summary.get("topics", {})
    if not isinstance(topics, dict) or not topics:
        return "", {}
    if topic:
        return topic, topics.get(topic, {}) if isinstance(topics.get(topic), dict) else {}
    first_topic = next(iter(topics))
    topic_data = topics.get(first_topic)
    return first_topic, topic_data if isinstance(topic_data, dict) else {}


def _sensitivity_analysis(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    baseline = next((row for row in rows if row.get("axis") == "baseline"), None)
    explicit_reference = next((row for row in rows if row.get("axis") == "reference"), None)
    ideal = next((row for row in rows if row.get("axis") == "ideal"), None)
    reference = baseline or explicit_reference or ideal
    reference_loss = float(reference.get("loss_pct") or 0.0) if reference else 0.0
    reference_latency = float(reference.get("latency_p95_ms") or 0.0) if reference else 0.0

    axes: dict[str, Any] = {}
    for axis in sorted({str(row.get("axis")) for row in rows} - {"baseline", "reference"}):
        axis_rows = [row for row in rows if row.get("axis") == axis]
        if not axis_rows:
            continue
        worst_loss = max(axis_rows, key=lambda row: float(row.get("loss_pct") or 0.0))
        worst_latency = max(axis_rows, key=lambda row: float(row.get("latency_p95_ms") or 0.0))
        axes[axis] = {
            "points": len(axis_rows),
            "worst_loss": {
                "profile": worst_loss.get("profile"),
                "value": worst_loss.get("value"),
                "loss_pct": worst_loss.get("loss_pct"),
                "delta_vs_reference_pct": round(float(worst_loss.get("loss_pct") or 0.0) - reference_loss, 3),
            },
            "worst_latency": {
                "profile": worst_latency.get("profile"),
                "value": worst_latency.get("value"),
                "latency_p95_ms": worst_latency.get("latency_p95_ms"),
                "delta_vs_reference_ms": round(
                    float(worst_latency.get("latency_p95_ms") or 0.0) - reference_latency,
                    3,
                ),
            },
        }
    return {"reference_profile": reference.get("profile") if reference else None, "axes": axes}


def drive_sensitivity(
    run_point: RunPointFn,
    *,
    cases: Sequence[dict[str, Any]],
    max_loss_pct: float,
    max_latency_ms: float,
    rate_hz: float,
    size: int,
    topic: str,
    duration_s: float,
    out_dir: Path,
    result_context: dict[str, Any] | None = None,
    generated_profiles_file: Path | None = None,
) -> list[dict[str, Any]]:
    from .benchmark import OracleThresholds, oracle_passes_topic

    thresholds = OracleThresholds(max_loss_pct=max_loss_pct, max_latency_ms=max_latency_ms)
    rows: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    load = {"size_a": size, "rate": rate_hz}
    load_info = _load_context(load)

    for case in cases:
        profile = _normalize_benchmark_profile(cast(str | None, case.get("profile")))
        profile_label = _benchmark_profile_label(profile)
        summary = run_point(profile=profile, load=load, duration_s=duration_s, out_dir=out_dir)
        selected_topic, topic_data = _selected_topic(summary, topic)
        rows_for_print = _topic_rows(summary, topic)
        passes = oracle_passes_topic(topic_data, thresholds) if topic_data else False
        loss_pct = float(topic_data.get("loss_pct", 100.0)) if topic_data else 100.0
        latency_p95 = (topic_data.get("ota_hop_ms") or {}).get("p95") if topic_data else None
        jitter_p95 = (topic_data.get("jitter_ms") or {}).get("p95") if topic_data else None
        row = {
            "profile": profile_label,
            "axis": case.get("axis"),
            "value": case.get("value"),
            "description": case.get("description"),
            "passes": passes,
            "topic": selected_topic,
            "expected": topic_data.get("expected") if topic_data else None,
            "delivered": topic_data.get("delivered") if topic_data else None,
            "lost": topic_data.get("lost") if topic_data else None,
            "reordered": topic_data.get("reordered") if topic_data else None,
            "loss_pct": loss_pct,
            "latency_p95_ms": float(latency_p95) if latency_p95 is not None else None,
            "jitter_p95_ms": float(jitter_p95) if jitter_p95 is not None else None,
            "offered_bw_bps": float(load_info["offered_bandwidth_bps"] or 0.0),
        }
        rows.append(row)
        measurements.append({"case": case, "load": load_info, "topics": rows_for_print, "summary": summary})
        print(
            f"  sensitivity({profile_label}, axis={case.get('axis')}, value={case.get('value')}): "
            f"{'PASS' if passes else 'FAIL'} offered_bw={_format_bps(load_info.get('offered_bandwidth_bps'))} "
            f"{_format_topic_rows(rows_for_print)}"
        )

    sensitivity_path = out_dir / "sensitivity.jsonl"
    sensitivity_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    analysis = _sensitivity_analysis(rows)
    _write_benchmark_result(
        out_dir,
        genre="sensitivity",
        context=result_context,
        configuration={
            "cases": list(cases),
            "load": load_info,
            "thresholds": {
                "max_loss_pct": max_loss_pct,
                "max_latency_ms": max_latency_ms,
                "latency_quantile": thresholds.latency_quantile,
            },
            "topic": topic or None,
            "duration_s": duration_s,
            "generated_profiles_file": generated_profiles_file.name if generated_profiles_file else None,
        },
        result={"rows": rows, "analysis": analysis},
        measurements={"points": measurements},
        verdict={
            "passed": all(row["passes"] for row in rows),
            "status": "all_cases_passed" if all(row["passes"] for row in rows) else "case_failures",
        },
        artifacts={
            "sensitivity": sensitivity_path.name,
            "generated_profiles": generated_profiles_file.name if generated_profiles_file else None,
            "stdout": "stdout.txt",
            "probes_dir": "probes",
        },
    )
    print(f"Sensitivity rows saved to {sensitivity_path}")
    return rows


def drive_matrix(
    run_point: RunPointFn,
    *,
    cases: Sequence[dict[str, Any]],
    max_loss_pct: float,
    max_latency_ms: float,
    topic: str,
    out_dir: Path,
    result_context: dict[str, Any] | None = None,
    generated_profiles_file: Path | None = None,
) -> list[dict[str, Any]]:
    from .benchmark import OracleThresholds, oracle_passes_topic

    thresholds = OracleThresholds(max_loss_pct=max_loss_pct, max_latency_ms=max_latency_ms)
    rows: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []

    for case in cases:
        profile = _normalize_benchmark_profile(cast(str | None, case.get("profile")))
        profile_label = _benchmark_profile_label(profile)
        load = dict(cast(dict[str, Any], case.get("load", {})))
        load_info = _load_context(load)
        duration_s = float(case.get("duration_s", 0.0) or 0.0)
        summary = run_point(
            profile=profile,
            load=load,
            duration_s=duration_s,
            out_dir=out_dir,
            session_options=case.get("session_options"),
        )
        selected_topic, topic_data = _selected_topic(summary, topic)
        rows_for_print = _topic_rows(summary, topic)
        passes = oracle_passes_topic(topic_data, thresholds) if topic_data else False
        loss_pct = float(topic_data.get("loss_pct", 100.0)) if topic_data else 100.0
        latency_p95 = (topic_data.get("ota_hop_ms") or {}).get("p95") if topic_data else None
        jitter_p95 = (topic_data.get("jitter_ms") or {}).get("p95") if topic_data else None
        row = {
            "profile": profile_label,
            "axis": case.get("axis"),
            "value": case.get("value"),
            "description": case.get("description"),
            "qos": case.get("qos"),
            "duration_s": duration_s,
            "load": load_info,
            "passes": passes,
            "topic": selected_topic,
            "expected": topic_data.get("expected") if topic_data else None,
            "delivered": topic_data.get("delivered") if topic_data else None,
            "lost": topic_data.get("lost") if topic_data else None,
            "reordered": topic_data.get("reordered") if topic_data else None,
            "loss_pct": loss_pct,
            "latency_p95_ms": float(latency_p95) if latency_p95 is not None else None,
            "jitter_p95_ms": float(jitter_p95) if jitter_p95 is not None else None,
            "offered_bw_bps": float(load_info["offered_bandwidth_bps"] or 0.0),
        }
        rows.append(row)
        measurements.append({"case": case, "load": load_info, "topics": rows_for_print, "summary": summary})
        print(
            f"  matrix({case.get('axis')}={case.get('value')}, qos={case.get('qos')}, "
            f"duration={duration_s:g}s): {'PASS' if passes else 'FAIL'} "
            f"offered_bw={_format_bps(load_info.get('offered_bandwidth_bps'))} {_format_topic_rows(rows_for_print)}"
        )

    matrix_path = out_dir / "matrix.jsonl"
    matrix_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    analysis = _sensitivity_analysis(rows)
    _write_benchmark_result(
        out_dir,
        genre="matrix",
        context=result_context,
        configuration={
            "cases": list(cases),
            "thresholds": {
                "max_loss_pct": max_loss_pct,
                "max_latency_ms": max_latency_ms,
                "latency_quantile": thresholds.latency_quantile,
            },
            "topic": topic or None,
            "generated_profiles_file": generated_profiles_file.name if generated_profiles_file else None,
        },
        result={"rows": rows, "analysis": analysis},
        measurements={"points": measurements},
        verdict={
            "passed": all(row["passes"] for row in rows),
            "status": "all_cases_passed" if all(row["passes"] for row in rows) else "case_failures",
        },
        artifacts={
            "matrix": matrix_path.name,
            "generated_profiles": generated_profiles_file.name if generated_profiles_file else None,
            "stdout": "stdout.txt",
            "probes_dir": "probes",
        },
    )
    print(f"Matrix rows saved to {matrix_path}")
    return rows


# --------------------------------------------------------------------------- #
# Benchmark driver: requirements
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _RequirementCandidate:
    bandwidth_bps: float
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0


def _parse_requirements_axes(raw: str) -> list[str]:
    available = ("bandwidth", "latency", "jitter", "loss")
    aliases = {"rate": "bandwidth", "bw": "bandwidth", "delay": "latency"}
    axes = [aliases.get(value.strip().lower(), value.strip().lower()) for value in raw.split(",") if value.strip()]
    if not axes or "all" in axes:
        return list(available)
    unknown = sorted(set(axes) - set(available))
    if unknown:
        raise ValueError(f"Unknown requirements axes: {', '.join(unknown)}")
    return [axis for axis in available if axis in axes]


def _requirements_axis_value(candidate: _RequirementCandidate, axis: str) -> float:
    return {
        "bandwidth": candidate.bandwidth_bps,
        "latency": candidate.latency_ms,
        "jitter": candidate.jitter_ms,
        "jitter_loss": candidate.jitter_ms,
        "loss": candidate.loss_pct,
    }[axis]


def _requirements_with_axis(candidate: _RequirementCandidate, axis: str, value: float) -> _RequirementCandidate:
    updates = {
        "bandwidth_bps": candidate.bandwidth_bps,
        "latency_ms": candidate.latency_ms,
        "jitter_ms": candidate.jitter_ms,
        "loss_pct": candidate.loss_pct,
    }
    if axis == "bandwidth":
        updates["bandwidth_bps"] = value
    elif axis == "latency":
        updates["latency_ms"] = value
    elif axis == "jitter":
        updates["jitter_ms"] = value
    elif axis == "loss":
        updates["loss_pct"] = value
    else:
        raise ValueError(f"Unknown requirements axis {axis!r}.")
    return _RequirementCandidate(**updates)


def _requirements_candidate_context(candidate: _RequirementCandidate, *, downlink_ratio: float = 1.0) -> dict[str, Any]:
    return {
        "bandwidth_bps": candidate.bandwidth_bps,
        "bandwidth": _format_yaml_rate(candidate.bandwidth_bps),
        "uplink_latency_ms": candidate.latency_ms,
        "downlink_latency_ms": candidate.latency_ms * downlink_ratio,
        "jitter_ms": candidate.jitter_ms,
        "loss_pct": candidate.loss_pct,
    }


def _requirements_profile_spec(
    candidate: _RequirementCandidate,
    *,
    distribution: str,
    downlink_ratio: float,
) -> dict[str, Any]:
    latency_downlink_ms = candidate.latency_ms * downlink_ratio
    delay_uplink = candidate.latency_ms if candidate.latency_ms > 0.0 or candidate.jitter_ms > 0.0 else None
    delay_downlink = latency_downlink_ms if latency_downlink_ms > 0.0 or candidate.jitter_ms > 0.0 else None
    loss = candidate.loss_pct if candidate.loss_pct > 0.0 else None
    return {
        "uplink": _direction_to_yaml(
            rate=_format_yaml_rate(candidate.bandwidth_bps),
            delay_ms=delay_uplink,
            jitter_ms=candidate.jitter_ms if candidate.jitter_ms > 0.0 else None,
            distribution=distribution,
            loss_pct=loss,
        ),
        "downlink": _direction_to_yaml(
            rate=_format_yaml_rate(candidate.bandwidth_bps),
            delay_ms=delay_downlink,
            jitter_ms=candidate.jitter_ms if candidate.jitter_ms > 0.0 else None,
            distribution=distribution,
            loss_pct=loss,
        ),
    }


def _requirements_profile_name(prefix: str, index: int, axis: str, candidate: _RequirementCandidate) -> str:
    parts = [
        prefix,
        f"{index:03d}",
        axis,
        _safe_case_token(_format_yaml_rate(candidate.bandwidth_bps) or str(candidate.bandwidth_bps)),
        f"lat{candidate.latency_ms:g}ms",
        f"jit{candidate.jitter_ms:g}ms",
        f"loss{candidate.loss_pct:g}pct",
    ]
    return _safe_case_token("_".join(parts))


def _requirements_row_summary(row: dict[str, Any]) -> str:
    return (
        f"{row['axis']}[{row['phase']}] "
        f"bw={_format_bps(row['candidate']['bandwidth_bps'])} "
        f"lat={row['candidate']['uplink_latency_ms']:g}ms "
        f"jitter={row['candidate']['jitter_ms']:g}ms "
        f"loss={row['candidate']['loss_pct']:g}%"
    )


def _requirements_jitter_loss_pct(jitter_ms: float) -> float:
    """Practical jitter/loss coupling curve for mobile-ish shaped links."""
    points = (
        (0.0, 0.0),
        (10.0, 0.1),
        (15.0, 0.2),
        (20.0, 0.5),
        (30.0, 1.0),
        (50.0, 3.0),
    )
    if jitter_ms <= points[0][0]:
        return points[0][1]
    for (left_jitter, left_loss), (right_jitter, right_loss) in zip(points, points[1:], strict=False):
        if jitter_ms <= right_jitter:
            span = right_jitter - left_jitter
            fraction = (jitter_ms - left_jitter) / span if span else 0.0
            return left_loss + (right_loss - left_loss) * fraction
    last_jitter, last_loss = points[-1]
    prev_jitter, prev_loss = points[-2]
    slope = (last_loss - prev_loss) / (last_jitter - prev_jitter)
    return last_loss + (jitter_ms - last_jitter) * slope


def _requirements_target_quality(row: dict[str, Any], *, max_loss_pct: float, max_latency_ms: float) -> dict[str, Any]:
    loss_pct = row.get("loss_pct")
    latency_p95_ms = row.get("latency_p95_ms")
    loss_ratio: float | None
    if loss_pct is None:
        loss_ratio = None
    elif max_loss_pct > 0:
        loss_ratio = float(loss_pct) / max_loss_pct
    elif float(loss_pct) <= 0.0:
        loss_ratio = 0.0
    else:
        loss_ratio = math.inf
    latency_ratio = (
        float(latency_p95_ms) / max_latency_ms if latency_p95_ms is not None and max_latency_ms > 0 else None
    )
    known_ratios: dict[str, float] = {}
    if loss_ratio is not None:
        known_ratios["loss"] = loss_ratio
    if latency_ratio is not None:
        known_ratios["latency"] = latency_ratio
    limiting_metric = max(known_ratios, key=lambda key: known_ratios[key]) if known_ratios else None
    utilization = known_ratios[limiting_metric] if limiting_metric else None
    return {
        "loss_margin_pct": max_loss_pct - float(loss_pct) if loss_pct is not None else None,
        "latency_margin_ms": max_latency_ms - float(latency_p95_ms) if latency_p95_ms is not None else None,
        "loss_ratio": loss_ratio,
        "latency_ratio": latency_ratio,
        "utilization": utilization,
        "limiting_metric": limiting_metric,
    }


def _format_requirements_target_quality(quality: dict[str, Any]) -> str:
    utilization = quality.get("utilization")
    limiting = quality.get("limiting_metric") or "unknown"
    if utilization is None:
        return "target=unknown"
    if math.isinf(float(utilization)):
        return f"target=over-limit({limiting})"
    return f"target={utilization * 100:.1f}%({limiting})"


def _format_requirements_profile(candidate: _RequirementCandidate, *, downlink_ratio: float) -> str:
    downlink_latency_ms = candidate.latency_ms * downlink_ratio
    return (
        f"uplink(rate={_format_bps(candidate.bandwidth_bps)}, delay={candidate.latency_ms:g}ms, "
        f"jitter={candidate.jitter_ms:g}ms, loss={candidate.loss_pct:g}%), "
        f"downlink(rate={_format_bps(candidate.bandwidth_bps)}, delay={downlink_latency_ms:g}ms, "
        f"jitter={candidate.jitter_ms:g}ms, loss={candidate.loss_pct:g}%)"
    )


def drive_requirements(
    run_point: RunPointFn,
    *,
    max_loss_pct: float,
    max_latency_ms: float,
    rate_hz: float,
    size: int,
    streams: int,
    qos_reliability: str,
    qos_depth: int,
    topic: str,
    out_dir: Path,
    bandwidth_high_bps: float,
    bandwidth_low_bps: float,
    latency_high_ms: float,
    jitter_high_ms: float,
    loss_high_pct: float,
    axes: Sequence[str],
    min_duration_s: float,
    min_messages: int,
    search_iterations: int,
    search_rounds: int,
    distribution: str,
    final_refine_iterations: int = 2,
    loss_coupling: str = "jitter",
    downlink_ratio: float = 1.0,
    result_context: dict[str, Any] | None = None,
    generated_profiles_file: Path | None = None,
    profile_prefix: str = "requirements",
) -> dict[str, Any]:
    from .benchmark import OracleThresholds, oracle_passes_topic

    if bandwidth_low_bps <= 0.0 or bandwidth_high_bps <= 0.0:
        raise ValueError("Bandwidth bounds must be > 0.")
    if bandwidth_low_bps >= bandwidth_high_bps:
        raise ValueError("bandwidth-low must be smaller than bandwidth-high.")
    if search_iterations < 1:
        raise ValueError("search_iterations must be >= 1.")
    if search_rounds < 1:
        raise ValueError("search_rounds must be >= 1.")
    if final_refine_iterations < 0:
        raise ValueError("final_refine_iterations must be >= 0.")
    if loss_coupling not in {"independent", "jitter"}:
        raise ValueError("loss_coupling must be 'independent' or 'jitter'.")

    thresholds = OracleThresholds(max_loss_pct=max_loss_pct, max_latency_ms=max_latency_ms)
    requested_axes = list(axes)
    selected_axes = list(requested_axes)
    strict_zero_loss_target = max_loss_pct <= 0.0
    effective_loss_coupling = "zero_loss" if strict_zero_loss_target else loss_coupling
    skipped_axes: list[str] = []
    if strict_zero_loss_target:
        skipped_axes = [axis for axis in selected_axes if axis == "loss"]
        selected_axes = [axis for axis in selected_axes if axis != "loss"]
    elif loss_coupling == "jitter" and "jitter" in selected_axes and "loss" in selected_axes:
        selected_axes = ["jitter_loss" if axis == "jitter" else axis for axis in selected_axes if axis != "loss"]
    load = {"size_a": size, "rate": rate_hz, "streams": streams}
    load_info = _load_context(load)
    duration_s = _duration_for_min_messages(rate_hz, min_duration_s=min_duration_s, min_messages=min_messages)
    ideal = _RequirementCandidate(bandwidth_bps=bandwidth_high_bps)
    candidate = ideal
    rows: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    bounds: dict[str, Any] = {}
    generated_doc: dict[str, Any] = {
        "profiles": {},
        "metadata": {
            "kind": "benchmark-requirements-generated",
            "stream": {
                "rate_hz": rate_hz,
                "size_bytes": size,
                "qos": {"reliability": qos_reliability, "depth": qos_depth},
                "offered_bandwidth_bps": load_info.get("offered_bandwidth_bps"),
            },
            "thresholds": {
                "max_loss_pct": max_loss_pct,
                "max_latency_ms": max_latency_ms,
                "latency_quantile": thresholds.latency_quantile,
            },
            "search": {
                "requested_axes": requested_axes,
                "axes": selected_axes,
                "skipped_axes": skipped_axes,
                "bandwidth_low_bps": bandwidth_low_bps,
                "bandwidth_high_bps": bandwidth_high_bps,
                "latency_high_ms": latency_high_ms,
                "jitter_high_ms": jitter_high_ms,
                "loss_high_pct": loss_high_pct,
                "iterations": search_iterations,
                "rounds": search_rounds,
                "final_refine_iterations": final_refine_iterations,
                "distribution": distribution,
                "loss_coupling": loss_coupling,
                "effective_loss_coupling": effective_loss_coupling,
                "strict_zero_loss_target": strict_zero_loss_target,
                "downlink_ratio": downlink_ratio,
            },
        },
    }
    profile_counter = 0

    def write_profiles() -> None:
        if generated_profiles_file is not None:
            generated_profiles_file.write_text(yaml.safe_dump(generated_doc, sort_keys=False), encoding="utf-8")

    def probe(candidate_to_probe: _RequirementCandidate, *, axis: str, phase: str) -> dict[str, Any]:
        nonlocal profile_counter
        profile_counter += 1
        profile_name = _requirements_profile_name(profile_prefix, profile_counter, axis, candidate_to_probe)
        generated_doc["profiles"][profile_name] = _requirements_profile_spec(
            candidate_to_probe,
            distribution=distribution,
            downlink_ratio=downlink_ratio,
        )
        write_profiles()
        summary = run_point(profile=profile_name, load=load, duration_s=duration_s, out_dir=out_dir)
        selected_topic, topic_data = _selected_topic(summary, topic)
        rows_for_print = _topic_rows(summary, topic)
        passes = oracle_passes_topic(topic_data, thresholds) if topic_data else False
        loss_pct = float(topic_data.get("loss_pct", 100.0)) if topic_data else 100.0
        latency_p95 = (topic_data.get("ota_hop_ms") or {}).get("p95") if topic_data else None
        jitter_p95 = (topic_data.get("jitter_ms") or {}).get("p95") if topic_data else None
        row = {
            "profile": profile_name,
            "axis": axis,
            "phase": phase,
            "candidate": _requirements_candidate_context(candidate_to_probe, downlink_ratio=downlink_ratio),
            "passes": passes,
            "topic": selected_topic,
            "expected": topic_data.get("expected") if topic_data else None,
            "delivered": topic_data.get("delivered") if topic_data else None,
            "lost": topic_data.get("lost") if topic_data else None,
            "reordered": topic_data.get("reordered") if topic_data else None,
            "loss_pct": loss_pct,
            "latency_p95_ms": float(latency_p95) if latency_p95 is not None else None,
            "jitter_p95_ms": float(jitter_p95) if jitter_p95 is not None else None,
            "load": load_info,
            "duration_s": duration_s,
            "qos": {"reliability": qos_reliability, "depth": qos_depth},
        }
        target_quality = _requirements_target_quality(
            row,
            max_loss_pct=max_loss_pct,
            max_latency_ms=max_latency_ms,
        )
        row["target_quality"] = target_quality
        rows.append(row)
        measurements.append({"row": row, "topics": rows_for_print, "summary": summary})
        print(
            f"  requirements({_requirements_row_summary(row)}): {'PASS' if passes else 'FAIL'} "
            f"offered_bw={_format_bps(load_info.get('offered_bandwidth_bps'))} "
            f"{_format_requirements_target_quality(target_quality)} {_format_topic_rows(rows_for_print)}"
        )
        return row

    def last_row_ref(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "profile": row.get("profile"),
            "candidate": row.get("candidate"),
            "passes": row.get("passes"),
            "loss_pct": row.get("loss_pct"),
            "latency_p95_ms": row.get("latency_p95_ms"),
        }

    def candidate_at_axis(base: _RequirementCandidate, axis: str, value: float) -> _RequirementCandidate:
        effective_axis = "jitter" if axis == "jitter_loss" else axis
        candidate_at_value = _requirements_with_axis(base, effective_axis, value)
        if effective_loss_coupling == "jitter" and axis in {"jitter", "jitter_loss"}:
            modeled_loss = min(loss_high_pct, max(0.0, _requirements_jitter_loss_pct(value)))
            candidate_at_value = _requirements_with_axis(candidate_at_value, "loss", modeled_loss)
        return candidate_at_value

    def axis_ceiling(axis: str) -> float:
        return {
            "latency": latency_high_ms,
            "jitter": jitter_high_ms,
            "jitter_loss": jitter_high_ms,
            "loss": loss_high_pct,
        }[axis]

    def bound_axis_value(axis: str, bound: dict[str, Any] | None, ref_name: str, default: float) -> float:
        if not bound:
            return default
        ref = bound.get(ref_name)
        if not isinstance(ref, dict):
            return default
        candidate_ref = ref.get("candidate")
        if not isinstance(candidate_ref, dict):
            return default
        key = {
            "bandwidth": "bandwidth_bps",
            "latency": "uplink_latency_ms",
            "jitter": "jitter_ms",
            "jitter_loss": "jitter_ms",
            "loss": "loss_pct",
        }[axis]
        value = candidate_ref.get(key)
        return float(value) if value is not None else default

    def search_axis(
        axis: str,
        current: _RequirementCandidate,
        *,
        phase_label: str,
        iterations: int,
        low_value: float | None = None,
        high_value: float | None = None,
    ) -> tuple[_RequirementCandidate, dict[str, Any]]:
        if axis == "bandwidth":
            floor_value = low_value if low_value is not None else bandwidth_low_bps
            low_row = probe(
                candidate_at_axis(current, axis, floor_value),
                axis=axis,
                phase=f"{phase_label}:floor",
            )
            if low_row["passes"]:
                selected = candidate_at_axis(current, axis, floor_value)
                if floor_value > bandwidth_low_bps:
                    reset_row = probe(
                        candidate_at_axis(current, axis, bandwidth_low_bps),
                        axis=axis,
                        phase=f"{phase_label}:floor-reset",
                    )
                    if not reset_row["passes"]:
                        pass_candidate = selected
                        fail_candidate = candidate_at_axis(current, axis, bandwidth_low_bps)
                        rebound_pass_row: dict[str, Any] | None = low_row
                        rebound_fail_row: dict[str, Any] | None = reset_row
                        for iteration in range(iterations):
                            mid = math.sqrt(pass_candidate.bandwidth_bps * fail_candidate.bandwidth_bps)
                            mid_candidate = candidate_at_axis(current, axis, mid)
                            mid_row = probe(mid_candidate, axis=axis, phase=f"{phase_label}:rebound{iteration + 1}")
                            if mid_row["passes"]:
                                pass_candidate = mid_candidate
                                rebound_pass_row = mid_row
                            else:
                                fail_candidate = mid_candidate
                                rebound_fail_row = mid_row
                        return pass_candidate, {
                            "axis": axis,
                            "selected": _requirements_candidate_context(pass_candidate, downlink_ratio=downlink_ratio),
                            "tight": True,
                            "status": "bounded_after_floor_reset",
                            "search": "geometric",
                            "last_pass": last_row_ref(rebound_pass_row),
                            "last_fail": last_row_ref(rebound_fail_row),
                        }
                    selected = candidate_at_axis(current, axis, bandwidth_low_bps)
                    low_row = reset_row
                return selected, {
                    "axis": axis,
                    "selected": _requirements_candidate_context(selected, downlink_ratio=downlink_ratio),
                    "tight": False,
                    "status": "floor_still_passes",
                    "search": "geometric",
                    "last_pass": last_row_ref(low_row),
                    "last_fail": None,
                }
            pass_candidate = current
            fail_candidate = candidate_at_axis(current, axis, floor_value)
            pass_row: dict[str, Any] | None = None
            fail_row: dict[str, Any] | None = low_row
            for iteration in range(iterations):
                mid = math.sqrt(pass_candidate.bandwidth_bps * fail_candidate.bandwidth_bps)
                mid_candidate = candidate_at_axis(current, axis, mid)
                mid_row = probe(mid_candidate, axis=axis, phase=f"{phase_label}:bisect{iteration + 1}")
                if mid_row["passes"]:
                    pass_candidate = mid_candidate
                    pass_row = mid_row
                else:
                    fail_candidate = mid_candidate
                    fail_row = mid_row
            return pass_candidate, {
                "axis": axis,
                "selected": _requirements_candidate_context(pass_candidate, downlink_ratio=downlink_ratio),
                "tight": True,
                "status": "bounded",
                "search": "geometric",
                "last_pass": last_row_ref(pass_row),
                "last_fail": last_row_ref(fail_row),
            }

        ceiling_value = high_value if high_value is not None else axis_ceiling(axis)
        high_candidate = candidate_at_axis(current, axis, ceiling_value)
        high_row = probe(high_candidate, axis=axis, phase=f"{phase_label}:ceiling")
        if high_row["passes"]:
            return high_candidate, {
                "axis": axis,
                "selected": _requirements_candidate_context(high_candidate, downlink_ratio=downlink_ratio),
                "tight": False,
                "status": "ceiling_still_passes",
                "search": "linear",
                "last_pass": last_row_ref(high_row),
                "last_fail": None,
            }
        pass_candidate = current
        fail_candidate = high_candidate
        pass_row = None
        fail_row = high_row
        for iteration in range(iterations):
            mid = (
                _requirements_axis_value(pass_candidate, axis) + _requirements_axis_value(fail_candidate, axis)
            ) / 2.0
            mid_candidate = candidate_at_axis(current, axis, mid)
            mid_row = probe(mid_candidate, axis=axis, phase=f"{phase_label}:bisect{iteration + 1}")
            if mid_row["passes"]:
                pass_candidate = mid_candidate
                pass_row = mid_row
            else:
                fail_candidate = mid_candidate
                fail_row = mid_row
        return pass_candidate, {
            "axis": axis,
            "selected": _requirements_candidate_context(pass_candidate, downlink_ratio=downlink_ratio),
            "tight": True,
            "status": "bounded",
            "search": "linear",
            "last_pass": last_row_ref(pass_row),
            "last_fail": last_row_ref(fail_row),
        }

    baseline = probe(candidate, axis="baseline", phase="ideal")
    if not baseline["passes"]:
        requirements_path = out_dir / "requirements.jsonl"
        requirements_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
        analysis = {
            "status": "ideal_failed",
            "tight": False,
            "loss_coupling": loss_coupling,
            "effective_loss_coupling": effective_loss_coupling,
            "strict_zero_loss_target": strict_zero_loss_target,
            "requested_axes": requested_axes,
            "searched_axes": selected_axes,
            "skipped_axes": skipped_axes,
        }
        _write_benchmark_result(
            out_dir,
            genre="requirements",
            context=result_context,
            configuration={
                "load": load_info,
                "duration_s": duration_s,
                "thresholds": {
                    "max_loss_pct": max_loss_pct,
                    "max_latency_ms": max_latency_ms,
                    "latency_quantile": thresholds.latency_quantile,
                },
                "topic": topic or None,
                "requested_axes": requested_axes,
                "axes": selected_axes,
                "skipped_axes": skipped_axes,
                "bounds": {
                    "bandwidth_low_bps": bandwidth_low_bps,
                    "bandwidth_high_bps": bandwidth_high_bps,
                    "latency_high_ms": latency_high_ms,
                    "jitter_high_ms": jitter_high_ms,
                    "loss_high_pct": loss_high_pct,
                },
                "final_refine_iterations": final_refine_iterations,
                "loss_coupling": loss_coupling,
                "effective_loss_coupling": effective_loss_coupling,
                "strict_zero_loss_target": strict_zero_loss_target,
                "generated_profiles_file": generated_profiles_file.name if generated_profiles_file else None,
            },
            result={
                "stream": {"load": load_info, "qos": {"reliability": qos_reliability, "depth": qos_depth}},
                "profile": None,
                "bounds": {},
                "rows": rows,
                "analysis": analysis,
            },
            measurements={"points": measurements},
            verdict={"passed": False, "status": "ideal_failed"},
            artifacts={
                "requirements": requirements_path.name,
                "generated_profiles": generated_profiles_file.name if generated_profiles_file else None,
                "stdout": "stdout.txt",
                "probes_dir": "probes",
            },
        )
        return {"profile": None, "bounds": {}, "rows": rows, "analysis": analysis}

    for round_index in range(1, search_rounds + 1):
        for axis in selected_axes:
            candidate, bounds[axis] = search_axis(
                axis,
                candidate,
                phase_label=f"round{round_index}",
                iterations=search_iterations,
            )

    if final_refine_iterations:
        for axis in selected_axes:
            previous_bound = bounds.get(axis)
            candidate, bounds[axis] = search_axis(
                axis,
                candidate,
                phase_label="refine",
                iterations=final_refine_iterations,
                low_value=bound_axis_value(axis, previous_bound, "last_fail", bandwidth_low_bps)
                if axis == "bandwidth"
                else None,
                high_value=bound_axis_value(axis, previous_bound, "last_fail", axis_ceiling(axis))
                if axis != "bandwidth"
                else None,
            )

    final_profile_name = _safe_case_token(f"{profile_prefix}_final")
    generated_doc["profiles"][final_profile_name] = _requirements_profile_spec(
        candidate,
        distribution=distribution,
        downlink_ratio=downlink_ratio,
    )
    write_profiles()
    final_row = probe(candidate, axis="combined", phase="final")
    tight = bool(final_row["passes"]) and all(bounds.get(axis, {}).get("tight") for axis in selected_axes)
    unresolved = [axis for axis in selected_axes if not bounds.get(axis, {}).get("tight")]
    offered_bandwidth_bps = load_info.get("offered_bandwidth_bps")
    bandwidth_overhead_ratio = candidate.bandwidth_bps / float(offered_bandwidth_bps) if offered_bandwidth_bps else None
    final_profile = {
        "name": final_profile_name,
        "candidate": _requirements_candidate_context(candidate, downlink_ratio=downlink_ratio),
        "yaml": generated_doc["profiles"][final_profile_name],
    }
    analysis: dict[str, Any] = {
        "status": "tight" if tight else "not_tight",
        "tight": tight,
        "unresolved_axes": unresolved,
        "final_passes": final_row["passes"],
        "final_target_quality": final_row["target_quality"],
        "bandwidth_overhead_ratio": bandwidth_overhead_ratio,
        "search_rounds": search_rounds,
        "search_iterations": search_iterations,
        "final_refine_iterations": final_refine_iterations,
        "loss_coupling": loss_coupling,
        "effective_loss_coupling": effective_loss_coupling,
        "strict_zero_loss_target": strict_zero_loss_target,
        "requested_axes": requested_axes,
        "searched_axes": selected_axes,
        "skipped_axes": skipped_axes,
    }
    result: dict[str, Any] = {
        "stream": {
            "load": load_info,
            "qos": {"reliability": qos_reliability, "depth": qos_depth},
        },
        "target": {
            "max_loss_pct": max_loss_pct,
            "max_latency_ms": max_latency_ms,
            "latency_quantile": thresholds.latency_quantile,
        },
        "profile": final_profile,
        "bounds": bounds,
        "rows": rows,
        "analysis": analysis,
    }
    requirements_path = out_dir / "requirements.jsonl"
    requirements_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    _write_benchmark_result(
        out_dir,
        genre="requirements",
        context=result_context,
        configuration={
            "load": load_info,
            "duration_s": duration_s,
            "thresholds": {
                "max_loss_pct": max_loss_pct,
                "max_latency_ms": max_latency_ms,
                "latency_quantile": thresholds.latency_quantile,
            },
            "topic": topic or None,
            "requested_axes": requested_axes,
            "axes": selected_axes,
            "skipped_axes": skipped_axes,
            "bounds": {
                "bandwidth_low_bps": bandwidth_low_bps,
                "bandwidth_high_bps": bandwidth_high_bps,
                "latency_high_ms": latency_high_ms,
                "jitter_high_ms": jitter_high_ms,
                "loss_high_pct": loss_high_pct,
            },
            "final_refine_iterations": final_refine_iterations,
            "loss_coupling": loss_coupling,
            "effective_loss_coupling": effective_loss_coupling,
            "strict_zero_loss_target": strict_zero_loss_target,
            "generated_profiles_file": generated_profiles_file.name if generated_profiles_file else None,
        },
        result=result,
        measurements={"points": measurements},
        verdict={"passed": bool(final_row["passes"]), "status": str(analysis["status"])},
        artifacts={
            "requirements": requirements_path.name,
            "generated_profiles": generated_profiles_file.name if generated_profiles_file else None,
            "stdout": "stdout.txt",
            "probes_dir": "probes",
        },
    )
    print(f"Requirements rows saved to {requirements_path}")
    print(
        "Required network profile: "
        f"{_format_requirements_profile(candidate, downlink_ratio=downlink_ratio)} "
        f"{_format_requirements_target_quality(final_row['target_quality'])} "
        f"({'tight' if tight else 'not tight within configured bounds'})"
    )
    return result


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
    profiles_file = _benchmark_profiles_file(profile, runtime.profiles_file)
    if profiles_file:
        profiles_path = Path(profiles_file).resolve()
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


def _add_benchmark_common_args(parser: argparse.ArgumentParser, *, ota_benchmark: bool = False) -> None:
    from .cli import (
        OTA_DEFAULT_SUDO_MODE,
        OTA_SUDO_MODES,
        _add_common_config_args,
        _add_peer_address_arg,
        _add_peer_arg,
        _add_peer_ssh_arg,
    )

    _add_common_config_args(parser)
    _add_peer_arg(parser)
    _add_peer_ssh_arg(parser)
    _add_peer_address_arg(parser)
    parser.add_argument("--skip-preflight", action="store_true", help="Skip SSH/Docker readiness checks.")
    parser.add_argument("--keep-workdir", action="store_true", help="Keep temporary checkout directories.")
    parser.add_argument("--dry-run", action="store_true", help="Show commands but do not run them.")
    parser.add_argument("--artifacts-dir", help="Output directory for all benchmark artifacts.")
    parser.add_argument(
        "--target",
        help="OTA target session or scenario (default for OTA: the benchmark genre session).",
    )
    parser.add_argument(
        "--target-type",
        choices=["auto", "session", "scenario"],
        help=(
            "How to resolve --target for OTA benchmark runs "
            "(default: session when --target is omitted, auto otherwise)."
        ),
    )
    parser.add_argument("--workdir", help="Remote OTA workdir (default: /tmp/rosotacom_ota).")
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="Reuse already staged OTA source/project on remote hosts.",
    )
    parser.add_argument(
        "--sudo-mode",
        choices=OTA_SUDO_MODES,
        default=OTA_DEFAULT_SUDO_MODE,
        help=(
            "How OTA benchmark profile shaping obtains sudo for tc/ip "
            "(passwordless: require sudo -n; askpass: prompt locally per peer and feed sudo -S)."
        ),
    )
    parser.add_argument("--interactive", action="store_true", help="Open a tmux operator view for this benchmark run.")
    parser.add_argument(
        "--no-attach",
        action="store_true",
        help="Create the interactive tmux session without attaching.",
    )
    parser.add_argument(
        "--rmw",
        default=DEFAULT_BENCHMARK_RMW,
        help=f"RMW implementation or rosotacom RMW alias for benchmark sessions (default: {DEFAULT_BENCHMARK_RMW}).",
    )
    parser.add_argument(
        "--qos-reliability",
        choices=["best_effort", "reliable"],
        help="Override benchmark session OTA pub/sub reliability for the whole run.",
    )
    parser.add_argument(
        "--qos-depth",
        type=int,
        help="Override benchmark session QoS depth for the whole run.",
    )
    parser.set_defaults(ota_benchmark=ota_benchmark)


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
        _ota_network_sudo_passwords,
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

    is_ota = _is_ota_benchmark(args)

    def run_point(
        *,
        profile: str | None,
        load: dict[str, Any],
        duration_s: float,
        out_dir: Path,
    ) -> dict[str, Any]:
        if is_ota:
            # --- Real-near (ota-smoke) path ---
            # Default project to fzi_projects/remote_assist/rosotacom.yaml if not set.
            rosotacom_config = getattr(args, "rosotacom_config", None)
            if not rosotacom_config:
                candidate = Path.cwd() / "fzi_projects" / "remote_assist" / "rosotacom.yaml"
                if candidate.is_file():
                    args.rosotacom_config = str(candidate)
            target_name, target_type = _benchmark_ota_target(args, session_name)
            profiles_file = _benchmark_profiles_file(profile, _load_runtime_config(args).profiles_file)

            smoke_args = argparse.Namespace(
                rosotacom_config=getattr(args, "rosotacom_config", None),
                ros2docker_config=getattr(args, "ros2docker_config", None),
                session_configs_dir=getattr(args, "session_configs_dir", None),
                scenario_configs_dir=getattr(args, "scenario_configs_dir", None),
                session_instances_dir=getattr(args, "session_instances_dir", None),
                deployment=getattr(args, "deployment", None),
                profiles_file=str(profiles_file) if profiles_file else None,
                target=target_name,
                target_type=target_type,
                peer=getattr(args, "peer", None),
                peer_address=getattr(args, "peer_address", None),
                peer_ssh=getattr(args, "peer_ssh", None),
                workdir=getattr(args, "workdir", None),
                reuse=getattr(args, "reuse", False),
                skip_preflight=getattr(args, "skip_preflight", False),
                keep_workdir=getattr(args, "keep_workdir", False),
                dry_run=getattr(args, "dry_run", False),
                instance_id=getattr(args, "instance_id", None),
                profile=profile,
                benchmark_stepping=True,
                sudo_mode=getattr(args, "sudo_mode", "passwordless"),
            )

            runtime, plan, target = _resolve_ota_smoke_context(smoke_args)
            dry_run = smoke_args.dry_run
            _profile_name, profile_obj = _resolve_ota_profile(runtime, target, smoke_args)
            sudo_passwords = _ota_network_sudo_passwords(
                plan,
                sudo_mode=smoke_args.sudo_mode,
                require_network_shaping_sudo=profile_obj is not None,
                dry_run=dry_run,
            )

            if not smoke_args.skip_preflight:
                _ota_preflight(
                    plan,
                    require_tmux=target.target_type == "scenario",
                    check_peer_reachability=False,
                    dry_run=dry_run,
                    require_network_shaping_sudo=profile_obj is not None,
                    sudo_mode=smoke_args.sudo_mode,
                    sudo_passwords=sudo_passwords,
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
                                _peer_command_runner(
                                    peer, dry_run=dry_run, sudo_password=sudo_passwords.get(peer_name)
                                ),
                                control_interface=control_iface,
                                safety_max_duration_s=_PROFILE_SAFETY_MAX_S,
                                watchdog_launcher=_peer_watchdog_launcher(
                                    peer, dry_run=dry_run, sudo_password=sudo_passwords.get(peer_name)
                                ),
                            )
                            shapers.append(shaper)
                            steps = expand_timeline(profile_obj, ota_iface, direction=direction)
                            peer_steps[peer_name] = (shaper, steps)

                        for shaper in shapers:
                            shaper.arm([])
                    else:
                        shapers = _ota_arm_profile(
                            plan, profile_obj, directions, dry_run=dry_run, sudo_passwords=sudo_passwords
                        )

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

            profile_name = profile
            profile_obj = None
            profiles_file = _benchmark_profiles_file(profile_name, runtime.profiles_file)
            if profile_name and profiles_file:
                profiles = load_profiles_file(profiles_file)
                profile_obj = profiles.get(profile_name)
                if profile_obj is None:
                    raise ValueError(f"Profile {profile_name!r} not found in profiles file {profiles_file}.")

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

    if getattr(args, "interactive", False):
        return _start_interactive_benchmark(args, "capacity")

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

    if getattr(args, "interactive", False):
        return _start_interactive_benchmark(args, "ramp")

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

    if getattr(args, "interactive", False):
        return _start_interactive_benchmark(args, "recovery")

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir

    run_dir = _setup_benchmark_run_dir(args, "recovery", args.profile)
    session_context = _prepare_benchmark_session_config(args, "bench_1_4_recovery", run_dir)
    profiles_file = _benchmark_profiles_file(args.profile, runtime.profiles_file)
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
            profiles_file=profiles_file,
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

    if getattr(args, "interactive", False):
        return _start_interactive_benchmark(args, "sweep")

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


def benchmark_sensitivity(args: argparse.Namespace) -> int:
    """Handler for ``rosotacom benchmark sensitivity``."""
    from .cli import _load_runtime_config

    if getattr(args, "interactive", False):
        return _start_interactive_benchmark(args, "sensitivity")

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir
    base_profile = str(args.profile)

    run_dir = _setup_benchmark_run_dir(args, "sensitivity", base_profile)
    source_profiles_file = _benchmark_profiles_file(base_profile, runtime.profiles_file)
    if source_profiles_file is None:
        raise FileNotFoundError("sensitivity requires a profiles file containing the base profile.")

    generated_doc, cases = _build_sensitivity_profiles(
        base_profile=base_profile,
        profiles_file=source_profiles_file,
        ideal_rate=getattr(args, "ideal_rate", "1gbit"),
        loss_values=_parse_values(getattr(args, "loss_values", "0,0.5,1,3,5")),
        delay_values=_parse_values(getattr(args, "delay_values", "0,60,120,180")),
        jitter_values=_parse_values(getattr(args, "jitter_values", "0,10,30,50,80")),
        rate_values=_parse_rate_values(getattr(args, "rate_values", "1gbit,100mbit,10mbit,1mbit")),
        correlation_values=_parse_values(getattr(args, "correlation_values", "0,25,50,75")),
        axes=_parse_sensitivity_axes(getattr(args, "axes", "all")),
    )
    generated_profiles_file = run_dir / "generated-profiles.yaml"
    generated_profiles_file.write_text(yaml.safe_dump(generated_doc, sort_keys=False), encoding="utf-8")
    args.profiles_file = str(generated_profiles_file)

    session_name = BENCHMARK_SESSIONS_BY_GENRE["sensitivity"]
    session_context = _prepare_benchmark_session_config(args, session_name, run_dir)
    result_context = _benchmark_result_context(
        args,
        genre="sensitivity",
        profile=base_profile,
        run_dir=run_dir,
        session=session_context,
    )
    result_context["sensitivity"] = {
        "source_profiles_file": str(source_profiles_file),
        "generated_profiles_file": str(generated_profiles_file),
        "base_profile": base_profile,
    }

    run_point = getattr(args, "_test_run_point", None) or _make_live_run_point(args, session_name)
    with log_stdout_stderr_to_file(run_dir / "stdout.txt"):
        drive_sensitivity(
            run_point,
            cases=cases,
            max_loss_pct=getattr(args, "max_loss", 5.0),
            max_latency_ms=getattr(args, "max_latency_ms", 300.0),
            rate_hz=getattr(args, "rate_hz", 20.0),
            size=getattr(args, "size", 1),
            topic=getattr(args, "topic", ""),
            duration_s=getattr(args, "duration", 60.0),
            out_dir=run_dir,
            result_context=result_context,
            generated_profiles_file=generated_profiles_file,
        )

    out_file_name = getattr(args, "out", "sensitivity.jsonl")
    if artifacts_dir:
        out_path = artifacts_dir / Path(out_file_name).name
    else:
        out_path = Path(out_file_name)

    run_sensitivity_file = run_dir / "sensitivity.jsonl"
    if run_sensitivity_file.is_file():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_sensitivity_file, out_path)
        print(f"Sensitivity rows copied to {out_path}")

    return 0


def benchmark_matrix(args: argparse.Namespace) -> int:
    """Handler for ``rosotacom benchmark matrix``."""
    from .cli import _load_runtime_config

    if getattr(args, "interactive", False):
        return _start_interactive_benchmark(args, "matrix")

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir
    base_profile = str(args.profile)

    run_dir = _setup_benchmark_run_dir(args, "matrix", base_profile)
    source_profiles_file = _benchmark_profiles_file(base_profile, runtime.profiles_file)
    if source_profiles_file is None:
        raise FileNotFoundError("matrix requires a profiles file containing the base profile.")

    generated_doc, cases = _build_matrix_profiles(
        base_profile=base_profile,
        profiles_file=source_profiles_file,
        ideal_rate=getattr(args, "ideal_rate", "1gbit"),
        jitter_ms=float(getattr(args, "jitter_ms", 30.0)),
        latency_values=_parse_values(getattr(args, "latency_values", "30,60,100,140,180,220")),
        rate_hz_values=_parse_values(getattr(args, "rate_hz_values", "20,15,10,5,1")),
        qos_cases=_parse_qos_cases(getattr(args, "qos_cases", "best_effort:1,reliable:1,best_effort:10,reliable:10")),
        axes=_parse_matrix_axes(getattr(args, "axes", "all")),
        size=int(getattr(args, "size", 1)),
        fixed_rate_hz=float(getattr(args, "rate_hz", 20.0)),
        min_duration_s=float(getattr(args, "min_duration", 20.0)),
        min_messages=int(getattr(args, "min_messages", 100)),
    )
    generated_profiles_file = run_dir / "generated-profiles.yaml"
    generated_profiles_file.write_text(yaml.safe_dump(generated_doc, sort_keys=False), encoding="utf-8")
    args.profiles_file = str(generated_profiles_file)

    session_name = BENCHMARK_SESSIONS_BY_GENRE["matrix"]
    session_context = _prepare_benchmark_session_config(args, session_name, run_dir)
    result_context = _benchmark_result_context(
        args,
        genre="matrix",
        profile=base_profile,
        run_dir=run_dir,
        session=session_context,
    )
    result_context["matrix"] = {
        "source_profiles_file": str(source_profiles_file),
        "generated_profiles_file": str(generated_profiles_file),
        "base_profile": base_profile,
    }

    raw_run_point = getattr(args, "_test_run_point", None) or _make_live_run_point(args, session_name)

    def run_point_with_session_options(
        *,
        profile: str | None,
        load: dict[str, Any],
        duration_s: float,
        out_dir: Path,
        session_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = _activate_benchmark_session_options(args, session_name, out_dir, session_options)
        try:
            return raw_run_point(profile=profile, load=load, duration_s=duration_s, out_dir=out_dir)
        finally:
            if session_options:
                _restore_benchmark_session_options(args, previous)

    with log_stdout_stderr_to_file(run_dir / "stdout.txt"):
        drive_matrix(
            run_point_with_session_options,
            cases=cases,
            max_loss_pct=getattr(args, "max_loss", 5.0),
            max_latency_ms=getattr(args, "max_latency_ms", 300.0),
            topic=getattr(args, "topic", ""),
            out_dir=run_dir,
            result_context=result_context,
            generated_profiles_file=generated_profiles_file,
        )

    out_file_name = getattr(args, "out", "matrix.jsonl")
    if artifacts_dir:
        out_path = artifacts_dir / Path(out_file_name).name
    else:
        out_path = Path(out_file_name)

    run_matrix_file = run_dir / "matrix.jsonl"
    if run_matrix_file.is_file():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_matrix_file, out_path)
        print(f"Matrix rows copied to {out_path}")

    return 0


def benchmark_requirements(args: argparse.Namespace) -> int:
    """Handler for ``rosotacom benchmark requirements``."""
    from .cli import _load_runtime_config
    from .network_profiles import parse_rate_bps

    if getattr(args, "interactive", False):
        return _start_interactive_benchmark(args, "requirements")

    runtime = _load_runtime_config(args)
    artifacts_dir = Path(args.artifacts_dir) if getattr(args, "artifacts_dir", None) else runtime.benchmarks_dir
    run_dir = _setup_benchmark_run_dir(args, "requirements", None)
    generated_profiles_file = run_dir / "generated-profiles.yaml"
    generated_profiles_file.write_text(
        yaml.safe_dump(
            {
                "profiles": {},
                "metadata": {
                    "kind": "benchmark-requirements-generated",
                    "status": "initializing",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    args.profiles_file = str(generated_profiles_file)
    args.qos_reliability = getattr(args, "qos_reliability", None) or "best_effort"
    args.qos_depth = int(getattr(args, "qos_depth", None) or 1)

    session_name = BENCHMARK_SESSIONS_BY_GENRE["requirements"]
    session_context = _prepare_benchmark_session_config(args, session_name, run_dir)
    result_context = _benchmark_result_context(
        args,
        genre="requirements",
        profile=None,
        run_dir=run_dir,
        session=session_context,
    )
    result_context["requirements"] = {
        "generated_profiles_file": str(generated_profiles_file),
    }

    rate_hz = float(getattr(args, "rate_hz", 20.0))
    size = int(getattr(args, "size", 18_000))
    streams = int(getattr(args, "streams", 1))
    bandwidth_high_bps = parse_rate_bps(getattr(args, "bandwidth_high", "1gbit"))
    bandwidth_low_raw = getattr(args, "bandwidth_low", None)
    if bandwidth_low_raw:
        bandwidth_low_bps = parse_rate_bps(bandwidth_low_raw)
    else:
        offered_bw = (size * 8.0 * rate_hz * streams) if size > 0 and rate_hz > 0 else 1.0
        bandwidth_low_bps = max(1.0, offered_bw * float(getattr(args, "bandwidth_low_factor", 0.25)))

    run_point = getattr(args, "_test_run_point", None) or _make_live_run_point(args, session_name)
    with log_stdout_stderr_to_file(run_dir / "stdout.txt"):
        drive_requirements(
            run_point,
            max_loss_pct=float(getattr(args, "max_loss", 5.0)),
            max_latency_ms=float(getattr(args, "max_latency_ms", 250.0)),
            rate_hz=rate_hz,
            size=size,
            streams=streams,
            qos_reliability=str(args.qos_reliability),
            qos_depth=int(args.qos_depth),
            topic=getattr(args, "topic", ""),
            out_dir=run_dir,
            bandwidth_high_bps=bandwidth_high_bps,
            bandwidth_low_bps=bandwidth_low_bps,
            latency_high_ms=float(getattr(args, "latency_high_ms", 500.0)),
            jitter_high_ms=float(getattr(args, "jitter_high_ms", 120.0)),
            loss_high_pct=float(getattr(args, "loss_high", 20.0)),
            axes=_parse_requirements_axes(getattr(args, "axes", "all")),
            min_duration_s=float(getattr(args, "min_duration", 20.0)),
            min_messages=int(getattr(args, "min_messages", 100)),
            search_iterations=int(getattr(args, "search_iterations", 6)),
            search_rounds=int(getattr(args, "search_rounds", 1)),
            distribution=str(getattr(args, "distribution", "normal")),
            final_refine_iterations=int(getattr(args, "final_refine_iterations", 3)),
            loss_coupling=str(getattr(args, "loss_coupling", "jitter")),
            downlink_ratio=float(getattr(args, "downlink_ratio", 1.0)),
            result_context=result_context,
            generated_profiles_file=generated_profiles_file,
            profile_prefix=str(getattr(args, "profile_prefix", "requirements")),
        )

    out_file_name = getattr(args, "out", "requirements.jsonl")
    if artifacts_dir:
        out_path = artifacts_dir / Path(out_file_name).name
    else:
        out_path = Path(out_file_name)

    run_requirements_file = run_dir / "requirements.jsonl"
    if run_requirements_file.is_file():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_requirements_file, out_path)
        print(f"Requirements rows copied to {out_path}")

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


def _register_benchmark_driver_parsers(benchmark_subparsers: Any, *, ota_benchmark: bool) -> None:
    # --- capacity ---
    cap_parser = benchmark_subparsers.add_parser(
        "capacity",
        help="Binary-search for the capacity breakpoint under a profile.",
    )
    _add_benchmark_common_args(cap_parser, ota_benchmark=ota_benchmark)
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
    _add_benchmark_common_args(ramp_parser, ota_benchmark=ota_benchmark)
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
    _add_benchmark_common_args(rec_parser, ota_benchmark=ota_benchmark)
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
    _add_benchmark_common_args(sweep_parser, ota_benchmark=ota_benchmark)
    sweep_parser.add_argument("--profile-grid", required=True, help="Comma-separated list of profile names to sweep.")
    sweep_parser.add_argument("--max-loss", type=float, default=5.0, help="Oracle: max loss %%.")
    sweep_parser.add_argument("--max-latency-ms", type=float, default=300.0, help="Oracle: max p95 latency (ms).")
    sweep_parser.add_argument("--rate-hz", type=float, default=20.0, help="Publish rate (Hz).")
    sweep_parser.add_argument("--size", type=int, default=60000, help="Payload size (bytes).")
    sweep_parser.add_argument("--topic", default="", help="Topic to evaluate.")
    sweep_parser.add_argument("--duration", type=float, default=60.0, help="Seconds per sweep point.")
    sweep_parser.add_argument("--out", default="frontier.jsonl", help="Output frontier JSONL file.")
    sweep_parser.set_defaults(func=benchmark_sweep)

    # --- sensitivity ---
    sensitivity_parser = benchmark_subparsers.add_parser(
        "sensitivity",
        help="Run a generated profile lab to isolate loss, delay, jitter, rate, and loss-correlation effects.",
    )
    _add_benchmark_common_args(sensitivity_parser, ota_benchmark=ota_benchmark)
    sensitivity_parser.add_argument("--profile", required=True, help="Static base profile to investigate.")
    sensitivity_parser.add_argument("--max-loss", type=float, default=5.0, help="Oracle: max loss %%.")
    sensitivity_parser.add_argument("--max-latency-ms", type=float, default=300.0, help="Oracle: max p95 latency (ms).")
    sensitivity_parser.add_argument("--rate-hz", type=float, default=20.0, help="Publish rate (Hz).")
    sensitivity_parser.add_argument("--size", type=int, default=1, help="Payload size (bytes).")
    sensitivity_parser.add_argument("--topic", default="", help="Topic to evaluate.")
    sensitivity_parser.add_argument("--duration", type=float, default=60.0, help="Seconds per sensitivity point.")
    sensitivity_parser.add_argument("--ideal-rate", default="1gbit", help="High-quality shaped-link rate.")
    sensitivity_parser.add_argument(
        "--loss-values",
        default="0,0.5,1,3,5",
        help="Comma/range list of loss percentages for the loss-only axis.",
    )
    sensitivity_parser.add_argument(
        "--delay-values",
        default="0,60,120,180",
        help="Comma/range list of fixed delays in milliseconds for the delay-only axis.",
    )
    sensitivity_parser.add_argument(
        "--jitter-values",
        default="0,10,30,50,80",
        help="Comma/range list of jitter values in milliseconds around the base mean delay.",
    )
    sensitivity_parser.add_argument(
        "--rate-values",
        default="1gbit,100mbit,10mbit,1mbit",
        help="Comma list of rate tokens for the rate-only axis.",
    )
    sensitivity_parser.add_argument(
        "--correlation-values",
        default="0,25,50,75",
        help="Comma/range list of loss-correlation percentages at the base loss rate.",
    )
    sensitivity_parser.add_argument(
        "--axes",
        default="all",
        help="Comma list of sensitivity axes to run: all, loss, delay, jitter, rate, loss-correlation.",
    )
    sensitivity_parser.add_argument("--out", default="sensitivity.jsonl", help="Output sensitivity JSONL file.")
    sensitivity_parser.set_defaults(func=benchmark_sensitivity)

    # --- matrix ---
    matrix_parser = benchmark_subparsers.add_parser(
        "matrix",
        help="Run a fixed-jitter matrix over latency, publish frequency, and QoS settings.",
    )
    _add_benchmark_common_args(matrix_parser, ota_benchmark=ota_benchmark)
    matrix_parser.add_argument("--profile", required=True, help="Static base profile whose latency ratio is reused.")
    matrix_parser.add_argument("--max-loss", type=float, default=5.0, help="Oracle: max loss %%.")
    matrix_parser.add_argument("--max-latency-ms", type=float, default=300.0, help="Oracle: max p95 latency (ms).")
    matrix_parser.add_argument("--rate-hz", type=float, default=20.0, help="Fixed publish rate for non-Hz axes.")
    matrix_parser.add_argument("--size", type=int, default=1, help="Payload size (bytes).")
    matrix_parser.add_argument("--topic", default="", help="Topic to evaluate.")
    matrix_parser.add_argument("--ideal-rate", default="1gbit", help="High-quality shaped-link rate.")
    matrix_parser.add_argument("--jitter-ms", type=float, default=30.0, help="Fixed jitter in milliseconds.")
    matrix_parser.add_argument(
        "--latency-values",
        default="30,60,100,140,180,220",
        help="Comma/range list of uplink mean delays in milliseconds; downlink preserves the base profile ratio.",
    )
    matrix_parser.add_argument(
        "--rate-hz-values",
        default="20,15,10,5,1",
        help="Comma/range list of publish frequencies for the Hz axis.",
    )
    matrix_parser.add_argument(
        "--qos-cases",
        default="best_effort:1,reliable:1,best_effort:10,reliable:10",
        help="Comma list of QoS cases as reliability:depth.",
    )
    matrix_parser.add_argument(
        "--axes",
        default="all",
        help="Comma list of matrix axes to run: all, latency, hz, qos.",
    )
    matrix_parser.add_argument(
        "--min-duration",
        type=float,
        default=20.0,
        help="Minimum seconds per matrix point.",
    )
    matrix_parser.add_argument(
        "--min-messages",
        type=int,
        default=100,
        help="Minimum intended published messages per matrix point.",
    )
    matrix_parser.add_argument("--out", default="matrix.jsonl", help="Output matrix JSONL file.")
    matrix_parser.set_defaults(func=benchmark_matrix)

    # --- requirements ---
    requirements_parser = benchmark_subparsers.add_parser(
        "requirements",
        help="Search for a tight network profile that satisfies a ROS 2 stream quality target.",
    )
    _add_benchmark_common_args(requirements_parser, ota_benchmark=ota_benchmark)
    requirements_parser.add_argument("--max-loss", type=float, default=5.0, help="Oracle: max ROS 2 loss %%.")
    requirements_parser.add_argument(
        "--max-latency-ms", type=float, default=250.0, help="Oracle: max acceptable p95 latency (ms)."
    )
    requirements_parser.add_argument("--rate-hz", type=float, default=20.0, help="ROS 2 publish rate (Hz).")
    requirements_parser.add_argument("--size", type=int, default=18_000, help="Payload size (bytes).")
    requirements_parser.add_argument("--streams", type=int, default=1, help="Parallel stream count.")
    requirements_parser.add_argument("--topic", default="", help="Topic to evaluate.")
    requirements_parser.add_argument("--bandwidth-high", default="1gbit", help="Known-good search bandwidth ceiling.")
    requirements_parser.add_argument(
        "--bandwidth-low",
        help="Search bandwidth floor; default is offered bandwidth times --bandwidth-low-factor.",
    )
    requirements_parser.add_argument(
        "--bandwidth-low-factor",
        type=float,
        default=0.25,
        help="Default bandwidth floor as a factor of offered stream bandwidth.",
    )
    requirements_parser.add_argument(
        "--latency-high-ms",
        type=float,
        default=500.0,
        help="Worst latency ceiling to try while searching.",
    )
    requirements_parser.add_argument(
        "--jitter-high-ms",
        type=float,
        default=120.0,
        help="Worst jitter ceiling to try while searching.",
    )
    requirements_parser.add_argument(
        "--loss-high",
        type=float,
        default=20.0,
        help="Worst network loss percentage to try while searching.",
    )
    requirements_parser.add_argument(
        "--axes",
        default="all",
        help="Comma list of network axes to tighten: all, bandwidth, latency, jitter, loss.",
    )
    requirements_parser.add_argument(
        "--search-iterations",
        type=int,
        default=6,
        help="Binary-search probes per axis and round.",
    )
    requirements_parser.add_argument(
        "--search-rounds",
        type=int,
        default=1,
        help="Coordinate-search rounds; more rounds account for knob interactions better.",
    )
    requirements_parser.add_argument(
        "--final-refine-iterations",
        type=int,
        default=3,
        help="Extra bounded refinement probes per axis after the coordinate search.",
    )
    requirements_parser.add_argument(
        "--loss-coupling",
        choices=["jitter", "independent"],
        default="jitter",
        help=(
            "Use a practical jitter/loss tradeoff curve or search network loss as its own independent axis. "
            "For --max-loss 0, generated network loss is clamped to 0 and jitter is searched separately."
        ),
    )
    requirements_parser.add_argument(
        "--min-duration",
        type=float,
        default=20.0,
        help="Minimum seconds per probe.",
    )
    requirements_parser.add_argument(
        "--min-messages",
        type=int,
        default=100,
        help="Minimum intended published messages per probe.",
    )
    requirements_parser.add_argument(
        "--distribution",
        default="normal",
        choices=["normal", "pareto", "paretonormal"],
        help="netem jitter distribution.",
    )
    requirements_parser.add_argument(
        "--downlink-ratio",
        type=float,
        default=1.0,
        help="Downlink latency as a ratio of uplink latency in generated profiles.",
    )
    requirements_parser.add_argument(
        "--profile-prefix",
        default="requirements",
        help="Prefix for generated profile names.",
    )
    requirements_parser.add_argument("--out", default="requirements.jsonl", help="Output requirements JSONL file.")
    requirements_parser.set_defaults(func=benchmark_requirements)


def _register_benchmark_plot_parser(benchmark_subparsers: Any) -> None:
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


def register_benchmark_parser(subparsers: Any) -> None:
    """Register the ``benchmark`` and ``ota-benchmark`` commands."""
    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Run local benchmark tests and render plots.",
    )
    benchmark_subparsers = benchmark_parser.add_subparsers(dest="benchmark_command", required=True)
    _register_benchmark_driver_parsers(benchmark_subparsers, ota_benchmark=False)
    _register_benchmark_plot_parser(benchmark_subparsers)

    ota_parser = subparsers.add_parser(
        "ota-benchmark",
        help="Run OTA benchmark tests against deployment peers.",
    )
    ota_subparsers = ota_parser.add_subparsers(dest="benchmark_command", required=True)
    _register_benchmark_driver_parsers(ota_subparsers, ota_benchmark=True)
