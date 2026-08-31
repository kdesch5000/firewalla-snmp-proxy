"""Persisted last-known-good API payload, so startup never needs the API.

The SNMP sockets cannot be bound until the port layout is known, because the
OID tree's shape depends on how many ports exist and which carry SFPs or PoE.
That layout came only from a live ``/topology`` call, which made startup
API-dependent: during a rate-limit lockout the proxy stayed up but never
listened, and the NMS saw the device as **down** rather than as stale.

Caching the payload fixes that. On a successful poll the merged per-switch
response is written here; on a startup that cannot reach the API, agents are
rebuilt from this file instead and begin serving immediately. Counters are
admittedly frozen at their cached values -- which reads as zero traffic, the
honest answer -- while ICMP (see :mod:`~firewalla_snmp_proxy.reachability`)
supplies live evidence of whether the switch is actually alive.

Serving a *truncated* ifTable during a lockout was considered and rejected:
Observium marks ports absent from ifTable as deleted, which would discard the
port history this cache exists to protect. Either the full port set is served
or nothing is.

The payload holds only data the proxy already publishes over SNMP -- MACs, IPs,
serials, port counters, network names -- and never the API token. It is written
atomically and 0600 all the same.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

FORMAT_VERSION = 1


class TopologySnapshot:
    """Read/write the cached API payload for every proxied switch."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self._data: Dict[str, Any] = {}
        #: A write failure is almost always a static condition -- wrong owner
        #: on the state directory, read-only mount -- so it would otherwise
        #: repeat on every poll forever. Warn once, then stay quiet until it
        #: starts working again.
        self._warned = False

    # -- write -----------------------------------------------------------
    def save(
        self,
        gid: str,
        switches: Dict[str, Dict[str, Any]],
        saved_at: Optional[float] = None,
    ) -> None:
        """Persist ``{mac: {"raw":..., "settings":..., "networks":...}}``.

        Failure to write is logged and swallowed: a missing cache costs a
        degraded startup later, whereas raising here would take down a
        currently-healthy poll cycle.
        """
        if not self.path:
            return
        payload = {
            "version": FORMAT_VERSION,
            "saved_at": float(saved_at if saved_at is not None else time.time()),
            "gid": gid,
            "switches": switches,
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".topology-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, separators=(",", ":"))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)  # atomic
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._data = payload
            self._warned = False
        except (OSError, TypeError, ValueError) as exc:
            if not self._warned:
                self._warned = True
                log.warning(
                    "could not persist topology cache to %s: %s "
                    "(further failures will not be logged)", self.path, exc
                )

    # -- read ------------------------------------------------------------
    def load(self) -> Optional[Dict[str, Any]]:
        """Return the cached payload, or None if absent/unusable."""
        if not self.path or not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable topology cache %s: %s", self.path, exc)
            return None
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            log.warning(
                "ignoring topology cache %s: unsupported format version %r",
                self.path, (data or {}).get("version") if isinstance(data, dict) else None,
            )
            return None
        if not isinstance(data.get("switches"), dict) or not data["switches"]:
            log.warning("ignoring topology cache %s: no switches recorded", self.path)
            return None
        self._data = data
        return data

    def age_seconds(self) -> Optional[float]:
        saved_at = (self._data or {}).get("saved_at")
        if not saved_at:
            return None
        return max(0.0, time.time() - float(saved_at))
