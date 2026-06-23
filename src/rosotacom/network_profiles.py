"""RFC 0004 — network profiles: schema + ``tc``/``netem`` command generation.

The environment axis of the fidelity ladder (RFC 0004): a declarative, named,
per-direction `tc`/`netem` condition applied *below* rosotacom on the OTA
interface, so the transport sees a realistic pipe and is unaware of it.

This module is the **pure, host-testable** half — everything that can be exercised
without a privileged interface:

* the profile **schema** (static + timeline, per-direction parameters);
* the **argv generation** (a profile/direction → the exact `tc`/`netem` command
  list), so a host test asserts the built command without touching a real `qdisc`;
* the **outage kinds** (`catchup` = `loss 100%` with the interface up, vs
  ``reconnect`` = link-down forcing RMW re-discovery — the two recovery semantics
  RFC 0004 left as an open question, resolved here as named variants);
* **timeline expansion** (ordered segments + outage → a stepping schedule).

The *privileged* half — actually arming/tearing down on a real interface with the
fail-safe revert — lives next to the orchestration that owns the SSH/staging path,
and consumes the argv this module builds.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Outage kinds — the two recovery semantics (RFC 0004 open question, resolved)
# --------------------------------------------------------------------------- #

OUTAGE_CATCHUP = "catchup"  # netem loss 100%, interface stays up — DDS survives, "catch up"
OUTAGE_RECONNECT = "reconnect"  # link-down — forces RMW re-discovery, the harsher reconnect
OUTAGE_KINDS = (OUTAGE_CATCHUP, OUTAGE_RECONNECT)

# Default token-bucket buffer/latency, mirroring the calibrated baseline profile
# (`tbf rate <r> burst 32kbit latency 100ms`); overridable per direction.
_DEFAULT_TBF_BURST = "32kbit"
_DEFAULT_TBF_LATENCY_MS = 100.0


# --------------------------------------------------------------------------- #
# Value parsing (human strings → normalized numbers)
# --------------------------------------------------------------------------- #


def _num(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what}: expected a number, got {value!r}") from exc


def parse_rate_bps(value: Any) -> float:
    """``"4mbit"`` / ``"500kbit"`` / ``2.5e6`` → bits per second.

    Bit-oriented (rosotacom profiles are stated in bit/s, like the operator's
    baseline); a bare number is taken as bit/s. Byte suffixes are rejected to keep
    the unit unambiguous on the wire.
    """
    if isinstance(value, (int, float)):
        rate = float(value)
    else:
        text = str(value).strip().lower()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(g|m|k)?bit", text) or re.fullmatch(r"(\d+(?:\.\d+)?)", text)
        if not match:
            raise ValueError(f"rate: expected e.g. '4mbit', '500kbit' or a bit/s number, got {value!r}")
        scale = {"k": 1e3, "m": 1e6, "g": 1e9, None: 1.0}[match.group(2) if match.lastindex == 2 else None]
        rate = float(match.group(1)) * scale
    if rate <= 0:
        raise ValueError(f"rate must be > 0, got {value!r}")
    return rate


def parse_ms(value: Any, what: str = "duration") -> float:
    """``"120ms"`` / ``"0.18s"`` / ``120`` → milliseconds (a bare number is ms)."""
    if isinstance(value, (int, float)):
        ms = float(value)
    else:
        text = str(value).strip().lower()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s)?", text)
        if not match:
            raise ValueError(f"{what}: expected e.g. '120ms' or '0.5s', got {value!r}")
        ms = float(match.group(1)) * (1000.0 if match.group(2) == "s" else 1.0)
    if ms < 0:
        raise ValueError(f"{what} must be >= 0, got {value!r}")
    return ms


def parse_seconds(value: Any, what: str = "duration") -> float:
    """``"30s"`` / ``"500ms"`` / ``30`` → seconds (a bare number is seconds)."""
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().lower()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s)?", text)
        if not match:
            raise ValueError(f"{what}: expected e.g. '30s' or '500ms', got {value!r}")
        seconds = float(match.group(1)) / 1000.0 if match.group(2) == "ms" else float(match.group(1))
    if seconds <= 0:
        raise ValueError(f"{what} must be > 0, got {value!r}")
    return seconds


def parse_pct(value: Any, what: str = "percentage") -> float:
    """``"2%"`` / ``2`` → percent (0–100)."""
    text = str(value).strip().rstrip("%") if isinstance(value, str) else value
    pct = _num(text, what)
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"{what} must be within [0, 100]%, got {value!r}")
    return pct


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DirectionShaping:
    """`tc`/`netem` parameters for one direction's egress. Every field optional;
    ``None`` means "not applied". An all-``None`` instance shapes nothing."""

    rate_bps: float | None = None
    delay_ms: float | None = None
    jitter_ms: float | None = None
    distribution: str | None = None  # normal | pareto | paretonormal (needs jitter)
    loss_pct: float | None = None
    loss_correlation_pct: float | None = None  # netem correlated loss (needs loss)
    reorder_pct: float | None = None  # netem reorder (needs delay)
    duplicate_pct: float | None = None
    burst: str = _DEFAULT_TBF_BURST  # tbf buffer, only used when rate_bps is set
    tbf_latency_ms: float = _DEFAULT_TBF_LATENCY_MS

    @property
    def is_empty(self) -> bool:
        return all(
            getattr(self, name) is None
            for name in (
                "rate_bps",
                "delay_ms",
                "jitter_ms",
                "loss_pct",
                "reorder_pct",
                "duplicate_pct",
            )
        )

    @property
    def has_netem(self) -> bool:
        return any(
            getattr(self, name) is not None
            for name in ("delay_ms", "jitter_ms", "loss_pct", "reorder_pct", "duplicate_pct")
        )

    def __post_init__(self) -> None:
        if self.jitter_ms is not None and self.delay_ms is None:
            raise ValueError("jitter requires a delay")
        if self.distribution is not None and self.jitter_ms is None:
            raise ValueError("distribution requires a jitter")
        if self.loss_correlation_pct is not None and self.loss_pct is None:
            raise ValueError("loss_correlation requires a loss")
        if self.reorder_pct is not None and self.delay_ms is None:
            raise ValueError("reorder requires a delay (netem reorders within the delay window)")
        if self.distribution is not None and self.distribution not in ("normal", "pareto", "paretonormal"):
            raise ValueError(f"unsupported distribution {self.distribution!r}")


@dataclass(frozen=True)
class TimelineSegment:
    """One ordered step of a timeline profile: hold ``shaping`` (per direction) for
    ``for_s`` seconds, or apply an ``outage`` of one of the named kinds."""

    for_s: float
    uplink: DirectionShaping | None = None
    downlink: DirectionShaping | None = None
    outage: str | None = None  # None | OUTAGE_CATCHUP | OUTAGE_RECONNECT

    def __post_init__(self) -> None:
        if self.outage is not None and self.outage not in OUTAGE_KINDS:
            raise ValueError(f"outage must be one of {OUTAGE_KINDS}, got {self.outage!r}")
        if self.outage is not None and (self.uplink is not None or self.downlink is not None):
            raise ValueError("an outage segment cannot also carry direction shaping")


@dataclass(frozen=True)
class Profile:
    """A named environment spec: either ``static`` (constant per-direction shaping)
    or ``timeline`` (ordered segments — the substrate for the recovery genre)."""

    name: str
    kind: str  # "static" | "timeline"
    uplink: DirectionShaping | None = None
    downlink: DirectionShaping | None = None
    timeline: Sequence[TimelineSegment] = field(default_factory=tuple)

    @property
    def is_timeline(self) -> bool:
        return self.kind == "timeline"

    @property
    def total_duration_s(self) -> float | None:
        return sum(segment.for_s for segment in self.timeline) if self.is_timeline else None


# --------------------------------------------------------------------------- #
# Schema parsing (profiles.yaml → Profile objects)
# --------------------------------------------------------------------------- #

_DIRECTION_KEYS = frozenset(
    {
        "rate",
        "delay",
        "jitter",
        "distribution",
        "loss",
        "loss_correlation",
        "reorder",
        "duplicate",
        "burst",
        "tbf_latency",
    }
)


def parse_direction(spec: Mapping[str, Any] | None) -> DirectionShaping | None:
    """Parse one ``uplink``/``downlink`` block into a :class:`DirectionShaping`."""
    if spec is None:
        return None
    unknown = sorted(set(spec) - _DIRECTION_KEYS)
    if unknown:
        raise ValueError(f"unsupported direction keys {unknown}; allowed: {sorted(_DIRECTION_KEYS)}")
    return DirectionShaping(
        rate_bps=parse_rate_bps(spec["rate"]) if "rate" in spec else None,
        delay_ms=parse_ms(spec["delay"], "delay") if "delay" in spec else None,
        jitter_ms=parse_ms(spec["jitter"], "jitter") if "jitter" in spec else None,
        distribution=str(spec["distribution"]) if "distribution" in spec else None,
        loss_pct=parse_pct(spec["loss"], "loss") if "loss" in spec else None,
        loss_correlation_pct=(
            parse_pct(spec["loss_correlation"], "loss_correlation") if "loss_correlation" in spec else None
        ),
        reorder_pct=parse_pct(spec["reorder"], "reorder") if "reorder" in spec else None,
        duplicate_pct=parse_pct(spec["duplicate"], "duplicate") if "duplicate" in spec else None,
        burst=str(spec.get("burst", _DEFAULT_TBF_BURST)),
        tbf_latency_ms=parse_ms(spec["tbf_latency"], "tbf_latency")
        if "tbf_latency" in spec
        else _DEFAULT_TBF_LATENCY_MS,
    )


def _parse_outage(value: Any) -> str:
    """``outage: catchup|reconnect`` (a bare ``true`` defaults to the milder ``catchup``)."""
    if value is True:
        return OUTAGE_CATCHUP
    kind = str(value).strip().lower()
    if kind not in OUTAGE_KINDS:
        raise ValueError(f"outage must be one of {OUTAGE_KINDS} (or true → {OUTAGE_CATCHUP}), got {value!r}")
    return kind


def parse_timeline_segment(spec: Mapping[str, Any]) -> TimelineSegment:
    unknown = sorted(set(spec) - {"for", "outage", "uplink", "downlink"})
    if unknown:
        raise ValueError(f"unsupported timeline-segment keys {unknown}; use for/outage/uplink/downlink")
    if "for" not in spec:
        raise ValueError("a timeline segment requires a 'for' duration")
    for_s = parse_seconds(spec["for"], "for")
    if "outage" in spec:
        if "uplink" in spec or "downlink" in spec:
            raise ValueError("an outage segment cannot also carry direction shaping")
        return TimelineSegment(for_s=for_s, outage=_parse_outage(spec["outage"]))
    return TimelineSegment(
        for_s=for_s,
        uplink=parse_direction(spec.get("uplink")),
        downlink=parse_direction(spec.get("downlink")),
    )


def parse_profile(name: str, spec: Mapping[str, Any]) -> Profile:
    """Parse one named profile block (static or timeline)."""
    if "timeline" in spec:
        unknown = sorted(set(spec) - {"timeline"})
        if unknown:
            raise ValueError(f"profile {name!r}: a timeline profile cannot also have {unknown}")
        segments = [parse_timeline_segment(segment) for segment in spec["timeline"]]
        if not segments:
            raise ValueError(f"profile {name!r}: timeline must have at least one segment")
        return Profile(name=name, kind="timeline", timeline=tuple(segments))
    unknown = sorted(set(spec) - {"uplink", "downlink"})
    if unknown:
        raise ValueError(f"profile {name!r}: unsupported keys {unknown}; use 'uplink'/'downlink' or 'timeline'")
    return Profile(
        name=name,
        kind="static",
        uplink=parse_direction(spec.get("uplink")),
        downlink=parse_direction(spec.get("downlink")),
    )


def parse_profiles(doc: Mapping[str, Any]) -> dict[str, Profile]:
    """Parse a ``profiles.yaml`` document (``{profiles: {<name>: <spec>}}``)."""
    profiles_block = doc.get("profiles", doc) if isinstance(doc, Mapping) else {}
    if not isinstance(profiles_block, Mapping):
        raise ValueError("profiles.yaml: top-level 'profiles' must be a mapping of name → spec")
    return {str(name): parse_profile(str(name), spec or {}) for name, spec in profiles_block.items()}


def load_profiles_file(path: str | Path) -> dict[str, Profile]:
    """Load a project-scoped ``profiles.yaml`` into ``{name: Profile}``."""
    import yaml

    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(doc, Mapping):
        raise ValueError(f"{path}: profiles file must be a mapping (with a top-level 'profiles:' block)")
    return parse_profiles(doc)


# Selecting nothing — the unshaped rung (0/1) or the "no emulation" sentinel.
PROFILE_NONE = "none"


def resolve_profile_selection(
    requested: str | None,
    *,
    shared_default: str | None = None,
    available: Collection[str] | None = None,
    allow_shaping: bool = True,
) -> str | None:
    """Resolve the active profile name (RFC 0004 selection rule).

    ``--profile <name>`` wins; otherwise ``shared.profile`` is the default; ``none``
    (or nothing) is the unshaped rung → ``None``. A requested name is validated
    against ``available`` when a profiles file is configured (catching typos). A
    profile selected on a run that may not be shaped — the real fleet, where the link
    *is* the condition and is measured, never imposed — is an error."""
    name = requested if requested is not None else shared_default
    if name is None or name == PROFILE_NONE:
        return None
    if not allow_shaping:
        raise ValueError(
            f"profile {name!r} selected, but this run may not be shaped: the real fleet's link is the "
            "condition and is measured, never imposed (RFC 0004). Use --profile none."
        )
    if available is not None and name not in available:
        raise ValueError(f"unknown profile {name!r}; configured profiles: {sorted(available)}")
    return name


# --------------------------------------------------------------------------- #
# tc / netem command generation (argv lists — no shell)
# --------------------------------------------------------------------------- #


def _ms(value: float) -> str:
    return f"{value:g}ms"


def _pct(value: float) -> str:
    return f"{value:g}%"


def _netem_args(shaping: DirectionShaping) -> list[str]:
    """The netem parameter tokens (delay/jitter/distribution, loss, duplicate, reorder).

    Ordered so each modifier follows the base it qualifies (netem requires
    ``delay`` before ``reorder``, and the correlation value follows its base loss).
    """
    args: list[str] = []
    if shaping.delay_ms is not None:
        args += ["delay", _ms(shaping.delay_ms)]
        if shaping.jitter_ms is not None:
            args.append(_ms(shaping.jitter_ms))
            if shaping.distribution is not None:
                args += ["distribution", shaping.distribution]
    if shaping.loss_pct is not None:
        args += ["loss", _pct(shaping.loss_pct)]
        if shaping.loss_correlation_pct is not None:
            args.append(_pct(shaping.loss_correlation_pct))
    if shaping.duplicate_pct is not None:
        args += ["duplicate", _pct(shaping.duplicate_pct)]
    if shaping.reorder_pct is not None:
        args += ["reorder", _pct(shaping.reorder_pct)]
    return args


def shaping_commands(interface: str, shaping: DirectionShaping) -> list[list[str]]:
    """The ordered ``tc`` argv list that arms ``shaping`` on ``interface`` egress.

    Layout mirrors the calibrated baseline: a ``tbf`` root for rate limiting with a
    child ``netem`` for delay/jitter/loss. When only one of the two is requested it
    becomes the root qdisc; an empty shaping arms nothing.
    """
    if shaping.is_empty:
        return []
    commands: list[list[str]] = []
    if shaping.rate_bps is not None:
        commands.append(
            [
                "tc",
                "qdisc",
                "add",
                "dev",
                interface,
                "root",
                "handle",
                "1:",
                "tbf",
                "rate",
                f"{int(round(shaping.rate_bps))}bit",
                "burst",
                shaping.burst,
                "latency",
                _ms(shaping.tbf_latency_ms),
            ]
        )
        if shaping.has_netem:
            commands.append(
                [
                    "tc",
                    "qdisc",
                    "add",
                    "dev",
                    interface,
                    "parent",
                    "1:",
                    "handle",
                    "10:",
                    "netem",
                    *_netem_args(shaping),
                ]
            )
    elif shaping.has_netem:
        commands.append(
            ["tc", "qdisc", "add", "dev", interface, "root", "handle", "10:", "netem", *_netem_args(shaping)]
        )
    return commands


def teardown_command(interface: str) -> list[str]:
    """Idempotent root-qdisc removal. Always safe to run; non-zero exit (no qdisc
    present) is expected and tolerated by the caller — it must *always* run on
    teardown so a crashed run cannot leave a ``qdisc`` shaping later results."""
    return ["tc", "qdisc", "del", "dev", interface, "root"]


def restore_link_command(interface: str) -> list[str]:
    """Bring the interface back up — undoes a ``reconnect`` outage. Idempotent on an
    already-up interface, so it is safe to run unconditionally on teardown."""
    return ["ip", "link", "set", "dev", interface, "up"]


def safety_teardown_command(interface: str, max_duration_s: float) -> list[str]:
    """A self-contained watchdog that reverts ``interface`` after ``max_duration_s``.

    The hard fail-safe (RFC 0004): launched **detached** at arm time, it survives an
    orchestrator crash and still tears the qdisc down (and brings the link back up),
    so a killed run cannot leave shaping on the machine. The detachment itself is the
    launcher's job; this builds the inner argv it runs."""
    iface = shlex.quote(interface)
    seconds = max(1, int(round(max_duration_s)))
    return ["sh", "-c", f"sleep {seconds}; tc qdisc del dev {iface} root; ip link set dev {iface} up"]


def outage_commands(interface: str, kind: str) -> list[list[str]]:
    """Arm an outage of the given kind on ``interface``.

    ``catchup`` drops every packet but keeps the interface up (DDS endpoints
    survive → recovery is "catch up"); ``reconnect`` brings the interface down
    (forces RMW re-discovery → the harsher reconnect). The matching restore is
    :func:`outage_restore_commands`.
    """
    if kind == OUTAGE_CATCHUP:
        return [
            teardown_command(interface),
            ["tc", "qdisc", "add", "dev", interface, "root", "handle", "10:", "netem", "loss", "100%"],
        ]
    if kind == OUTAGE_RECONNECT:
        return [["ip", "link", "set", "dev", interface, "down"]]
    raise ValueError(f"outage must be one of {OUTAGE_KINDS}, got {kind!r}")


def outage_restore_commands(interface: str, kind: str) -> list[list[str]]:
    """Undo an outage. ``catchup`` clears the 100%-loss qdisc; ``reconnect`` brings
    the interface back up. The caller re-arms the following segment's shaping after."""
    if kind == OUTAGE_CATCHUP:
        return [teardown_command(interface)]
    if kind == OUTAGE_RECONNECT:
        return [["ip", "link", "set", "dev", interface, "up"]]
    raise ValueError(f"outage must be one of {OUTAGE_KINDS}, got {kind!r}")


# --------------------------------------------------------------------------- #
# Timeline expansion (segments → a stepping schedule)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TimelineStep:
    """A timeline segment placed on the absolute run clock: hold from ``start_s``
    to ``end_s``. ``commands`` arms the step; ``outage`` names the kind if any."""

    index: int
    start_s: float
    end_s: float
    outage: str | None
    commands: list[list[str]]


def expand_timeline(
    profile: Profile,
    interface: str,
    *,
    direction: str = "uplink",
) -> list[TimelineStep]:
    """Expand a timeline profile into absolute-clock steps for the given direction.

    Each step first tears the previous shaping down (the idempotent root delete),
    then arms its own — shaping segments arm ``shaping_commands``; outage segments
    arm ``outage_commands``. The stepping driver runs ``commands`` at ``start_s``;
    the recovery genre (RFC 0005) reads ``outage`` to know which restore semantics
    a given ``end_s`` represents.
    """
    if not profile.is_timeline:
        raise ValueError(f"profile {profile.name!r} is not a timeline profile")
    if direction not in ("uplink", "downlink"):
        raise ValueError(f"direction must be 'uplink' or 'downlink', got {direction!r}")

    steps: list[TimelineStep] = []
    clock = 0.0
    for index, segment in enumerate(profile.timeline):
        start_s, end_s = clock, clock + segment.for_s
        if segment.outage is not None:
            commands = outage_commands(interface, segment.outage)
        else:
            shaping = segment.uplink if direction == "uplink" else segment.downlink
            commands = [teardown_command(interface)]
            if shaping is not None:
                commands += shaping_commands(interface, shaping)
        steps.append(TimelineStep(index=index, start_s=start_s, end_s=end_s, outage=segment.outage, commands=commands))
        clock = end_s
    return steps
