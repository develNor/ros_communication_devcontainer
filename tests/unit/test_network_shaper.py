"""RFC 0004 validation checklist — host tests for the fail-safe arming controller
(the highest-risk item: a stuck qdisc silently corrupts every later result)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rosotacom.network_profiles import (
    restore_link_command,
    safety_teardown_command,
    teardown_command,
)
from rosotacom.network_shaper import ProfileShaper, ota_interface_from_route


class FakeRunner:
    """Records every argv it is asked to run; optionally fails matching ones."""

    def __init__(self, fail_on: object = None) -> None:
        self.calls: list[list[str]] = []
        self._fail_on = fail_on

    def __call__(self, argv: Sequence[str]) -> None:
        recorded = list(argv)
        self.calls.append(recorded)
        if self._fail_on is not None and self._fail_on in recorded:
            raise RuntimeError(f"boom on {self._fail_on!r}")


# --- interface resolution + the control-interface guard -------------------- #


def test_ota_interface_is_read_from_the_route() -> None:
    out = "10.8.0.2 dev tun0 src 10.8.0.7 uid 1000 \n    cache"
    assert ota_interface_from_route(out) == "tun0"
    with pytest.raises(ValueError):
        ota_interface_from_route("no device here")


def test_refuses_to_shape_the_control_interface() -> None:
    with pytest.raises(ValueError):
        ProfileShaper("eth0", FakeRunner(), control_interface="eth0")
    # A distinct data interface is fine.
    ProfileShaper("tun0", FakeRunner(), control_interface="eth0")


# --- arm / teardown ordering and the safety watchdog ----------------------- #


def test_arm_cleans_first_launches_watchdog_then_shapes() -> None:
    runner = FakeRunner()
    watchdog = FakeRunner()
    shaper = ProfileShaper("tun0", runner, safety_max_duration_s=300, watchdog_launcher=watchdog)
    shaping = [["tc", "qdisc", "add", "dev", "tun0", "root", "handle", "1:", "tbf", "rate", "4000000bit"]]
    shaper.arm(shaping)

    # Clean slate (del + link up) runs before the shaping command.
    assert runner.calls[0] == teardown_command("tun0")
    assert runner.calls[1] == restore_link_command("tun0")
    assert runner.calls[-1] == shaping[0]
    assert shaper.armed
    # The watchdog is launched detached, before shaping, with the configured timeout.
    assert watchdog.calls == [safety_teardown_command("tun0", 300)]


def test_no_watchdog_without_launcher_or_duration() -> None:
    watchdog = FakeRunner()
    ProfileShaper("tun0", FakeRunner(), watchdog_launcher=watchdog).arm([])
    assert watchdog.calls == []  # no max-duration → no watchdog


def test_teardown_tolerates_a_missing_qdisc() -> None:
    # Runner raises on the qdisc delete (no qdisc present) — teardown must not blow up.
    runner = FakeRunner(fail_on="del")
    shaper = ProfileShaper("tun0", runner)
    shaper.teardown()
    assert runner.calls[0] == teardown_command("tun0")
    assert runner.calls[1] == restore_link_command("tun0")  # still brings the link up
    assert not shaper.armed


def test_apply_tolerates_timeline_cleanup_with_no_qdisc() -> None:
    runner = FakeRunner(fail_on="del")
    shaper = ProfileShaper("tun0", runner)
    shaping = ["tc", "qdisc", "add", "dev", "tun0", "root", "handle", "10:", "netem", "delay", "50ms"]

    shaper.apply([teardown_command("tun0"), shaping])

    assert runner.calls == [teardown_command("tun0"), shaping]
    assert shaper.armed


def test_apply_reraises_timeline_shaping_failures() -> None:
    runner = FakeRunner(fail_on="add")
    shaper = ProfileShaper("tun0", runner)
    shaping = ["tc", "qdisc", "add", "dev", "tun0", "root", "handle", "10:", "netem", "delay", "50ms"]

    with pytest.raises(RuntimeError, match="boom on 'add'"):
        shaper.apply([teardown_command("tun0"), shaping])

    assert runner.calls == [teardown_command("tun0"), shaping]


# --- revert on stop and on error (the context manager) --------------------- #


def test_context_manager_reverts_on_normal_exit() -> None:
    runner = FakeRunner()
    with ProfileShaper("tun0", runner) as shaper:
        shaper.apply([["tc", "qdisc", "add", "dev", "tun0", "root", "handle", "10:", "netem", "loss", "1%"]])
        assert shaper.armed
    assert runner.calls[-2:] == [teardown_command("tun0"), restore_link_command("tun0")]
    assert not shaper.armed


def test_context_manager_reverts_on_error_and_reraises() -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError, match="run blew up"):
        with ProfileShaper("tun0", runner) as shaper:
            shaper.apply([["tc", "qdisc", "add", "dev", "tun0", "root", "handle", "10:", "netem", "loss", "1%"]])
            raise ValueError("run blew up")
    # The error propagates, but the interface is still reverted.
    assert runner.calls[-2:] == [teardown_command("tun0"), restore_link_command("tun0")]


def test_safety_teardown_command_reverts_after_the_timeout() -> None:
    argv = safety_teardown_command("tun0", 90)
    assert argv[0] == "sh" and argv[1] == "-c"
    script = argv[2]
    assert "sleep 90" in script
    assert "tc qdisc del dev tun0 root" in script
    assert "ip link set dev tun0 up" in script
