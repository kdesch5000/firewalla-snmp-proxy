"""BRIDGE-MIB (RFC 1493, 1.3.6.1.2.1.17): base bridge info and STP port state.

``stp.portState`` maps cleanly onto ``dot1dStpPortState``, with RSTP's
"discarding" collapsing onto the legacy ``blocking(2)`` -- which is what
RSTP-aware NMSes expect from a BRIDGE-MIB view.

``stp.portRole`` (root/designated/alternate) has no BRIDGE-MIB equivalent; it
belongs to IEEE8021-SPANNING-TREE-MIB, which few NMSes implement, so it is
published in the vendor subtree instead.

The dot1dStp group is only instantiated when STP is actually enabled on the
switch, so an NMS does not draw spanning-tree state for a switch not running it.
"""

from __future__ import annotations

from pysnmp.proto import rfc1902

from . import SwitchContext

DOT1D_BASE = (1, 3, 6, 1, 2, 1, 17, 1)
DOT1D_BASE_PORT_ENTRY = (1, 3, 6, 1, 2, 1, 17, 1, 4, 1)
DOT1D_STP = (1, 3, 6, 1, 2, 1, 17, 2)
DOT1D_STP_PORT_ENTRY = (1, 3, 6, 1, 2, 1, 17, 2, 15, 1)

TRANSPARENT_ONLY = 2  # dot1dBaseType
IEEE8021D = 3  # dot1dStpProtocolSpecification; RSTP still reports ieee8021d


def build(tree, ctx: SwitchContext) -> None:
    sw = ctx.switch

    if sw.mac_bytes:
        tree.set(DOT1D_BASE + (1, 0), lambda: rfc1902.OctetString(ctx.switch.mac_bytes))
    tree.set(DOT1D_BASE + (2, 0), lambda: rfc1902.Integer(len(ctx.switch.ports)))
    tree.set(DOT1D_BASE + (3, 0), lambda: rfc1902.Integer(TRANSPARENT_ONLY))

    for port in sw.ports:
        n = port.number
        # Bridge port number == ifIndex == physical port number here, which
        # keeps dot1dBasePortIfIndex a straightforward identity mapping.
        tree.set(DOT1D_BASE_PORT_ENTRY + (1, n), lambda num=n: rfc1902.Integer(num))
        tree.set(DOT1D_BASE_PORT_ENTRY + (2, n), lambda num=n: rfc1902.Integer(num))

    if not sw.stp_enabled:
        return

    tree.set(DOT1D_STP + (1, 0), lambda: rfc1902.Integer(IEEE8021D))

    for port in sw.ports:
        n = port.number

        def p(num=n):
            for cand in ctx.switch.ports:
                if cand.number == num:
                    return cand
            return None

        tree.set(DOT1D_STP_PORT_ENTRY + (1, n), lambda num=n: rfc1902.Integer(num))
        tree.set(
            DOT1D_STP_PORT_ENTRY + (3, n),
            lambda f=p: rfc1902.Integer(f().stp_port_state if f() else 1),
        )
        # dot1dStpPortEnable: enabled(1)/disabled(2), from the port's STP role.
        tree.set(
            DOT1D_STP_PORT_ENTRY + (4, n),
            lambda f=p: rfc1902.Integer(
                2 if (f() and str(f().stp_role or "").lower() == "disabled") else 1
            ),
        )
        # Omitted: dot1dStpPortPriority, dot1dStpPortPathCost,
        # dot1dStpPortDesignatedRoot/Bridge/Port, dot1dStpPortForwardTransitions.
        # None are exposed by the API.
