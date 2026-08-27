"""Shared fixtures. No test in this suite touches the network."""

from __future__ import annotations

import json
import os
import time

import pytest

from firewalla_snmp_proxy.counters import CounterStore
from firewalla_snmp_proxy.mibs import SwitchContext
from firewalla_snmp_proxy.model import Switch
from firewalla_snmp_proxy.tree_builder import build_tree

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

NETWORKS = {
    "11111111-1111-1111-1111-111111111111": "ExampleLAN",
    "22222222-2222-2222-2222-222222222222": "IoTVLan",
}


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def switch_raw():
    return load_fixture("switch_detail.json")


@pytest.fixture
def switch_settings():
    return load_fixture("switch_settings.json")


@pytest.fixture
def topology():
    return load_fixture("topology.json")


@pytest.fixture
def switch(switch_raw, switch_settings):
    # Fixed polled_at keeps uptime-derived values deterministic per test.
    return Switch(
        raw=switch_raw,
        settings=switch_settings,
        networks=NETWORKS,
        polled_at=time.time(),
    )


@pytest.fixture
def ctx(switch):
    context = SwitchContext(
        switch=switch,
        counters=CounterStore(None),
        enterprise_oid="1.3.6.1.4.1.99999",
        proxy_version="0.1.0-test",
    )
    context.poll_count = 1
    context.last_poll_ok = time.time()
    return context


@pytest.fixture
def tree(ctx):
    return build_tree(ctx)
