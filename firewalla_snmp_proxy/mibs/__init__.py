"""MIB modules: each builds part of the OID tree for one switch.

Every module exposes ``build(tree, ctx)``. Values are registered as callables
closing over ``ctx``, so the tree is built once at startup but serves live data
on every request -- an NMS never sees rows appear or vanish between the two
halves of a GETNEXT pair.

Guiding rule throughout: **if the API does not supply a value, the OID is not
instantiated.** A sparse table is legal SNMP and reads honestly; publishing a
plausible zero instead would show up on an NMS as "0 errors, always" or
"0 degrees C", which is worse than absent.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..counters import CounterStore, as_counter32, as_counter64
from ..model import Switch

# Poll status enum published as fwProxyPollStatus.
POLL_OK, POLL_STALE, POLL_ERROR = 1, 2, 3


class SwitchContext:
    """Mutable holder for one switch's live state.

    The agent replaces :attr:`switch` after each poll; the OID tree's callables
    read through this object, so no tree rebuild is needed for value changes.
    """

    def __init__(
        self,
        switch: Switch,
        counters: CounterStore,
        enterprise_oid: str = "1.3.6.1.4.1.99999",
        sys_object_id: Optional[str] = None,
        sys_contact: str = "",
        sys_location: str = "",
        proxy_version: str = "0.0.0",
        stale_after: float = 300.0,
    ) -> None:
        self.switch = switch
        self.counters = counters
        self.enterprise = tuple(int(x) for x in str(enterprise_oid).split("."))
        # sysObjectID.0 defaults into our own subtree, but can be pinned to a
        # legacy value so an NMS keeps recognising a replaced proxy as the same
        # device (and keeps its historical RRDs).
        self.sys_object_id = (
            tuple(int(x) for x in str(sys_object_id).lstrip(".").split("."))
            if sys_object_id
            else self.enterprise + (2,)
        )
        self.sys_contact = sys_contact
        self.sys_location = sys_location
        self.proxy_version = proxy_version
        self.stale_after = stale_after
        # Poll bookkeeping, surfaced in the vendor subtree so an operator can
        # tell "switch is quiet" apart from "proxy stopped polling".
        #: Set from per-switch config; re-applied by the poller on every cycle.
        self.name_override: Optional[str] = switch.name_override
        self.poll_count = 0
        self.last_poll_ok: Optional[float] = None
        self.last_error: str = ""
        self.api_latency_ms: int = 0

    # -- port signature --------------------------------------------------
    def port_signature(self) -> tuple:
        """Identity of the tree's shape.

        The agent rebuilds the tree when this changes (a port gains/loses an
        SFP, or the port count changes), since those alter which OIDs exist.
        """
        return tuple(
            (p.number, p.is_sfp, p.poe_capable) for p in self.switch.ports
        )

    # -- counters --------------------------------------------------------
    def counter(self, port_number: int, field: str, stats_since: Optional[int]) -> int:
        """Monotonic value for one raw API counter field."""
        for p in self.switch.ports:
            if p.number == port_number:
                return self.counters.update(
                    self.switch.mac, port_number, field, p.counter(field), stats_since
                )
        return 0

    def c32(self, port_number: int, field: str) -> int:
        p = self._port(port_number)
        since = p.stats_since if p else None
        return as_counter32(self.counter(port_number, field, since))

    def c64(self, port_number: int, field: str) -> int:
        p = self._port(port_number)
        since = p.stats_since if p else None
        return as_counter64(self.counter(port_number, field, since))

    def c32_sum(self, port_number: int, *fields: str) -> int:
        """Sum of several monotonic counters (for the deprecated NUcast pair)."""
        p = self._port(port_number)
        since = p.stats_since if p else None
        return as_counter32(sum(self.counter(port_number, f, since) for f in fields))

    def _port(self, number: int):
        for p in self.switch.ports:
            if p.number == number:
                return p
        return None

    # -- poll health -----------------------------------------------------
    def poll_status(self) -> int:
        if self.last_poll_ok is None:
            return POLL_ERROR
        if time.time() - self.last_poll_ok > self.stale_after:
            return POLL_STALE
        return POLL_OK

    def seconds_since_poll(self) -> int:
        if self.last_poll_ok is None:
            return 0
        return int(max(0.0, time.time() - self.last_poll_ok))


def truth(value: Optional[bool]) -> int:
    """SNMPv2-TC TruthValue: true(1) / false(2)."""
    return 1 if value else 2
