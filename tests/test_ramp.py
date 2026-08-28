"""Counter ramping: interpolation math, monotonicity, and the escape hatches."""

from __future__ import annotations

import pytest

from firewalla_snmp_proxy.ramp import CounterRamp


@pytest.fixture
def ramp():
    return CounterRamp(max_window=2250.0)  # 2.5 x 900s


def test_first_reading_is_served_raw(ramp):
    assert ramp.value("k", 1000, now=0.0) == 1000


def test_no_ramp_until_a_second_observation(ramp):
    """One observation gives no window, so there is nothing to interpolate."""
    ramp.value("k", 1000, now=0.0)
    assert ramp.value("k", 1000, now=100.0) == 1000
    assert ramp.value("k", 1000, now=899.0) == 1000


def test_increment_is_paid_out_over_the_following_window(ramp):
    """The canonical case: 900s window, NMS polling every 300s.

    Observium sees a third of the increment per poll, so all three samples
    compute the same rate instead of 0/0/3x.
    """
    ramp.value("k", 0, now=0.0)
    ramp.value("k", 9000, now=900.0)  # learned 9000 bytes over 900s

    assert ramp.value("k", 9000, now=900.0) == 0
    assert ramp.value("k", 9000, now=1200.0) == 3000
    assert ramp.value("k", 9000, now=1500.0) == 6000
    assert ramp.value("k", 9000, now=1800.0) == 9000


def test_totals_are_exact_across_a_full_window(ramp):
    """Ramping redistributes bytes; it must never add or drop any."""
    ramp.value("k", 0, now=0.0)
    ramp.value("k", 12345, now=600.0)
    assert ramp.value("k", 12345, now=1200.0) == 12345


def test_plateaus_at_the_true_value_when_window_elapses(ramp):
    """A late next poll must not extrapolate past what we actually know."""
    ramp.value("k", 0, now=0.0)
    ramp.value("k", 9000, now=900.0)
    assert ramp.value("k", 9000, now=1800.0) == 9000
    assert ramp.value("k", 9000, now=5000.0) == 9000


def test_frozen_upstream_reads_as_zero_traffic(ramp):
    """An API that stops updating must not look like traffic.

    This is the failure that motivated the feature: during a rate limit the
    counters stop advancing, and the honest rendering is a flat line, not an
    invented ramp toward a value that never arrived.
    """
    ramp.value("k", 0, now=0.0)
    ramp.value("k", 9000, now=900.0)
    ramp.value("k", 9000, now=1800.0)  # ramp completed
    for t in (2700.0, 3600.0, 4500.0):
        assert ramp.value("k", 9000, now=t) == 9000


def test_idle_port_stays_flat(ramp):
    ramp.value("k", 500, now=0.0)
    for t in (300.0, 600.0, 900.0, 1200.0):
        assert ramp.value("k", 500, now=t) == 500


def test_output_is_monotonic_across_many_irregular_windows():
    """SNMP counters must never decrease.

    Windows are deliberately irregular -- early polls, late polls, and a
    backoff-sized gap -- because that is where a naive seam would step back.
    """
    ramp = CounterRamp(max_window=2250.0)
    schedule = [
        (0.0, 0), (900.0, 10_000), (1500.0, 25_000), (3000.0, 26_000),
        (3400.0, 90_000), (4300.0, 90_000), (5200.0, 150_000),
    ]
    served = []
    t = 0.0
    idx = 0
    while t <= 6000.0:
        while idx + 1 < len(schedule) and schedule[idx + 1][0] <= t:
            idx += 1
        served.append(ramp.value("k", schedule[idx][1], now=t))
        t += 60.0
    assert served == sorted(served), "counter went backwards"


def test_window_longer_than_max_serves_raw(ramp):
    """A multi-hour gap must not smear across a multi-hour ramp."""
    ramp.value("k", 0, now=0.0)
    ramp.value("k", 500_000, now=10_000.0)  # 10000s >> 2250s ceiling
    assert ramp.value("k", 500_000, now=10_000.0) == 500_000
    assert ramp.value("k", 500_000, now=10_300.0) == 500_000


def test_ramp_resumes_after_an_over_long_window(ramp):
    """The ceiling suppresses one sample, not the feature."""
    ramp.value("k", 0, now=0.0)
    ramp.value("k", 500_000, now=10_000.0)
    ramp.value("k", 509_000, now=10_900.0)
    assert ramp.value("k", 509_000, now=11_200.0) == 503_000


def test_decreasing_value_resyncs_rather_than_serving_a_decrease(ramp, caplog):
    """Unreachable via CounterStore, but must fail safe if it ever happens."""
    ramp.value("k", 0, now=0.0)
    ramp.value("k", 9000, now=900.0)
    assert ramp.value("k", 4000, now=1000.0) == 4000
    assert ramp.value("k", 4000, now=1300.0) == 4000
    assert "decrease" in caplog.text


def test_keys_are_independent(ramp):
    ramp.value("a", 0, now=0.0)
    ramp.value("b", 0, now=0.0)
    ramp.value("a", 900, now=900.0)
    ramp.value("b", 90_000, now=900.0)
    assert ramp.value("a", 900, now=1350.0) == 450
    assert ramp.value("b", 90_000, now=1350.0) == 45_000


def test_large_64bit_counters_keep_full_precision(ramp):
    """Scaling the delta rather than the absolute avoids float rounding."""
    base = 2 ** 60
    ramp.value("k", base, now=0.0)
    ramp.value("k", base + 9000, now=900.0)
    assert ramp.value("k", base + 9000, now=1200.0) == base + 3000


def test_forget_drops_only_the_named_switch(ramp):
    ramp.value("AA:BB|1|ifInOctets", 5, now=0.0)
    ramp.value("CC:DD|1|ifInOctets", 5, now=0.0)
    ramp.forget("AA:BB")
    assert "AA:BB|1|ifInOctets" not in ramp._state
    assert "CC:DD|1|ifInOctets" in ramp._state
