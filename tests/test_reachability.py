"""ICMP reachability: tri-state semantics, debounce, and SNMP mapping.

No packets are sent -- ``ping_once`` is stubbed. The point of these tests is
the decision logic, especially that "could not check" never becomes "down".
"""

from __future__ import annotations

import pytest

from firewalla_snmp_proxy import reachability as r
from firewalla_snmp_proxy.reachability import (
    DOWN,
    ICMP_DISABLED,
    ICMP_DOWN,
    ICMP_UNKNOWN,
    ICMP_UP,
    UNKNOWN,
    UP,
    ReachabilityMonitor,
    ping_once,
)


def _mon(**kw):
    kw.setdefault("fail_threshold", 3)
    kw.setdefault("recover_threshold", 1)
    return ReachabilityMonitor("switch.example.com", **kw)


# -- debounce -------------------------------------------------------------
def test_starts_unknown_before_any_evidence():
    assert _mon().state is UNKNOWN


def test_single_success_confirms_up():
    m = _mon()
    m.feed(UP, 1.5)
    assert m.state is UP
    assert m.rtt_ms == 1.5


def test_one_dropped_packet_does_not_flip_to_down():
    """A single loss must not report the switch away."""
    m = _mon()
    m.feed(UP, 1.0)
    m.feed(DOWN)
    assert m.state is UP


def test_down_requires_the_full_threshold():
    m = _mon(fail_threshold=3)
    m.feed(UP, 1.0)
    m.feed(DOWN)
    m.feed(DOWN)
    assert m.state is UP, "flipped early"
    m.feed(DOWN)
    assert m.state is DOWN


def test_recovery_is_faster_than_failure():
    m = _mon(fail_threshold=3, recover_threshold=1)
    for _ in range(3):
        m.feed(DOWN)
    assert m.state is DOWN
    m.feed(UP, 2.0)
    assert m.state is UP


def test_success_resets_the_failure_run():
    m = _mon(fail_threshold=3)
    m.feed(DOWN)
    m.feed(DOWN)
    m.feed(UP, 1.0)
    m.feed(DOWN)
    m.feed(DOWN)
    assert m.state is UP, "failure counter was not reset by the success"


def test_unknown_does_not_move_the_debounce_counters():
    """The core rule: an unattemptable check is evidence of nothing.

    Folding it into "down" would report a fake outage every time this host
    rebooted before its resolver came up.
    """
    m = _mon(fail_threshold=3)
    m.feed(UP, 1.0)
    for _ in range(10):
        m.feed(UNKNOWN)
    assert m.state is UP


def test_unknown_does_not_creep_a_down_device_back_up():
    m = _mon(fail_threshold=1)
    m.feed(DOWN)
    assert m.state is DOWN
    for _ in range(5):
        m.feed(UNKNOWN)
    assert m.state is DOWN


def test_rtt_is_cleared_on_failure():
    """A stale RTT would advertise a round-trip that did not happen."""
    m = _mon(fail_threshold=1)
    m.feed(UP, 3.3)
    m.feed(DOWN)
    assert m.rtt_ms is None


# -- SNMP mapping ---------------------------------------------------------
@pytest.mark.parametrize(
    "state,expected",
    [(UP, ICMP_UP), (DOWN, ICMP_DOWN), (UNKNOWN, ICMP_UNKNOWN)],
)
def test_snmp_status_mapping(state, expected):
    m = _mon()
    m.state = state
    assert m.snmp_status() == expected


def test_no_host_reports_disabled_and_never_checks():
    m = ReachabilityMonitor("")
    assert not m.enabled
    assert m.snmp_status() == ICMP_DISABLED
    assert m.check() is UNKNOWN


# -- ping_once dispatch ---------------------------------------------------
def test_ping_once_empty_host_is_unknown():
    assert ping_once("") == (UNKNOWN, None)


def test_native_success_short_circuits_the_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(r, "_icmp_socket_ping", lambda h, t: 4.2)
    monkeypatch.setattr(
        r, "_cli_ping", lambda h, t: calls.append(h) or 9.9
    )
    assert ping_once("host") == (UP, 4.2)
    assert not calls, "CLI fallback ran despite native success"


def test_falls_back_to_cli_when_native_is_unavailable(monkeypatch):
    """Hosts with a restricted net.ipv4.ping_group_range take this path."""
    def no_native(h, t):
        raise r.Unattemptable("no unprivileged ICMP socket")

    monkeypatch.setattr(r, "_icmp_socket_ping", no_native)
    monkeypatch.setattr(r, "_cli_ping", lambda h, t: 7.5)
    assert ping_once("host") == (UP, 7.5)


def test_cli_no_reply_is_down(monkeypatch):
    monkeypatch.setattr(
        r, "_icmp_socket_ping",
        lambda h, t: (_ for _ in ()).throw(r.Unattemptable("x")),
    )
    monkeypatch.setattr(r, "_cli_ping", lambda h, t: None)
    assert ping_once("host") == (DOWN, None)


def test_cli_unattemptable_is_unknown_not_down(monkeypatch):
    monkeypatch.setattr(
        r, "_icmp_socket_ping",
        lambda h, t: (_ for _ in ()).throw(r.Unattemptable("x")),
    )
    monkeypatch.setattr(
        r, "_cli_ping",
        lambda h, t: (_ for _ in ()).throw(r.Unattemptable("unresolvable")),
    )
    assert ping_once("host") == (UNKNOWN, None)


def test_native_timeout_is_down(monkeypatch):
    def timeout(h, t):
        raise TimeoutError()

    monkeypatch.setattr(r, "_icmp_socket_ping", timeout)
    assert ping_once("host") == (DOWN, None)


def test_native_oserror_is_unknown_not_down(monkeypatch):
    """Network-unreachable is ambiguous; it must not fake a switch outage."""
    def refused(h, t):
        raise OSError("network unreachable")

    monkeypatch.setattr(r, "_icmp_socket_ping", refused)
    assert ping_once("host") == (UNKNOWN, None)


def test_unresolvable_host_is_unknown():
    """Real call, no stubbing: .invalid is guaranteed not to resolve."""
    assert ping_once("no-such-host.invalid", timeout=1.0) == (UNKNOWN, None)
