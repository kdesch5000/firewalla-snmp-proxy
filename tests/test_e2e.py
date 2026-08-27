"""End-to-end: a real UDP socket, queried by a real SNMP manager.

These bind a loopback port and drive the agent with pysnmp's client API, so
they exercise the actual BER encoding, community authentication and GETNEXT
walk that an NMS performs. No Firewalla API access is involved -- the switch
data comes from fixtures.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from firewalla_snmp_proxy.agent import SwitchAgent

COMMUNITY = "testcommunity"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _walk(host, port, community, root):
    """Walk ``root`` using GETNEXT, exactly as an NMS discovery pass does."""
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        next_cmd,
    )

    from pysnmp.proto.api import v2c

    engine = SnmpEngine()
    target = await UdpTransportTarget.create((host, port), timeout=3, retries=1)
    results = []
    current = root
    while True:
        err, status, idx, binds = await next_cmd(
            engine, CommunityData(community, mpModel=1), target, ContextData(),
            ObjectType(ObjectIdentity(current)), lexicographicMode=False,
        )
        if err or status or not binds:
            break
        name, value = binds[0]
        name_str = str(name)
        # End of tree: the agent echoes the requested OID back with an
        # endOfMibView value. Detect both that marker and any failure to
        # advance, or the walk loops forever on the final OID.
        if isinstance(value, v2c.EndOfMibView):
            break
        if _as_tuple(name_str) <= _as_tuple(current):
            break
        if not name_str.startswith(root + "."):
            break
        results.append((name_str, value))
        current = name_str
        if len(results) > 5000:  # runaway guard
            pytest.fail("walk did not terminate")
    engine.close_dispatcher()
    return results


def _as_tuple(oid_str):
    return tuple(int(x) for x in oid_str.strip(".").split("."))


async def _get(host, port, community, oid):
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
    )

    engine = SnmpEngine()
    target = await UdpTransportTarget.create((host, port), timeout=3, retries=1)
    err, status, idx, binds = await get_cmd(
        engine, CommunityData(community, mpModel=1), target, ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )
    engine.close_dispatcher()
    return err, status, binds


def run_with_agent(ctx, coro_factory):
    """Start an agent on a free port, run one coroutine against it, stop."""
    port = free_port()

    async def main():
        agent = SwitchAgent(ctx, "127.0.0.1", port, COMMUNITY)
        agent.start()
        try:
            await asyncio.sleep(0.3)  # let the transport settle
            return await coro_factory("127.0.0.1", port)
        finally:
            agent.stop()

    return asyncio.run(main())


def test_get_sysdescr_over_the_wire(ctx):
    err, status, binds = run_with_agent(
        ctx, lambda h, p: _get(h, p, COMMUNITY, "1.3.6.1.2.1.1.1.0")
    )
    assert err is None and not status
    assert "Firewalla" in str(binds[0][1])


def test_wrong_community_gets_no_answer(ctx):
    err, status, binds = run_with_agent(
        ctx, lambda h, p: _get(h, p, "wrong-community", "1.3.6.1.2.1.1.1.0")
    )
    # An agent must silently drop a bad community; the manager sees a timeout.
    assert err is not None


def test_full_walk_terminates_and_is_ordered(ctx):
    results = run_with_agent(ctx, lambda h, p: _walk(h, p, COMMUNITY, "1.3.6.1"))
    assert len(results) > 400
    oids = [tuple(int(x) for x in name.split(".")) for name, _ in results]
    assert oids == sorted(oids), "walk returned OIDs out of order"
    assert len(oids) == len(set(oids)), "walk repeated an OID"


def test_lldp_subtree_is_reachable(ctx):
    """LLDP lives at 1.0.8802, outside 1.3.6.1.

    If the agent's access-control view is scoped to 1.3.6.1 -- the obvious
    choice -- this whole subtree silently disappears and with it the NMS
    topology link. This test is the guard against that regression.
    """
    results = run_with_agent(ctx, lambda h, p: _walk(h, p, COMMUNITY, "1.0.8802"))
    assert results, "LLDP subtree not served; check the VACM view root"
    names = [n for n, _ in results]
    assert any(n.startswith("1.0.8802.1.1.2.1.4.1.1.5") for n in names), (
        "no lldpRemChassisId row -- topology discovery would find no neighbour"
    )


def test_interface_table_walk(ctx, switch):
    results = run_with_agent(
        ctx, lambda h, p: _walk(h, p, COMMUNITY, "1.3.6.1.2.1.2.2.1.8")
    )
    assert len(results) == len(switch.ports)
    statuses = {name.rsplit(".", 1)[1]: int(v) for name, v in results}
    assert statuses["1"] == 1   # port 1 up
    assert statuses["9"] == 2   # port 9 down


def test_counter64_survives_the_wire(ctx, switch):
    """A >2^32 value must round-trip as Counter64, not overflow."""
    port1 = next(p for p in switch.ports if p.number == 1)
    assert port1.counter("rxBytes") > 2 ** 32, "fixture should exceed 32 bits"
    err, status, binds = run_with_agent(
        ctx, lambda h, p: _get(h, p, COMMUNITY, "1.3.6.1.2.1.31.1.1.1.6.1")
    )
    assert err is None and not status
    assert int(binds[0][1]) == port1.counter("rxBytes")


def test_set_is_refused(ctx):
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        set_cmd,
    )
    from pysnmp.proto import rfc1902

    async def do_set(host, port):
        engine = SnmpEngine()
        target = await UdpTransportTarget.create((host, port), timeout=3, retries=1)
        result = await set_cmd(
            engine, CommunityData(COMMUNITY, mpModel=1), target, ContextData(),
            ObjectType(
                ObjectIdentity("1.3.6.1.2.1.1.6.0"), rfc1902.OctetString("hacked")
            ),
        )
        engine.close_dispatcher()
        return result

    err, status, idx, binds = run_with_agent(ctx, do_set)
    # Must be an explicit refusal, not a timeout: a silent drop looks to the
    # manager like the agent is down.
    assert err is None, "SET should be answered, not dropped"
    assert status, "SET must return an error status"


def test_two_agents_on_separate_ports(ctx, switch_raw, switch_settings):
    """Each switch gets its own UDP port; some NMSes key devices on IP:port."""
    import copy
    import time

    from firewalla_snmp_proxy.counters import CounterStore
    from firewalla_snmp_proxy.mibs import SwitchContext
    from firewalla_snmp_proxy.model import Switch

    raw_b = copy.deepcopy(switch_raw)
    raw_b["hostname"] = "second-switch"
    ctx_b = SwitchContext(
        switch=Switch(raw=raw_b, settings=switch_settings, polled_at=time.time()),
        counters=CounterStore(None),
        proxy_version="0.1.0-test",
    )
    ctx_b.last_poll_ok = time.time()

    port_a, port_b = free_port(), free_port()

    async def main():
        a = SwitchAgent(ctx, "127.0.0.1", port_a, COMMUNITY)
        b = SwitchAgent(ctx_b, "127.0.0.1", port_b, COMMUNITY)
        a.start()
        b.start()
        try:
            await asyncio.sleep(0.3)
            ra = await _get("127.0.0.1", port_a, COMMUNITY, "1.3.6.1.2.1.1.5.0")
            rb = await _get("127.0.0.1", port_b, COMMUNITY, "1.3.6.1.2.1.1.5.0")
            return str(ra[2][0][1]), str(rb[2][0][1])
        finally:
            a.stop()
            b.stop()

    name_a, name_b = asyncio.run(main())
    assert name_a != name_b
    assert name_b == "second-switch"


def test_tree_rebuilds_when_port_layout_changes(ctx):
    """Adding a port must grow the tree; values alone need no rebuild."""
    agent = SwitchAgent(ctx, "127.0.0.1", free_port(), COMMUNITY)
    before = len(agent.tree)

    assert agent.refresh() is False, "no change should not trigger a rebuild"

    ctx.switch.raw["ports"].append(
        {"port": "11", "linkUp": True, "linkSpeed": 1000, "stp": {}, "settings": {}}
    )
    assert agent.refresh() is True
    assert len(agent.tree) > before
    assert agent.tree.get((1, 3, 6, 1, 2, 1, 2, 2, 1, 8, 11)) is not None
