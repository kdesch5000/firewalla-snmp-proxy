"""Counter monotonicity across resets, restarts and wrap."""

from __future__ import annotations

import json
import os

from firewalla_snmp_proxy.counters import CounterStore, as_counter32, as_counter64

MAC = "20:6D:31:00:00:01"


def test_first_reading_passes_through():
    store = CounterStore(None)
    assert store.update(MAC, 1, "rxBytes", 500, 100) == 500


def test_rising_values_pass_through():
    store = CounterStore(None)
    store.update(MAC, 1, "rxBytes", 500, 100)
    assert store.update(MAC, 1, "rxBytes", 900, 100) == 900


def test_value_decrease_is_absorbed():
    """A counter that drops without any timestamp signal is still a reset."""
    store = CounterStore(None)
    store.update(MAC, 1, "rxBytes", 5000, 100)
    result = store.update(MAC, 1, "rxBytes", 20, 100)
    assert result == 5020
    assert result >= 5000, "published counter must never go backwards"


def test_stats_since_advance_detects_reset_even_when_value_rose():
    """The case a value comparison cannot catch.

    Counters were zeroed, but enough traffic passed before the next poll that
    the raw value is already *above* the previous reading. Only the advancing
    statsSinceTs reveals the discontinuity.
    """
    store = CounterStore(None)
    store.update(MAC, 1, "rxBytes", 1000, 100)
    store.update(MAC, 1, "rxBytes", 9999, 200)  # reset detected via timestamp
    assert store.update(MAC, 1, "rxBytes", 10000, 200) == 11000


def test_monotonic_across_many_resets():
    store = CounterStore(None)
    published = []
    since = 100
    for cycle in range(5):
        for raw in (100, 500, 900):
            published.append(store.update(MAC, 1, "rxBytes", raw, since))
        since += 10  # each cycle resets the counters
    assert published == sorted(published), "must be monotonically non-decreasing"


def test_persistence_survives_restart(tmp_path):
    path = str(tmp_path / "counters.json")
    store = CounterStore(path)
    store.update(MAC, 1, "rxBytes", 5000, 100)
    store.update(MAC, 1, "rxBytes", 20, 200)  # reset -> offset 5000
    store.save()

    reloaded = CounterStore(path)
    assert reloaded.update(MAC, 1, "rxBytes", 100, 200) == 5100


def test_state_file_is_written_atomically(tmp_path):
    path = str(tmp_path / "sub" / "counters.json")
    store = CounterStore(path)
    store.update(MAC, 1, "rxBytes", 1, 1)
    store.save()
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["version"] == 1
    # No temporary files left behind.
    leftovers = [f for f in os.listdir(os.path.dirname(path)) if f.startswith(".")]
    assert leftovers == []


def test_corrupt_state_file_is_ignored(tmp_path):
    """A bad state file must not stop the agent starting."""
    path = tmp_path / "counters.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = CounterStore(str(path))
    assert store.update(MAC, 1, "rxBytes", 42, 1) == 42


def test_forget_drops_only_that_switch():
    store = CounterStore(None)
    store.update(MAC, 1, "rxBytes", 100, 1)
    store.update("OTHER", 1, "rxBytes", 100, 1)
    store.forget(MAC)
    assert store.update(MAC, 1, "rxBytes", 5, 1) == 5        # state gone
    assert store.update("OTHER", 1, "rxBytes", 150, 1) == 150  # state kept


def test_negative_raw_is_clamped():
    store = CounterStore(None)
    assert store.update(MAC, 1, "rxBytes", -5, 1) == 0


def test_counter_folding():
    assert as_counter32(2 ** 32) == 0
    assert as_counter32(2 ** 32 + 7) == 7
    assert as_counter64(2 ** 64 + 3) == 3
    assert as_counter32(12345) == 12345
