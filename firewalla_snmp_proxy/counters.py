"""Monotonic counter store.

SNMP counters must only ever increase (wrapping at 2^32 / 2^64 is fine and
every NMS handles it). Firewalla's port counters, however, can be *reset to
zero* -- by a firmware event, or by someone hitting the port ``reset-stats``
endpoint in the MSP UI. A reset looks like a large negative delta, which an NMS
renders as a garbage spike or a gap.

Two independent reset signals are used, because either alone is insufficient:

* **``statsSinceTs`` advancing** -- the authoritative signal. The API tells us
  outright that counters were zeroed. Catches the case where traffic since the
  reset has already pushed the raw value back above where it was, which a
  value comparison would miss entirely.
* **The raw value decreasing** -- the fallback, for firmware that zeroes
  counters without moving ``statsSinceTs``.

On either signal the last raw value is folded into a persistent offset, so the
value published over SNMP keeps rising across resets, restarts and reboots.

``ifCounterDiscontinuityTime`` is *also* published from ``statsSinceTs`` (see
:mod:`~firewalla_snmp_proxy.mibs.ifmib`). A well-behaved NMS reads it and
discards the suspect delta by itself; most do not, hence the offsets. Doing
both is belt-and-braces on purpose.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF


class CounterStore:
    """Persistent per-counter monotonic offsets.

    State is keyed ``"<switch-mac>|<port>|<counter>"`` and written atomically,
    so a crash or power loss mid-write cannot corrupt it.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self._state: Dict[str, Dict[str, int]] = {}
        self._dirty = False
        if path:
            self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._state = {
                    str(k): {str(a): int(b) for a, b in v.items()}
                    for k, v in data.get("counters", {}).items()
                    if isinstance(v, dict)
                }
            log.debug("loaded %d counter states from %s", len(self._state), self.path)
        except (OSError, ValueError) as exc:
            # A corrupt state file must not stop the agent; worst case is one
            # spurious delta as offsets rebuild.
            log.warning("ignoring unreadable counter state %s: %s", self.path, exc)
            self._state = {}

    def save(self, force: bool = False) -> None:
        if not self.path or (not self._dirty and not force):
            return
        payload: Dict[str, Any] = {"version": 1, "counters": self._state}
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".counters-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, separators=(",", ":"))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)  # atomic
            except BaseException:
                for leftover in (tmp,):
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass
                raise
            self._dirty = False
        except OSError as exc:
            log.warning("could not persist counter state to %s: %s", self.path, exc)

    # -- core ------------------------------------------------------------
    @staticmethod
    def key(mac: str, port: int, name: str) -> str:
        return "%s|%s|%s" % (mac, port, name)

    def update(
        self,
        mac: str,
        port: int,
        name: str,
        raw: int,
        stats_since: Optional[int] = None,
    ) -> int:
        """Feed a raw counter reading; return the monotonic value to publish."""
        raw = max(0, int(raw))
        k = self.key(mac, port, name)
        entry = self._state.get(k)

        if entry is None:
            self._state[k] = {
                "offset": 0,
                "last_raw": raw,
                "stats_since": int(stats_since or 0),
            }
            self._dirty = True
            return raw

        offset = entry.get("offset", 0)
        last_raw = entry.get("last_raw", 0)
        prev_since = entry.get("stats_since", 0)
        since = int(stats_since or 0)

        reset_by_ts = bool(since and prev_since and since > prev_since)
        reset_by_value = raw < last_raw

        if reset_by_ts or reset_by_value:
            offset += last_raw
            log.info(
                "counter reset on %s port %s %s (%s): folding %d into offset",
                mac, port, name,
                "statsSinceTs advanced" if reset_by_ts else "value decreased",
                last_raw,
            )
            self._dirty = True

        if raw != last_raw or since != prev_since or reset_by_ts or reset_by_value:
            self._dirty = True

        entry["offset"] = offset
        entry["last_raw"] = raw
        entry["stats_since"] = since or prev_since
        return offset + raw

    def forget(self, mac: str) -> None:
        """Drop all state for a switch (used when it leaves the topology)."""
        prefix = "%s|" % mac
        gone = [k for k in self._state if k.startswith(prefix)]
        for k in gone:
            del self._state[k]
        if gone:
            self._dirty = True


def as_counter32(value: int) -> int:
    """Fold a monotonic value into the Counter32 range.

    Deriving the 32-bit counters from the same monotonic value as the 64-bit
    ones means 32-bit wrap needs no separate detection: it emerges naturally,
    exactly as a real agent's would.
    """
    return int(value) & MASK32


def as_counter64(value: int) -> int:
    return int(value) & MASK64
