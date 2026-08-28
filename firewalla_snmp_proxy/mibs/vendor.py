"""FIREWALLA-SNMP-PROXY-MIB: the vendor subtree.

Holds the switch data that has no home in any standard MIB:

* **per-port PoE wattage** -- RFC 3621 models PoE status but has no per-port
  power object at all;
* **STP port role** (root/designated/alternate) -- lives in
  IEEE8021-SPANNING-TREE-MIB, which few NMSes implement;
* **SFP transceiver detail** -- connector type, bit rate, vendor OUI;
* **ACL usage** -- entirely Firewalla-specific.

Plus a proxy health group, which matters more than it looks: without it an
operator cannot distinguish "the switch is quiet" from "the proxy stopped
polling and is serving stale numbers". ``fwProxyPollStatus`` and
``fwProxySecondsSincePoll`` make that visible, and are the right things to
alert on.

``fwProxyIcmpStatus``, ``fwProxyIcmpRtt`` and ``fwProxyServingCache`` extend
that idea to the case where the MSP API is unavailable entirely. The proxy then
serves its cached payload so the NMS keeps seeing the device and its full port
set, and these three objects are what say so out loud -- ICMP supplies live
proof the switch is (or is not) alive, at no API cost, while the counters are
frozen and therefore reading as zero traffic.

The base OID is configurable (``enterprise_oid`` in config.yaml) and defaults
to a placeholder in unassigned space -- see the README. Everything is read-only.
"""

from __future__ import annotations

from pysnmp.proto import rfc1902

from . import SwitchContext, truth

# Subtree layout, relative to the configured enterprise base:
#   .1  fwProxy      - proxy health
#   .2  fwSwitch     - switch-wide scalars
#   .3.1.1 fwPortEntry - per-port table, indexed by ifIndex
#
# Note the depth of PORT_ENTRY: fwPortTable is .3.1 and fwPortEntry is .3.1.1,
# so columns are .3.1.1.<col>.<ifIndex>. Flattening this to .3.1.<col> still
# walks cleanly but makes every column resolve as a bare "fwPortTable.<col>"
# against the shipped MIB -- the served OIDs must match the MIB exactly.
PROXY = (1,)
SWITCH = (2,)
PORT_ENTRY = (3, 1, 1)


def _oct(value) -> rfc1902.OctetString:
    return rfc1902.OctetString(("" if value is None else str(value)).encode())


def build(tree, ctx: SwitchContext) -> None:
    base = ctx.enterprise

    # -- fwProxy: proxy health ------------------------------------------
    tree.set(base + PROXY + (1, 0), lambda: _oct(ctx.proxy_version))
    tree.set(base + PROXY + (2, 0), lambda: rfc1902.Integer(ctx.seconds_since_poll()))
    # fwProxyPollStatus: ok(1) / stale(2) / error(3) -- the object to alert on.
    tree.set(base + PROXY + (3, 0), lambda: rfc1902.Integer(ctx.poll_status()))
    tree.set(base + PROXY + (4, 0), lambda: rfc1902.Gauge32(max(0, ctx.api_latency_ms)))
    tree.set(base + PROXY + (5, 0), lambda: _oct(ctx.last_error))
    tree.set(base + PROXY + (6, 0), lambda: rfc1902.Counter32(ctx.poll_count))
    # fwProxyIcmpStatus / fwProxyIcmpRtt: live evidence about the switch that
    # costs no API quota, and therefore stays valid through a rate-limit
    # lockout when every API-derived value has gone stale.
    tree.set(
        base + PROXY + (7, 0),
        lambda: rfc1902.Integer(
            ctx.reach.snmp_status() if ctx.reach is not None else 4
        ),
    )
    # Microseconds, so sub-millisecond LAN round-trips survive the integer.
    tree.set(
        base + PROXY + (8, 0),
        lambda: rfc1902.Gauge32(
            int(round((ctx.reach.rtt_ms or 0.0) * 1000))
            if ctx.reach is not None else 0
        ),
    )
    # fwProxyServingCache: 1 while the port layout and counters come from the
    # on-disk cache rather than a live poll. Without this, cached counters are
    # indistinguishable from an idle switch.
    tree.set(
        base + PROXY + (9, 0),
        lambda: rfc1902.Integer(1 if ctx.serving_cache else 2),
    )

    # -- fwSwitch: switch-wide scalars ----------------------------------
    S = base + SWITCH
    sw = lambda: ctx.switch  # noqa: E731 - terse accessor used throughout below

    tree.set(S + (1, 0), lambda: _oct(sw().mac))
    tree.set(S + (2, 0), lambda: _oct(sw().name))
    tree.set(S + (3, 0), lambda: _oct(sw().model))
    tree.set(S + (4, 0), lambda: _oct(sw().serial))
    tree.set(S + (5, 0), lambda: _oct(sw().firmware_rev))
    tree.set(S + (6, 0), lambda: _oct(sw().hardware_rev))
    tree.set(S + (7, 0), lambda: _oct(sw().protocol_version))
    tree.set(S + (8, 0), lambda: _oct(sw().active_branch))
    # ICMP outranks the API's stale 'online' field -- see switch_online().
    tree.set(S + (9, 0), lambda: rfc1902.Integer(truth(ctx.switch_online())))
    tree.set(S + (10, 0), lambda: _oct(sw().healthy_str))
    tree.set(S + (11, 0), lambda: _oct(sw().fan_status))
    tree.set(S + (12, 0), lambda: _oct(sw().ip))

    # PoE totals. Milliwatts preserves the API's 0.1 W resolution, which the
    # whole-watt Gauge32 in POWER-ETHERNET-MIB necessarily discards.
    tree.set(
        S + (13, 0),
        lambda: rfc1902.Gauge32(int(round((sw().poe_used_watts or 0.0) * 1000))),
    )
    # budgetUtil is passed through verbatim and intentionally uninterpreted: it
    # was observed reading 114 alongside 10.5 W used, so it is not a percentage
    # of consumption and its true meaning is undocumented.
    if sw().poe_budget_util is not None:
        tree.set(S + (14, 0), lambda: rfc1902.Gauge32(sw().poe_budget_util or 0))

    # ACL usage. `count` = tracking + control; `max` appears to bound only the
    # control entries (observed count 1229 against max 256), so all four are
    # published rather than deriving a single utilisation figure that would be
    # wrong.
    acl = lambda k: rfc1902.Gauge32(int(ctx.switch.acl.get(k) or 0))  # noqa: E731
    tree.set(S + (15, 0), lambda: acl("count"))
    tree.set(S + (16, 0), lambda: acl("max"))
    tree.set(S + (17, 0), lambda: acl("tracking"))
    tree.set(S + (18, 0), lambda: acl("control"))

    if sw().flow_control is not None:
        tree.set(S + (19, 0), lambda: rfc1902.Integer(truth(sw().flow_control)))
    if sw().stp_enabled is not None:
        tree.set(S + (20, 0), lambda: rfc1902.Integer(truth(sw().stp_enabled)))
    tree.set(S + (21, 0), lambda: _oct(sw().stp_protocol))

    # Uplink, mirrored here in human-readable form; LLDP-MIB carries the
    # machine-readable version an NMS uses for topology.
    tree.set(S + (22, 0), lambda: _oct(sw().uplink.get("mac")))
    tree.set(S + (23, 0), lambda: _oct(sw().uplink.get("port")))
    tree.set(S + (24, 0), lambda: rfc1902.Integer(sw().uplink_local_port or 0))
    tree.set(S + (25, 0), lambda: _oct(sw().uplink.get("type")))
    tree.set(S + (26, 0), lambda: _oct(sw().uplink.get("connectionType")))

    # A/B firmware partitions: which slot is live, and what is in each. Useful
    # for spotting a switch that upgraded but is still running the old slot.
    fw = lambda: ctx.switch.raw.get("firmwareInfo") or {}  # noqa: E731
    tree.set(S + (27, 0), lambda: rfc1902.Integer(int(fw().get("activePartition") or 0)))
    tree.set(S + (28, 0), lambda: _oct(fw().get("partition1Version")))
    tree.set(S + (29, 0), lambda: _oct(fw().get("partition2Version")))

    # -- fwPortEntry: per-port table, indexed by ifIndex -----------------
    P = base + PORT_ENTRY
    for port in ctx.switch.ports:
        n = port.number

        def p(num=n):
            for cand in ctx.switch.ports:
                if cand.number == num:
                    return cand
            return None

        tree.set(P + (1, n), lambda num=n: rfc1902.Integer(num))
        # The headline gap-filler: per-port PoE draw, in mW.
        tree.set(P + (2, n), lambda f=p: rfc1902.Gauge32(f().poe_milliwatts if f() else 0))
        tree.set(P + (3, n), lambda f=p: _oct(f().poe_mode if f() else None))
        tree.set(P + (4, n), lambda f=p: _oct(f().poe_status if f() else None))
        tree.set(P + (5, n), lambda f=p: _oct(f().stp_role if f() else None))
        tree.set(P + (6, n), lambda f=p: _oct(f().stp_state_str if f() else None))
        tree.set(P + (7, n), lambda f=p: _oct(f().port_type if f() else None))
        tree.set(
            P + (8, n),
            lambda f=p: _oct(ctx.switch.network_name(f().network_uuid) if f() else None),
        )
        tree.set(P + (9, n), lambda f=p: _oct(f().network_uuid if f() else None))

        if not port.is_sfp:
            continue
        tree.set(
            P + (10, n),
            lambda f=p: rfc1902.Integer(truth(bool(f().sfp.get("present")) if f() else False)),
        )
        tree.set(P + (11, n), lambda f=p: _oct(f().sfp.get("connectorType") if f() else None))
        tree.set(P + (12, n), lambda f=p: _oct(f().sfp.get("nominalBitRate") if f() else None))
        tree.set(P + (13, n), lambda f=p: _oct(f().sfp.get("vendorOui") if f() else None))
