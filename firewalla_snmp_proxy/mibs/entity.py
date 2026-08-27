"""ENTITY-MIB (RFC 4133, 1.3.6.1.2.1.47): physical inventory.

Gives an NMS the chassis identity -- model, serial, hardware and firmware
revisions -- in the standard place, which is where inventory reports and
"firmware out of date" checks look.

Entity layout::

    1            chassis            (entPhysicalClass chassis(3))
    100 + n      port n             (port(10)),   contained in 1
    200 + n      SFP transceiver    (module(9)),  contained in 100 + n

Transceiver entities appear only for ports that actually report an SFP cage.
"""

from __future__ import annotations

from pysnmp.proto import rfc1902

from . import SwitchContext, truth

ENT_PHYSICAL_ENTRY = (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1)

CLASS_CHASSIS = 3
CLASS_MODULE = 9
CLASS_PORT = 10

CHASSIS_IDX = 1
PORT_IDX_BASE = 100
SFP_IDX_BASE = 200

MFG_NAME = "Firewalla Inc"


def _oct(value) -> rfc1902.OctetString:
    return rfc1902.OctetString(("" if value is None else str(value)).encode())


def build(tree, ctx: SwitchContext) -> None:
    sw = ctx.switch
    E = ENT_PHYSICAL_ENTRY

    # -- chassis ---------------------------------------------------------
    tree.set(E + (1, CHASSIS_IDX), lambda: rfc1902.Integer(CHASSIS_IDX))
    tree.set(E + (2, CHASSIS_IDX), lambda: _oct(ctx.switch.model_name or "Firewalla Switch"))
    tree.set(E + (3, CHASSIS_IDX), lambda: rfc1902.ObjectIdentifier(ctx.sys_object_id))
    # entPhysicalContainedIn 0 == not contained in anything (the root).
    tree.set(E + (4, CHASSIS_IDX), lambda: rfc1902.Integer(0))
    tree.set(E + (5, CHASSIS_IDX), lambda: rfc1902.Integer(CLASS_CHASSIS))
    tree.set(E + (6, CHASSIS_IDX), lambda: rfc1902.Integer(-1))
    tree.set(E + (7, CHASSIS_IDX), lambda: _oct(ctx.switch.name))
    tree.set(E + (8, CHASSIS_IDX), lambda: _oct(ctx.switch.hardware_rev))
    tree.set(E + (9, CHASSIS_IDX), lambda: _oct(ctx.switch.firmware_rev))
    # entPhysicalSoftwareRev: the switch's MSP-side agent version, which the API
    # reports separately from firmware.
    tree.set(E + (10, CHASSIS_IDX), lambda: _oct(ctx.switch.software_rev))
    tree.set(E + (11, CHASSIS_IDX), lambda: _oct(ctx.switch.serial))
    tree.set(E + (12, CHASSIS_IDX), lambda: _oct(MFG_NAME))
    tree.set(E + (13, CHASSIS_IDX), lambda: _oct(ctx.switch.model_name))
    tree.set(E + (16, CHASSIS_IDX), lambda: rfc1902.Integer(truth(False)))

    # -- ports and transceivers -----------------------------------------
    for port in sw.ports:
        n = port.number
        pidx = PORT_IDX_BASE + n

        def p(num=n):
            for cand in ctx.switch.ports:
                if cand.number == num:
                    return cand
            return None

        tree.set(E + (1, pidx), lambda i=pidx: rfc1902.Integer(i))
        tree.set(E + (2, pidx), lambda num=n: _oct("Port %d" % num))
        tree.set(E + (4, pidx), lambda: rfc1902.Integer(CHASSIS_IDX))
        tree.set(E + (5, pidx), lambda: rfc1902.Integer(CLASS_PORT))
        tree.set(E + (6, pidx), lambda num=n: rfc1902.Integer(num))
        tree.set(E + (7, pidx), lambda num=n: _oct(str(num)))
        # entPhysicalIsFRU: a fixed port is not field-replaceable.
        tree.set(E + (16, pidx), lambda: rfc1902.Integer(truth(False)))

        if not port.is_sfp:
            continue

        sidx = SFP_IDX_BASE + n
        tree.set(E + (1, sidx), lambda i=sidx: rfc1902.Integer(i))
        tree.set(
            E + (2, sidx),
            lambda f=p: _oct(
                "SFP transceiver: %s" % ((f().sfp.get("connectorType") or "unknown") if f() else "unknown")
                if (f() and f().sfp.get("present"))
                else "SFP cage (empty)"
            ),
        )
        tree.set(E + (4, sidx), lambda i=pidx: rfc1902.Integer(i))
        tree.set(E + (5, sidx), lambda: rfc1902.Integer(CLASS_MODULE))
        tree.set(E + (6, sidx), lambda: rfc1902.Integer(1))
        tree.set(E + (7, sidx), lambda num=n: _oct("Port %d SFP" % num))
        # An SFP module is genuinely field-replaceable, unlike a fixed port.
        tree.set(E + (16, sidx), lambda: rfc1902.Integer(truth(True)))
