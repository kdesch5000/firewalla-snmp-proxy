"""Assemble every MIB module into one frozen OID tree."""

from __future__ import annotations

import logging

from .mibs import SwitchContext, bridge, entity, ifmib, lldp, poe, sensor, system, vendor
from .oid import OidTree

log = logging.getLogger(__name__)

#: Order is irrelevant to correctness (the tree sorts itself), but is kept in
#: rough OID order for readability of debug output.
MODULES = (system, ifmib, bridge, poe, entity, sensor, lldp, vendor)


def build_tree(ctx: SwitchContext) -> OidTree:
    tree = OidTree()
    for module in MODULES:
        before = len(tree)
        module.build(tree, ctx)
        log.debug("%s registered %d OIDs", module.__name__, len(tree) - before)
    tree.freeze()
    log.info(
        "built OID tree for %s: %d objects across %d ports",
        ctx.switch.name, len(tree), len(ctx.switch.ports),
    )
    return tree
