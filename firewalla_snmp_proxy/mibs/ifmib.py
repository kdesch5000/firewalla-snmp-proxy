"""IF-MIB: ifTable (1.3.6.1.2.1.2) and ifXTable (1.3.6.1.2.1.31).

This is the payload of the whole project. Every SNMP monitoring system already
knows how to graph an ifEntry, so mapping Firewalla's port counters here is
what makes the proxy work with any NMS instead of just one.

Notable mappings:

* ``statsSinceTs`` -> ``ifCounterDiscontinuityTime``. An exact semantic match:
  RFC 2863 defines it as the time of the last counter discontinuity, which is
  precisely what the Firewalla field reports. NMSes that honour it will discard
  a suspect delta on their own.
* 64-bit ``ifHC*`` counters carry the real values; the 32-bit columns are the
  same monotonic numbers folded to 32 bits, so wrap behaves exactly as a real
  agent's would.
* ``ifAlias`` carries the operator-meaningful label (uplink target, or
  "Access: <VLAN name>"), which is what most NMSes display next to the port.

Deliberately **not** published, because the API does not supply them and a
guess would read as fact: ``ifPhysAddress`` (no per-port MAC),
``ifInUnknownProtos``, ``ifOutQLen``, ``ifSpecific``, ``ifLastChange``.
"""

from __future__ import annotations

from pysnmp.proto import rfc1902

from . import SwitchContext

IF_NUMBER = (1, 3, 6, 1, 2, 1, 2, 1, 0)
IF_ENTRY = (1, 3, 6, 1, 2, 1, 2, 2, 1)
IFX_ENTRY = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1)

ETHERNET_CSMACD = 6
#: IEEE 802.3 default payload MTU. The API exposes no per-port MTU and does not
#: report whether jumbo frames are enabled, so this is the standard default.
#: Documented as an assumption in the README rather than silently implied.
ASSUMED_MTU = 1500
GAUGE32_MAX = 4294967295


def _if_speed(mbps: int) -> int:
    """ifSpeed in bits/sec, saturated at Gauge32 max.

    RFC 2863 requires saturation (not wrap) above 4.29 Gbit/s, with the true
    rate reported via ifHighSpeed in Mbit/s.
    """
    bps = int(mbps) * 1_000_000
    return min(bps, GAUGE32_MAX)


def _descr(ctx: SwitchContext, port) -> str:
    """ifDescr: stable physical identification of the port."""
    label = "Port %d" % port.number
    if port.is_sfp:
        label += " (SFP)"
    return label


def build(tree, ctx: SwitchContext) -> None:
    ports = ctx.switch.ports
    tree.set(IF_NUMBER, lambda: rfc1902.Integer(len(ctx.switch.ports)))

    for port in ports:
        n = port.number

        def p(num=n):
            """Re-resolve the port on each access so values stay live."""
            for cand in ctx.switch.ports:
                if cand.number == num:
                    return cand
            return None

        # -- ifTable ----------------------------------------------------
        tree.set(IF_ENTRY + (1, n), lambda num=n: rfc1902.Integer(num))
        tree.set(
            IF_ENTRY + (2, n),
            lambda f=p: rfc1902.OctetString(_descr(ctx, f()).encode()) if f() else rfc1902.OctetString(b""),
        )
        tree.set(IF_ENTRY + (3, n), lambda: rfc1902.Integer(ETHERNET_CSMACD))
        tree.set(IF_ENTRY + (4, n), lambda: rfc1902.Integer(ASSUMED_MTU))
        tree.set(
            IF_ENTRY + (5, n),
            lambda f=p: rfc1902.Gauge32(_if_speed(f().speed_mbps) if f() else 0),
        )
        # ifAdminStatus: the API exposes no admin/config state for a port, only
        # link state. up(1) is reported so an NMS does not read every port as
        # administratively shut; ifOperStatus carries the real signal.
        tree.set(IF_ENTRY + (7, n), lambda: rfc1902.Integer(1))
        tree.set(
            IF_ENTRY + (8, n),
            lambda f=p: rfc1902.Integer(f().oper_status if f() else 2),
        )
        tree.set(IF_ENTRY + (10, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "rxBytes")))
        tree.set(IF_ENTRY + (11, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "rxUnicastFrames")))
        # ifInNUcastPkts / ifOutNUcastPkts are deprecated but still read by
        # older NMSes; ifXTable carries the split multicast/broadcast values.
        tree.set(
            IF_ENTRY + (12, n),
            lambda num=n: rfc1902.Counter32(
                ctx.c32_sum(num, "rxMulticastFrames", "rxBroadcastFrames")
            ),
        )
        tree.set(IF_ENTRY + (13, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "rxDiscardFrames")))
        tree.set(IF_ENTRY + (14, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "rxErrorFrames")))
        tree.set(IF_ENTRY + (16, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "txBytes")))
        tree.set(IF_ENTRY + (17, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "txUnicastFrames")))
        tree.set(
            IF_ENTRY + (18, n),
            lambda num=n: rfc1902.Counter32(
                ctx.c32_sum(num, "txMulticastFrames", "txBroadcastFrames")
            ),
        )
        tree.set(IF_ENTRY + (19, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "txDiscardFrames")))
        tree.set(IF_ENTRY + (20, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "txErrorFrames")))

        # -- ifXTable ---------------------------------------------------
        # ifName is the short canonical name; NMSes prefer it for graph titles.
        tree.set(IFX_ENTRY + (1, n), lambda num=n: rfc1902.OctetString(str(num).encode()))
        tree.set(IFX_ENTRY + (2, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "rxMulticastFrames")))
        tree.set(IFX_ENTRY + (3, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "rxBroadcastFrames")))
        tree.set(IFX_ENTRY + (4, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "txMulticastFrames")))
        tree.set(IFX_ENTRY + (5, n), lambda num=n: rfc1902.Counter32(ctx.c32(num, "txBroadcastFrames")))
        tree.set(IFX_ENTRY + (6, n), lambda num=n: rfc1902.Counter64(ctx.c64(num, "rxBytes")))
        tree.set(IFX_ENTRY + (7, n), lambda num=n: rfc1902.Counter64(ctx.c64(num, "rxUnicastFrames")))
        tree.set(IFX_ENTRY + (8, n), lambda num=n: rfc1902.Counter64(ctx.c64(num, "rxMulticastFrames")))
        tree.set(IFX_ENTRY + (9, n), lambda num=n: rfc1902.Counter64(ctx.c64(num, "rxBroadcastFrames")))
        tree.set(IFX_ENTRY + (10, n), lambda num=n: rfc1902.Counter64(ctx.c64(num, "txBytes")))
        tree.set(IFX_ENTRY + (11, n), lambda num=n: rfc1902.Counter64(ctx.c64(num, "txUnicastFrames")))
        tree.set(IFX_ENTRY + (12, n), lambda num=n: rfc1902.Counter64(ctx.c64(num, "txMulticastFrames")))
        tree.set(IFX_ENTRY + (13, n), lambda num=n: rfc1902.Counter64(ctx.c64(num, "txBroadcastFrames")))
        # ifLinkUpDownTrapEnable: disabled(2). The proxy polls on an interval and
        # cannot observe a transition at the moment it happens, so promising
        # link traps would be a lie.
        tree.set(IFX_ENTRY + (14, n), lambda: rfc1902.Integer(2))
        tree.set(
            IFX_ENTRY + (15, n),
            lambda f=p: rfc1902.Gauge32(f().speed_mbps if f() else 0),
        )
        tree.set(IFX_ENTRY + (16, n), lambda: rfc1902.Integer(2))  # promiscuous: false
        tree.set(IFX_ENTRY + (17, n), lambda: rfc1902.Integer(1))  # connector present: true
        tree.set(
            IFX_ENTRY + (18, n),
            lambda f=p: rfc1902.OctetString(
                (ctx.switch.port_alias(f()) if f() else "").encode()
            ),
        )
        tree.set(
            IFX_ENTRY + (19, n),
            lambda f=p: rfc1902.TimeTicks(
                ctx.switch.ticks_at_epoch(f().stats_since) if f() else 0
            ),
        )
