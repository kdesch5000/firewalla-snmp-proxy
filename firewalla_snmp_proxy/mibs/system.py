"""SNMPv2-MIB system group (1.3.6.1.2.1.1).

Every NMS reads this group first to identify a device, so it decides how the
switch is labelled everywhere in the UI.
"""

from __future__ import annotations

from pysnmp.proto import rfc1902

from . import SwitchContext

SYSTEM = (1, 3, 6, 1, 2, 1, 1)

#: sysServices bitmask: sum of 2^(layer-1). datalink(2) -> 2, i.e. an L2 bridge.
SYS_SERVICES_L2 = 2


def sys_descr(ctx: SwitchContext) -> str:
    """A single human-readable identity line.

    Deliberately front-loads vendor and model so NMS auto-detection heuristics
    (which frequently regex sysDescr) have the best chance of classifying this
    as a Firewalla switch.
    """
    sw = ctx.switch
    bits = ["Firewalla", sw.model_name or "Switch"]
    if sw.model and sw.model != sw.model_name:
        bits.append("(%s)" % sw.model)
    if sw.firmware_rev:
        bits.append("firmware %s" % sw.firmware_rev)
    if sw.hardware_rev:
        bits.append("hardware %s" % sw.hardware_rev)
    if sw.serial:
        bits.append("S/N %s" % sw.serial)
    bits.append("- proxied by firewalla-snmp-proxy %s" % ctx.proxy_version)
    return " ".join(str(b) for b in bits)


def build(tree, ctx: SwitchContext) -> None:
    tree.set(SYSTEM + (1, 0), lambda: rfc1902.OctetString(sys_descr(ctx).encode()))
    # sysObjectID is how an NMS keys "what kind of thing is this", and so which
    # OS definition, icon and graph set to apply. Defaults into our own
    # enterprise subtree; overridable via sys_object_id for drop-in migrations.
    tree.set(
        SYSTEM + (2, 0),
        lambda: rfc1902.ObjectIdentifier(ctx.sys_object_id),
    )
    tree.set(SYSTEM + (3, 0), lambda: rfc1902.TimeTicks(ctx.switch.uptime_ticks))
    tree.set(SYSTEM + (4, 0), lambda: rfc1902.OctetString(ctx.sys_contact.encode()))
    tree.set(
        SYSTEM + (5, 0),
        lambda: rfc1902.OctetString(
            (ctx.switch.hostname or ctx.switch.name).encode()
        ),
    )
    tree.set(SYSTEM + (6, 0), lambda: rfc1902.OctetString(ctx.sys_location.encode()))
    tree.set(SYSTEM + (7, 0), lambda: rfc1902.Integer(SYS_SERVICES_L2))
