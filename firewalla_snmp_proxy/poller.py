"""Poll loop: refresh every proxied switch from the MSP API.

One ``/topology`` call covers all switches on a box, so the poller fetches it
once per cycle rather than per switch. Per-switch detail (serial, uptime,
firmware) comes from ``/switches/<mac>`` because the topology node omits it.

A failed poll never clears existing data. The agents keep serving the last good
values and ``fwProxyPollStatus`` flips to stale(2)/error(3), so an NMS can
distinguish "switch is idle" from "proxy has lost the API" -- which a blanked
counter set could not express.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from .agent import SwitchAgent
from .counters import CounterStore
from .model import Switch
from .msp_api import MspClient, MspError

log = logging.getLogger(__name__)

#: Network names change rarely and cost an extra API call, so they are refreshed
#: on a slower cadence than switch statistics.
NETWORK_REFRESH_SECONDS = 900


class Poller:
    """Drives periodic refresh of a set of :class:`SwitchAgent` objects."""

    def __init__(
        self,
        client: MspClient,
        gid: str,
        agents: Dict[str, SwitchAgent],
        counters: CounterStore,
        interval: int = 60,
    ) -> None:
        self.client = client
        self.gid = gid
        self.agents = agents  # normalized MAC -> agent
        self.counters = counters
        self.interval = interval
        self._networks: Dict[str, str] = {}
        self._networks_at: float = 0.0
        self._stop = asyncio.Event()

    # -- one cycle -------------------------------------------------------
    def poll_once(self) -> bool:
        """Run a single refresh. Returns True if at least one switch updated."""
        now = time.time()

        if now - self._networks_at > NETWORK_REFRESH_SECONDS:
            names = self.client.network_names(self.gid)
            if names:
                self._networks = names
                self._networks_at = now

        try:
            nodes = self.client.find_switches(self.gid)
        except MspError as exc:
            self._mark_all_failed(str(exc))
            log.warning("topology poll failed: %s", exc)
            return False

        settings = self.client.switch_settings(self.gid)
        by_mac = {
            str(n.get("id") or n.get("mac") or "").upper(): n for n in nodes
        }

        updated = 0
        for mac, agent in self.agents.items():
            node = by_mac.get(mac)
            if node is None:
                agent.ctx.last_error = "switch %s not present in MSP topology" % mac
                log.warning("%s is not in the topology response", mac)
                continue

            # Merge topology node with per-switch detail. Detail is authoritative
            # where they overlap, and is the only source of uptime and serial.
            merged = dict(node)
            try:
                detail = self.client.switch_detail(self.gid, mac)
                if detail:
                    merged.update(detail)
            except MspError as exc:
                # Detail is optional; topology alone still yields all port
                # counters, so degrade rather than fail the whole switch.
                log.warning("detail fetch failed for %s: %s", mac, exc)
                agent.ctx.last_error = "detail: %s" % exc

            agent.ctx.switch = Switch(
                raw=merged,
                settings=settings,
                networks=self._networks,
                polled_at=time.time(),
                # Must be re-applied here: the Switch is rebuilt from scratch on
                # every poll, so an override applied only at startup would be
                # silently discarded by the first poll cycle.
                name_override=agent.ctx.name_override,
            )
            agent.ctx.poll_count += 1
            agent.ctx.last_poll_ok = time.time()
            agent.ctx.api_latency_ms = int((self.client.last_latency or 0.0) * 1000)
            if not agent.ctx.last_error.startswith("detail:"):
                agent.ctx.last_error = ""
            agent.refresh()
            updated += 1

        self.counters.save()
        return updated > 0

    def _mark_all_failed(self, message: str) -> None:
        for agent in self.agents.values():
            agent.ctx.last_error = message

    # -- loop ------------------------------------------------------------
    async def run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                # The MSP client is blocking stdlib HTTP; run it off the event
                # loop so SNMP requests keep being answered during a slow or
                # timing-out API call.
                await asyncio.get_running_loop().run_in_executor(None, self.poll_once)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("unexpected poll error: %s", exc)
                self._mark_all_failed(str(exc))
            elapsed = time.monotonic() - started
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(1.0, self.interval - elapsed)
                )
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
        self.counters.save(force=True)
