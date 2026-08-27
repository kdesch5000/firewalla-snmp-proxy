"""pysnmp wiring: one SNMP agent per switch.

Design notes:

* **One UDP port per switch.** Multiplexing several switches onto one port via
  distinct community strings works in some NMSes but breaks any that key a
  device on ``IP:port``. A port each keeps every NMS happy.
* **All engines share one asyncio loop.** pysnmp registers its transport with
  the running loop, so N agents cost N sockets, not N threads.
* **The tree is rebuilt only when its shape changes** (port count, SFP presence,
  PoE capability). Values are live via callables, so an ordinary poll needs no
  rebuild and an NMS never sees rows flicker mid-walk.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto import rfc1902
from pysnmp.proto.api import v2c
from pysnmp.smi.instrum import AbstractMibInstrumController

from .mibs import SwitchContext
from .oid import OidTree
from .tree_builder import build_tree

log = logging.getLogger(__name__)

#: Access-control view root. Note this is ``iso(1)``, not ``1.3.6.1``:
#: LLDP-MIB lives at ``1.0.8802``, outside the internet subtree. Scoping the
#: view to 1.3.6.1 -- the obvious choice -- silently hides the entire neighbour
#: table, and therefore the NMS topology link, with no error anywhere.
VIEW_ROOT = (1,)

#: SNMP security models served: v1 (1) and v2c (2).
SECURITY_MODELS = (1, 2)


class TreeInstrumController(AbstractMibInstrumController):
    """Serves GET / GETNEXT / GETBULK from an :class:`OidTree`.

    SET is refused: this is a monitoring proxy and the upstream API's mutating
    endpoints must never be reachable over SNMP.
    """

    def __init__(self, tree: OidTree) -> None:
        self.tree = tree

    def swap_tree(self, tree: OidTree) -> None:
        self.tree = tree

    # -- GET -------------------------------------------------------------
    def read_variables(self, *varBinds, **context):
        result = []
        for oid, _val in varBinds:
            key = tuple(oid)
            value = self.tree.get(key)
            if value is not None:
                result.append((oid, value))
                continue
            # Distinguish "wrong instance" from "wrong object", as a real agent
            # does. Two ways this OID can still name a real object:
            #   - it is an interior node with instances beneath it
            #     (e.g. a GET on the ifOperStatus column itself), or
            #   - its parent column holds other instances, so the column is
            #     real and only this index is missing (e.g. ifOperStatus.99).
            # Anything else genuinely is not implemented here.
            if self.tree.has_descendants(key) or (
                len(key) > 1 and self.tree.has_descendants(key[:-1])
            ):
                result.append((oid, v2c.NoSuchInstance("")))
            else:
                result.append((oid, v2c.NoSuchObject("")))
        return result

    # -- GETNEXT / GETBULK ----------------------------------------------
    def read_next_variables(self, *varBinds, **context):
        result = []
        for oid, _val in varBinds:
            nxt = self.tree.get_next(tuple(oid))
            if nxt is None:
                result.append((oid, v2c.EndOfMibView("")))
            else:
                next_oid, value = nxt
                result.append((rfc1902.ObjectName(next_oid), value))
        return result

    # -- SET -------------------------------------------------------------
    def write_variables(self, *varBinds, **context):
        from pysnmp.smi import error

        raise error.NotWritableError(
            name=varBinds[0][0] if varBinds else None,
            idx=0,
        )


class SwitchAgent:
    """An SNMP agent serving one Firewalla switch on its own UDP port."""

    def __init__(
        self,
        ctx: SwitchContext,
        listen_host: str,
        listen_port: int,
        community: str = "public",
    ) -> None:
        self.ctx = ctx
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.community = community
        self._signature = ctx.port_signature()
        self.tree = build_tree(ctx)
        self._instrum = TreeInstrumController(self.tree)
        self._engine: Optional[engine.SnmpEngine] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Bind the socket and register command responders.

        Must be called with an asyncio event loop running, since pysnmp's
        asyncio transport attaches to the current loop.
        """
        snmp_engine = engine.SnmpEngine()
        transport = udp.UdpTransport().open_server_mode(
            (self.listen_host, self.listen_port)
        )
        config.add_transport(snmp_engine, udp.DOMAIN_NAME, transport)

        # Read-only v1 + v2c access under one community. writeSubTree is left
        # empty, so SET is rejected by access control before it ever reaches the
        # instrumentation controller.
        config.add_v1_system(snmp_engine, "ro-area", self.community)
        for model in SECURITY_MODELS:
            config.add_vacm_user(
                snmp_engine, model, "ro-area", "noAuthNoPriv", VIEW_ROOT
            )

        snmp_context = context.SnmpContext(snmp_engine)
        snmp_context.unregister_context_name(v2c.OctetString(""))
        snmp_context.register_context_name(v2c.OctetString(""), self._instrum)

        cmdrsp.GetCommandResponder(snmp_engine, snmp_context)
        cmdrsp.NextCommandResponder(snmp_engine, snmp_context)
        cmdrsp.BulkCommandResponder(snmp_engine, snmp_context)
        # Registered deliberately even though writes are denied: without a SET
        # responder the agent simply does not reply, and the manager sees a
        # timeout that looks like the agent is down. With it, a SET gets a
        # clean notWritable/noAccess error. VACM (empty writeSubTree) rejects
        # the request before it reaches the instrumentation controller, whose
        # write_variables is a second line of defence.
        cmdrsp.SetCommandResponder(snmp_engine, snmp_context)

        self._engine = snmp_engine
        log.info(
            "SNMP agent for %s listening on %s:%d (%d objects)",
            self.ctx.switch.name, self.listen_host, self.listen_port, len(self.tree),
        )

    def stop(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close_dispatcher()
            except Exception as exc:  # pragma: no cover - shutdown best effort
                log.debug("error closing dispatcher: %s", exc)
            self._engine = None

    # -- refresh ---------------------------------------------------------
    def refresh(self) -> bool:
        """Rebuild the tree if the port set changed. Returns True if rebuilt."""
        signature = self.ctx.port_signature()
        if signature == self._signature:
            return False
        log.info(
            "port layout changed on %s; rebuilding OID tree", self.ctx.switch.name
        )
        self._signature = signature
        self.tree = build_tree(self.ctx)
        self._instrum.swap_tree(self.tree)
        return True
