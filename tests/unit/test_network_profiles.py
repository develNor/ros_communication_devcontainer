"""RFC 0004 validation checklist — host tests for the network-profile schema and
``tc``/``netem`` command generation (the pure half; privileged arming is a bench check)."""

from __future__ import annotations

import pytest

from rosotacom.network_profiles import (
    OUTAGE_CATCHUP,
    OUTAGE_RECONNECT,
    DirectionShaping,
    Profile,
    TimelineSegment,
    expand_timeline,
    load_profiles_file,
    outage_commands,
    outage_restore_commands,
    parse_direction,
    parse_ms,
    parse_pct,
    parse_profile,
    parse_profiles,
    parse_rate_bps,
    parse_seconds,
    resolve_profile_selection,
    shaping_commands,
    teardown_command,
)

# --- value parsing --------------------------------------------------------- #


def test_rate_parsing_is_bit_oriented() -> None:
    assert parse_rate_bps("4mbit") == 4_000_000
    assert parse_rate_bps("500kbit") == 500_000
    assert parse_rate_bps("2.5mbit") == 2_500_000
    assert parse_rate_bps(4_000_000) == 4_000_000
    with pytest.raises(ValueError):
        parse_rate_bps("4mbps")  # byte units rejected — ambiguous on the wire
    with pytest.raises(ValueError):
        parse_rate_bps("0mbit")


def test_duration_and_pct_parsing() -> None:
    assert parse_ms("120ms") == 120.0
    assert parse_ms("0.5s") == 500.0
    assert parse_ms(30) == 30.0
    assert parse_seconds("30s") == 30.0
    assert parse_seconds("500ms") == 0.5
    assert parse_pct("2%") == 2.0
    assert parse_pct(100) == 100.0
    with pytest.raises(ValueError):
        parse_pct("150%")
    with pytest.raises(ValueError):
        parse_seconds("0s")


# --- direction schema + validation ----------------------------------------- #


def test_parse_direction_full_block() -> None:
    shaping = parse_direction(
        {"rate": "4mbit", "delay": "120ms", "jitter": "30ms", "distribution": "normal", "loss": "2%"}
    )
    assert shaping is not None
    assert shaping.rate_bps == 4_000_000
    assert shaping.delay_ms == 120.0
    assert shaping.jitter_ms == 30.0
    assert shaping.distribution == "normal"
    assert shaping.loss_pct == 2.0
    assert not shaping.is_empty and shaping.has_netem


def test_parse_direction_rejects_unknown_keys_and_invalid_combos() -> None:
    with pytest.raises(ValueError):
        parse_direction({"bandwidth": "4mbit"})  # unknown key
    with pytest.raises(ValueError):
        DirectionShaping(jitter_ms=30.0)  # jitter without delay
    with pytest.raises(ValueError):
        DirectionShaping(delay_ms=120.0, distribution="normal")  # distribution without jitter
    with pytest.raises(ValueError):
        DirectionShaping(loss_correlation_pct=25.0)  # correlation without loss
    with pytest.raises(ValueError):
        DirectionShaping(reorder_pct=5.0)  # reorder without delay


# --- tc/netem command generation ------------------------------------------- #


def test_shaping_commands_tbf_root_with_netem_child() -> None:
    # The canonical static cellular profile from the calibrated baseline.
    shaping = DirectionShaping(rate_bps=4_000_000, delay_ms=120.0, jitter_ms=30.0, distribution="normal", loss_pct=2.0)
    commands = shaping_commands("tun0", shaping)
    assert commands == [
        [
            "tc",
            "qdisc",
            "add",
            "dev",
            "tun0",
            "root",
            "handle",
            "1:",
            "tbf",
            "rate",
            "4000000bit",
            "burst",
            "32kbit",
            "latency",
            "100ms",
        ],
        [
            "tc",
            "qdisc",
            "add",
            "dev",
            "tun0",
            "parent",
            "1:",
            "handle",
            "10:",
            "netem",
            "delay",
            "120ms",
            "30ms",
            "distribution",
            "normal",
            "loss",
            "2%",
        ],
    ]


def test_shaping_commands_netem_only_becomes_root() -> None:
    commands = shaping_commands("tun0", DirectionShaping(delay_ms=60.0, loss_pct=1.0))
    assert commands == [
        ["tc", "qdisc", "add", "dev", "tun0", "root", "handle", "10:", "netem", "delay", "60ms", "loss", "1%"]
    ]


def test_shaping_commands_rate_only_and_empty() -> None:
    rate_only = shaping_commands("eth0", DirectionShaping(rate_bps=1_000_000))
    assert rate_only == [
        [
            "tc",
            "qdisc",
            "add",
            "dev",
            "eth0",
            "root",
            "handle",
            "1:",
            "tbf",
            "rate",
            "1000000bit",
            "burst",
            "32kbit",
            "latency",
            "100ms",
        ]
    ]
    assert shaping_commands("eth0", DirectionShaping()) == []


def test_netem_arg_ordering_is_valid() -> None:
    # netem requires delay before reorder, and the correlation value follows its base loss.
    shaping = DirectionShaping(
        delay_ms=100.0, jitter_ms=20.0, loss_pct=3.0, loss_correlation_pct=25.0, duplicate_pct=1.0, reorder_pct=5.0
    )
    netem = shaping_commands("tun0", shaping)[0]
    assert netem[netem.index("netem") :] == [
        "netem",
        "delay",
        "100ms",
        "20ms",
        "loss",
        "3%",
        "25%",
        "duplicate",
        "1%",
        "reorder",
        "5%",
    ]


def test_teardown_is_root_delete() -> None:
    assert teardown_command("tun0") == ["tc", "qdisc", "del", "dev", "tun0", "root"]


# --- outage kinds (both named variants) ------------------------------------ #


def test_outage_catchup_is_full_loss_interface_up() -> None:
    assert outage_commands("tun0", OUTAGE_CATCHUP) == [
        ["tc", "qdisc", "del", "dev", "tun0", "root"],
        ["tc", "qdisc", "add", "dev", "tun0", "root", "handle", "10:", "netem", "loss", "100%"],
    ]
    assert outage_restore_commands("tun0", OUTAGE_CATCHUP) == [["tc", "qdisc", "del", "dev", "tun0", "root"]]


def test_outage_reconnect_is_link_down() -> None:
    assert outage_commands("tun0", OUTAGE_RECONNECT) == [["ip", "link", "set", "dev", "tun0", "down"]]
    assert outage_restore_commands("tun0", OUTAGE_RECONNECT) == [["ip", "link", "set", "dev", "tun0", "up"]]


# --- profile parsing (static + timeline) ----------------------------------- #


def test_parse_static_profile() -> None:
    profile = parse_profile(
        "cellular-typical",
        {"uplink": {"rate": "4mbit", "delay": "120ms", "loss": "2%"}, "downlink": {"rate": "25mbit", "delay": "60ms"}},
    )
    assert profile.kind == "static" and not profile.is_timeline
    assert profile.uplink is not None and profile.uplink.rate_bps == 4_000_000
    assert profile.downlink is not None and profile.downlink.rate_bps == 25_000_000


def test_parse_profiles_document() -> None:
    profiles = parse_profiles(
        {"profiles": {"none-ish": {"uplink": {"delay": "10ms"}}, "cellular": {"uplink": {"rate": "4mbit"}}}}
    )
    assert set(profiles) == {"none-ish", "cellular"}
    assert profiles["cellular"].uplink is not None and profiles["cellular"].uplink.rate_bps == 4_000_000


def test_timeline_outage_kinds_and_default() -> None:
    profile = parse_profile(
        "handover",
        {
            "timeline": [
                {"for": "30s", "uplink": {"rate": "4mbit", "delay": "60ms", "loss": "0.1%"}},
                {"for": "15s", "uplink": {"rate": "1mbit", "delay": "180ms", "loss": "3%"}},
                {"for": "5s", "outage": "reconnect"},
                {"for": "40s", "uplink": {"rate": "2.5mbit", "delay": "90ms", "loss": "1%"}},
            ]
        },
    )
    assert profile.is_timeline and profile.total_duration_s == 90.0
    assert profile.timeline[2].outage == OUTAGE_RECONNECT
    # A bare `outage: true` defaults to the milder catchup kind.
    assert parse_profile("o", {"timeline": [{"for": "5s", "outage": True}]}).timeline[0].outage == OUTAGE_CATCHUP


def test_parse_profile_rejects_mixed_and_unknown() -> None:
    with pytest.raises(ValueError):
        parse_profile("bad", {"timeline": [{"for": "5s"}], "uplink": {"rate": "4mbit"}})  # timeline + static
    with pytest.raises(ValueError):
        parse_profile("bad", {"sidelink": {"rate": "4mbit"}})  # unknown direction key
    with pytest.raises(ValueError):
        parse_profile("bad", {"timeline": [{"for": "5s", "outage": "reconnect", "uplink": {"rate": "4mbit"}}]})


# --- timeline expansion ----------------------------------------------------- #


def test_expand_timeline_places_steps_on_absolute_clock() -> None:
    profile = parse_profile(
        "handover",
        {
            "timeline": [
                {"for": "30s", "uplink": {"rate": "4mbit", "delay": "60ms"}},
                {"for": "5s", "outage": "catchup"},
                {"for": "40s", "uplink": {"rate": "2.5mbit", "delay": "90ms"}},
            ]
        },
    )
    steps = expand_timeline(profile, "tun0", direction="uplink")
    assert [(step.start_s, step.end_s, step.outage) for step in steps] == [
        (0.0, 30.0, None),
        (30.0, 35.0, OUTAGE_CATCHUP),
        (35.0, 75.0, None),
    ]
    # Every shaping step first tears the previous qdisc down (idempotent), then arms its own.
    assert steps[0].commands[0] == teardown_command("tun0")
    assert steps[0].commands[1][:9] == ["tc", "qdisc", "add", "dev", "tun0", "root", "handle", "1:", "tbf"]
    assert steps[1].commands == outage_commands("tun0", OUTAGE_CATCHUP)


def test_expand_timeline_uses_the_requested_direction() -> None:
    profile = Profile(
        name="asym",
        kind="timeline",
        timeline=(
            TimelineSegment(
                for_s=10.0,
                uplink=DirectionShaping(rate_bps=1_000_000),
                downlink=DirectionShaping(rate_bps=25_000_000),
            ),
        ),
    )
    up = expand_timeline(profile, "tun0", direction="uplink")[0]
    down = expand_timeline(profile, "tun0", direction="downlink")[0]
    assert "1000000bit" in up.commands[1]
    assert "25000000bit" in down.commands[1]
    with pytest.raises(ValueError):
        expand_timeline(profile, "tun0", direction="sideways")


# --- profile file loading + selection resolution --------------------------- #


def test_load_profiles_file(tmp_path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(
        "profiles:\n"
        "  cellular-typical:\n"
        "    uplink: { rate: 4mbit, delay: 120ms, loss: 2% }\n"
        "  handover:\n"
        "    timeline:\n"
        "      - { for: 30s, uplink: { rate: 4mbit } }\n"
        "      - { for: 5s, outage: reconnect }\n",
        encoding="utf-8",
    )
    profiles = load_profiles_file(path)
    assert set(profiles) == {"cellular-typical", "handover"}
    assert profiles["handover"].is_timeline


def test_resolve_profile_selection() -> None:
    available = {"cellular", "handover"}
    # Explicit selection wins and is validated against the configured set.
    assert resolve_profile_selection("cellular", available=available) == "cellular"
    # Omitted → the shared default; 'none' and omission with no default → unshaped.
    assert resolve_profile_selection(None, shared_default="handover", available=available) == "handover"
    assert resolve_profile_selection(None) is None
    assert resolve_profile_selection("none", available=available) is None
    # A typo is caught when a profiles file is configured.
    with pytest.raises(ValueError):
        resolve_profile_selection("celluar", available=available)
    # A profile on a run that may not be shaped (the real fleet) is an error.
    with pytest.raises(ValueError):
        resolve_profile_selection("cellular", available=available, allow_shaping=False)
    # ...but 'none' is always fine, shaping or not.
    assert resolve_profile_selection("none", allow_shaping=False) is None
