"""LLDP-MIB (IEEE 802.1AB, 1.0.8802.1.1.2): neighbour discovery.

The switch does not speak LLDP to us, but the MSP API tells us exactly what its
uplink is connected to (``uplink: {mac, type, port, localPort}``). Publishing
that as a single lldpRemTable entry lets Observium, LibreNMS and Zabbix **draw
the switch-to-Firewalla link on their topology maps automatically**, because
that is the table they already read to discover neighbours.

Note the OID root is ``1.0.8802`` -- ``iso(1).std(0).iso8802(8802)`` -- not
under ``1.3.6.1`` like the other MIBs here. The SNMP engine's access-control
view has to cover it explicitly or the whole subtree is invisible.

Index columns (lldpRemTimeMark, lldpRemLocalPortNum, lldpRemIndex) are
not-accessible per the MIB definition, so they are correctly *not*
instantiated; they exist only inside the row index.
"""

from __future__ import annotations

from pysnmp.proto import rfc1902

from . import SwitchContext
from .system import sys_descr

LLDP_LOC = (1, 0, 8802, 1, 1, 2, 1, 3)
LLDP_LOC_PORT_ENTRY = LLDP_LOC + (7, 1)
LLDP_REM_ENTRY = (1, 0, 8802, 1, 1, 2, 1, 4, 1, 1)

CHASSIS_ID_MAC = 4  # LldpChassisIdSubtype macAddress(4)
PORT_ID_IFNAME = 5  # LldpPortIdSubtype interfaceName(5)

#: LldpSystemCapabilitiesMap BITS with bridge(2) set -> 0b0010_0000.
CAP_BRIDGE = b"\x20"

#: All neighbour rows use timeMark 0: the API gives a current snapshot with no
#: age information, and 0 is the conventional "no ageing data" value.
TIME_MARK = 0
REM_INDEX = 1


def build(tree, ctx: SwitchContext) -> None:
    sw = ctx.switch

    # -- local system ----------------------------------------------------
    tree.set(LLDP_LOC + (1, 0), lambda: rfc1902.Integer(CHASSIS_ID_MAC))
    if sw.mac_bytes:
        tree.set(LLDP_LOC + (2, 0), lambda: rfc1902.OctetString(ctx.switch.mac_bytes))
    tree.set(
        LLDP_LOC + (3, 0),
        lambda: rfc1902.OctetString((ctx.switch.hostname or ctx.switch.name).encode()),
    )
    tree.set(LLDP_LOC + (4, 0), lambda: rfc1902.OctetString(sys_descr(ctx).encode()))
    tree.set(LLDP_LOC + (5, 0), lambda: rfc1902.OctetString(CAP_BRIDGE))
    tree.set(LLDP_LOC + (6, 0), lambda: rfc1902.OctetString(CAP_BRIDGE))

    # -- local ports -----------------------------------------------------
    for port in sw.ports:
        n = port.number
        tree.set(LLDP_LOC_PORT_ENTRY + (2, n), lambda: rfc1902.Integer(PORT_ID_IFNAME))
        tree.set(LLDP_LOC_PORT_ENTRY + (3, n), lambda num=n: rfc1902.OctetString(str(num).encode()))
        tree.set(
            LLDP_LOC_PORT_ENTRY + (4, n),
            lambda num=n: rfc1902.OctetString(("Port %d" % num).encode()),
        )

    # -- the uplink neighbour -------------------------------------------
    local_port = sw.uplink_local_port
    uplink = sw.uplink
    if not local_port or not uplink:
        return  # no uplink reported: publish no neighbour rather than a guess

    idx = (TIME_MARK, local_port, REM_INDEX)

    def up(key, default=""):
        return lambda: rfc1902.OctetString(str(ctx.switch.uplink.get(key) or default).encode())

    tree.set(LLDP_REM_ENTRY + (4,) + idx, lambda: rfc1902.Integer(CHASSIS_ID_MAC))
    from ..model import _mac_to_bytes  # local import: avoids a cycle at module load

    tree.set(
        LLDP_REM_ENTRY + (5,) + idx,
        lambda: rfc1902.OctetString(_mac_to_bytes(ctx.switch.uplink.get("mac"))),
    )
    tree.set(LLDP_REM_ENTRY + (6,) + idx, lambda: rfc1902.Integer(PORT_ID_IFNAME))
    # e.g. "eth1" -- the interface on the Firewalla box this switch plugs into.
    tree.set(LLDP_REM_ENTRY + (7,) + idx, up("port"))
    tree.set(LLDP_REM_ENTRY + (8,) + idx, up("port"))
    # lldpRemSysName: the MAC is the only stable identifier the uplink record
    # gives us; the box's friendly name is not included in this response.
    tree.set(
        LLDP_REM_ENTRY + (9,) + idx,
        lambda: rfc1902.OctetString(str(ctx.switch.uplink.get("mac") or "").encode()),
    )
    tree.set(
        LLDP_REM_ENTRY + (10,) + idx,
        lambda: rfc1902.OctetString(
            (
                "Firewalla %s (uplink via %s)"
                % (
                    ctx.switch.uplink.get("type") or "device",
                    ctx.switch.uplink.get("connectionType") or "ethernet",
                )
            ).encode()
        ),
    )
