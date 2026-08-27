"""POWER-ETHERNET-MIB (RFC 3621, 1.3.6.1.2.1.105).

The correct standard home for PoE state, which means PoE port status graphs and
total-consumption alerts work in any NMS with no vendor MIB loaded.

RFC 3621 has one significant gap: **there is no per-port power draw object.**
It models detection status, type and classification, but not watts per port. So
Firewalla's ``poePower`` is published in the vendor subtree instead (in mW, to
stay integral) while everything with a standard home lands here.

Only PoE-capable ports get a row, keyed on the port reporting any PoE state at
all -- not on ``poe: true``, which appears only while actually delivering and
would make rows vanish whenever a powered device unplugged.
"""

from __future__ import annotations

from pysnmp.proto import rfc1902

from . import SwitchContext, truth

PSE_PORT_ENTRY = (1, 3, 6, 1, 2, 1, 105, 1, 1, 1)
MAIN_PSE_ENTRY = (1, 3, 6, 1, 2, 1, 105, 1, 3, 1)

#: All ports live in PSE group 1; the SE is a single-PSE chassis.
GROUP = 1


def build(tree, ctx: SwitchContext) -> None:
    poe_ports = [p for p in ctx.switch.ports if p.poe_capable]

    for port in poe_ports:
        n = port.number
        idx = (GROUP, n)

        def p(num=n):
            for cand in ctx.switch.ports:
                if cand.number == num:
                    return cand
            return None

        tree.set(PSE_PORT_ENTRY + (1,) + idx, lambda: rfc1902.Integer(GROUP))
        tree.set(PSE_PORT_ENTRY + (2,) + idx, lambda num=n: rfc1902.Integer(num))
        # pethPsePortAdminEnable: PoE is enabled on the port. The API reports no
        # separate admin toggle, and a port that reports PoE state is by
        # definition not administratively disabled.
        tree.set(PSE_PORT_ENTRY + (3,) + idx, lambda: rfc1902.Integer(truth(True)))
        # pethPsePortPowerPairsControlAbility: false -- pair selection is not
        # exposed or controllable through this API.
        tree.set(PSE_PORT_ENTRY + (4,) + idx, lambda: rfc1902.Integer(truth(False)))
        # pethPsePortPowerPairs: signal(1). Standard mode-A/endspan delivery.
        tree.set(PSE_PORT_ENTRY + (5,) + idx, lambda: rfc1902.Integer(1))
        # pethPsePortDetectionStatus -- the key object. Maps Firewalla's
        # poeStatus onto disabled(1)/searching(2)/deliveringPower(3)/fault(4).
        tree.set(
            PSE_PORT_ENTRY + (6,) + idx,
            lambda f=p: rfc1902.Integer(f().poe_detection_status if f() else 1),
        )
        # pethPsePortType: the negotiated standard, e.g. "802.3at".
        tree.set(
            PSE_PORT_ENTRY + (9,) + idx,
            lambda f=p: rfc1902.OctetString(((f().poe_mode if f() else None) or "").encode()),
        )
        # Omitted on purpose: pethPsePortPowerPriority,
        # pethPsePortMPSAbsentCounter, pethPsePortInvalidSignatureCounter,
        # pethPsePortPowerDeniedCounter, pethPsePortOverConsumptionCounter,
        # pethPsePortShortCounter, pethPsePortPowerClassifications.
        # The API supplies none of them. Publishing zeros would read as
        # "no PoE faults have ever occurred", which we cannot substantiate;
        # power class in particular is negotiated, so deriving it from measured
        # draw would be wrong rather than merely unknown.

    if poe_ports:
        tree.set(MAIN_PSE_ENTRY + (1, GROUP), lambda: rfc1902.Integer(GROUP))
        # pethMainPseOperStatus: on(1) whenever the switch has PoE ports.
        tree.set(MAIN_PSE_ENTRY + (3, GROUP), lambda: rfc1902.Integer(1))
        # pethMainPsePower (the nominal budget) is omitted: the API reports
        # `budgetUtil` but not the budget itself, and `budgetUtil` does not
        # behave as a percentage of `used` (observed 114 alongside 10.5 W), so
        # its meaning is unclear. It is passed through verbatim in the vendor
        # subtree rather than guessed at here.

    if ctx.switch.poe_used_watts is not None:
        # pethMainPseConsumptionPower, in whole watts per RFC 3621. The vendor
        # subtree carries the same figure in mW where precision matters.
        tree.set(
            MAIN_PSE_ENTRY + (4, GROUP),
            lambda: rfc1902.Gauge32(int(round(ctx.switch.poe_used_watts or 0))),
        )
