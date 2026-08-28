"""Ramping through the full SNMP tree, at Observium's real polling cadence.

The unit tests in ``test_ramp.py`` cover the interpolation math. These cover
the thing that actually broke in production: a 15-minute MSP poll interval
serving a 5-minute NMS poller, and whether the rates that poller derives are
steady or a sawtooth.
"""

from __future__ import annotations

import time

import pytest

from firewalla_snmp_proxy.counters import CounterStore
from firewalla_snmp_proxy.mibs import SwitchContext
from firewalla_snmp_proxy.model import Switch
from firewalla_snmp_proxy.ramp import CounterRamp
from firewalla_snmp_proxy.tree_builder import build_tree

from .conftest import NETWORKS

#: ifHCInOctets.<ifIndex>
IF_HC_IN_OCTETS = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 6)

MSP_INTERVAL = 900.0     # proxy -> MSP API
NMS_INTERVAL = 300.0     # Observium -> proxy
PORT = 10
#: 30 Mbps sustained, in octets per MSP window.
OCTETS_PER_WINDOW = int(30e6 / 8 * MSP_INTERVAL)


def _ctx(switch_raw, switch_settings, ramp):
    switch = Switch(
        raw=switch_raw, settings=switch_settings, networks=NETWORKS,
        polled_at=time.time(),
    )
    ctx = SwitchContext(
        switch=switch, counters=CounterStore(None),
        enterprise_oid="1.3.6.1.4.1.99999", proxy_version="test", ramp=ramp,
    )
    ctx.poll_count = 1
    ctx.last_poll_ok = time.time()
    return ctx


def _set_octets(ctx, switch_raw, switch_settings, port_number, value):
    """Rewrite the raw fixture's counter and re-wrap it, as a poll would."""
    raw = {**switch_raw}
    raw["ports"] = [dict(p) for p in raw["ports"]]
    for p in raw["ports"]:
        if int(p.get("id", p.get("port", -1))) == port_number:
            p["rxBytes"] = value
            break
    else:
        pytest.skip("fixture has no port %d" % port_number)
    ctx.switch = Switch(
        raw=raw, settings=switch_settings, networks=NETWORKS,
        polled_at=time.time(),
    )


def _in_octets(tree, if_index):
    # OidTree.get resolves the provider, so this is the value an SNMP GET
    # would encode -- the ramp runs inside that call.
    value = tree.get(IF_HC_IN_OCTETS + (if_index,))
    assert value is not None, "ifHCInOctets.%d not instantiated" % if_index
    return int(value)


def _port_ifindex(switch_raw):
    return int(switch_raw["ports"][0].get("id", switch_raw["ports"][0].get("port")))


def _simulate(switch_raw, switch_settings, ramp, windows=4):
    """Return the octet values an NMS would read, polling every 300s.

    Traffic is constant, so a correct implementation yields a constant rate.
    """
    ctx = _ctx(switch_raw, switch_settings, ramp)
    tree = build_tree(ctx)
    idx = _port_ifindex(switch_raw)

    base = _in_octets(tree, idx)
    samples = []
    t = 0.0
    total = 0
    for w in range(windows):
        # An MSP refresh lands at the top of each window.
        total += OCTETS_PER_WINDOW
        _set_octets(ctx, switch_raw, switch_settings, idx, base + total)
        for _ in range(int(MSP_INTERVAL / NMS_INTERVAL)):
            if ramp is not None:
                # Ramping is time-dependent; drive it off the simulated clock.
                samples.append(_read_at(tree, idx, ramp, t))
            else:
                samples.append(_in_octets(tree, idx))
            t += NMS_INTERVAL
    return samples


def _read_at(tree, idx, ramp, when):
    """Read ifHCInOctets with the ramp's clock pinned to ``when``."""
    real = ramp.value

    def pinned(key, current, now=None):
        return real(key, current, now=when)

    ramp.value = pinned
    try:
        return _in_octets(tree, idx)
    finally:
        ramp.value = real


def _rates(samples, interval=NMS_INTERVAL):
    return [
        (b - a) / interval for a, b in zip(samples, samples[1:])
    ]


def test_raw_mode_sawtooths_at_a_faster_nms_cadence(switch_raw, switch_settings):
    """Documents the bug this feature exists to fix.

    Without ramping, two of every three Observium polls see identical counters
    and derive a rate of zero, and the third divides 900s of traffic by 300s.
    """
    samples = _simulate(switch_raw, switch_settings, ramp=None)
    rates = _rates(samples)
    zeros = [r for r in rates if r == 0]
    spikes = [r for r in rates if r > 0]
    assert zeros, "expected zero-rate samples in raw mode"
    assert spikes, "expected spike samples in raw mode"
    # The spike overstates the true rate by the cadence ratio (900/300 = 3x).
    true_rate = OCTETS_PER_WINDOW / MSP_INTERVAL
    assert max(spikes) == pytest.approx(true_rate * 3, rel=0.01)


def test_ramp_mode_yields_a_steady_rate(switch_raw, switch_settings):
    """The fix: every NMS poll derives the same, correct rate."""
    ramp = CounterRamp(max_window=MSP_INTERVAL * 2.5)
    samples = _simulate(switch_raw, switch_settings, ramp=ramp)
    rates = _rates(samples)
    true_rate = OCTETS_PER_WINDOW / MSP_INTERVAL

    # Skip the first window: the ramp has no prior observation to interpolate
    # from, so it legitimately reports zero until a second reading arrives.
    steady = rates[int(MSP_INTERVAL / NMS_INTERVAL):]
    assert steady, "no steady-state samples"
    for r in steady:
        assert r == pytest.approx(true_rate, rel=0.02), rates
    assert 0 not in steady


def test_ramp_mode_never_decreases(switch_raw, switch_settings):
    ramp = CounterRamp(max_window=MSP_INTERVAL * 2.5)
    samples = _simulate(switch_raw, switch_settings, ramp=ramp, windows=6)
    assert samples == sorted(samples)


def test_ramp_delivers_exactly_one_window_per_window(switch_raw, switch_settings):
    """Conservation: redistribution must not invent or lose octets.

    Measured boundary to boundary, where the ramp has just finished paying out
    one increment and not yet started the next, so the comparison is exact
    rather than approximate.
    """
    per_window = int(MSP_INTERVAL / NMS_INTERVAL)
    samples = _simulate(
        switch_raw, switch_settings,
        ramp=CounterRamp(max_window=MSP_INTERVAL * 2.5), windows=5,
    )
    boundaries = samples[::per_window]
    deltas = [b - a for a, b in zip(boundaries, boundaries[1:])]

    # The first window delivers nothing: at startup there is no prior
    # observation to interpolate from, so there is no increment to pay out yet.
    # This is the one-window lag, asserted rather than glossed over.
    assert deltas[0] == 0
    # Every window thereafter delivers its full increment, exactly.
    assert deltas[1:] == [OCTETS_PER_WINDOW] * (len(deltas) - 1), deltas
