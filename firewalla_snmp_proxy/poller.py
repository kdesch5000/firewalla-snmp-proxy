"""Poll loop: refresh every proxied switch from the MSP API.

One ``/topology`` call covers all switches on a box, so the poller fetches it
once per cycle rather than per switch. Per-switch detail (serial, uptime,
firmware) comes from ``/switches/<mac>`` because the topology node omits it.

A failed poll never clears existing data. The agents keep serving the last good
values and ``fwProxyPollStatus`` flips to stale(2)/error(3), so an NMS can
distinguish "switch is idle" from "proxy has lost the API" -- which a blanked
counter set could not express.

**Rate limiting is handled as a first-class state, not as an error.** The MSP
API enforces a quota; once exceeded it returns HTTP 429 to every request.
Retrying on the normal interval then keeps the quota pinned indefinitely and
can extend the lockout, so a 429 puts the poller into exponential backoff and
it stops issuing requests entirely until the backoff expires. This matters
because the failure is silent from the NMS's point of view: counters simply
stop advancing, and every derived rate reads as a legitimate zero.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Dict, Optional

from .agent import SwitchAgent
from .counters import CounterStore
from .model import Switch
from .msp_api import MspClient, MspError, MspRateLimited

log = logging.getLogger(__name__)

#: Network names change rarely and cost an extra API call, so they are refreshed
#: on a slower cadence than switch statistics.
NETWORK_REFRESH_SECONDS = 900

#: Ceiling on rate-limit backoff. An hour is long enough to let a quota window
#: roll over, and short enough that recovery does not need an operator.
DEFAULT_MAX_BACKOFF = 3600.0

#: Backoff is multiplied by a random factor in this range. Without jitter,
#: several proxies sharing one MSP token would retry in lockstep and re-trip
#: the quota together the moment it resets.
JITTER = (0.8, 1.2)


def rate_limit_delay(
    exc: MspRateLimited,
    interval: float,
    strikes: int,
    max_backoff: float,
) -> float:
    """Seconds to wait after a rate-limit response.

    Shared by the poll loop and by startup, which faces the same problem: the
    service hits the API before the poller exists, so without this a 429 at
    startup would exit non-zero and let systemd's Restart= crash-loop against
    the rate-limited API.

    The server's ``Retry-After`` wins when present -- it is the only party that
    knows when the quota actually resets. Otherwise back off exponentially from
    the poll interval, jittered, capped.
    """
    if exc.retry_after is not None:
        return max(1.0, min(max_backoff, float(exc.retry_after)))
    delay = float(interval) * (2 ** max(0, strikes - 1))
    delay *= random.uniform(*JITTER)
    return max(1.0, min(max_backoff, delay))


class Poller:
    """Drives periodic refresh of a set of :class:`SwitchAgent` objects."""

    def __init__(
        self,
        client: MspClient,
        gid: str,
        agents: Dict[str, SwitchAgent],
        counters: CounterStore,
        interval: int = 60,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
    ) -> None:
        self.client = client
        self.gid = gid
        self.agents = agents  # normalized MAC -> agent
        self.counters = counters
        self.interval = interval
        self.max_backoff = float(max_backoff)
        self._networks: Dict[str, str] = {}
        self._networks_at: float = 0.0
        self._stop = asyncio.Event()
        #: Consecutive rate-limit responses; drives the exponential schedule.
        self._strikes = 0
        #: monotonic() deadline before which no request may be issued.
        self._backoff_until: float = 0.0

    # -- one cycle -------------------------------------------------------
    def poll_once(self) -> bool:
        """Run a single refresh. Returns True if at least one switch updated.

        :class:`MspRateLimited` is allowed to propagate from anywhere in here
        -- including the otherwise-optional network-name and per-switch-detail
        calls -- because the caller must see it to enter backoff. Every other
        MspError is still degraded locally so one bad sub-request cannot cost a
        whole cycle.
        """
        now = time.time()

        if now - self._networks_at > NETWORK_REFRESH_SECONDS:
            names = self.client.network_names(self.gid)
            if names:
                self._networks = names
                self._networks_at = now

        try:
            nodes = self.client.find_switches(self.gid)
        except MspRateLimited:
            raise
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
            except MspRateLimited:
                raise
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

    # -- rate-limit backoff ----------------------------------------------
    def backoff_delay(self, exc: MspRateLimited) -> float:
        """Seconds to wait after a rate-limit response."""
        return rate_limit_delay(
            exc, self.interval, self._strikes, self.max_backoff
        )

    def _enter_backoff(self, exc: MspRateLimited) -> float:
        self._strikes += 1
        delay = self.backoff_delay(exc)
        self._backoff_until = time.monotonic() + delay
        source = "Retry-After" if exc.retry_after is not None else "strike %d" % self._strikes
        message = "MSP API rate limited (%s); not polling for %ds, resume ~%s" % (
            source, int(delay), time.strftime("%H:%M:%S", time.localtime(time.time() + delay)),
        )
        log.warning("%s -- %s", exc, message)
        self._mark_all_failed(message)
        return delay

    @property
    def in_backoff(self) -> bool:
        return time.monotonic() < self._backoff_until

    # -- loop ------------------------------------------------------------
    async def run(self) -> None:
        while not self._stop.is_set():
            if self.in_backoff:
                # Skip the cycle outright rather than calling and eating another
                # 429; the whole point of backing off is to stop the requests.
                wait = max(1.0, self._backoff_until - time.monotonic())
            else:
                started = time.monotonic()
                try:
                    # The MSP client is blocking stdlib HTTP; run it off the
                    # event loop so SNMP requests keep being answered during a
                    # slow or timing-out API call.
                    await asyncio.get_running_loop().run_in_executor(
                        None, self.poll_once
                    )
                    if self._strikes:
                        log.info(
                            "MSP API recovered after %d rate-limited cycle(s)",
                            self._strikes,
                        )
                    self._strikes = 0
                except MspRateLimited as exc:
                    wait = self._enter_backoff(exc)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("unexpected poll error: %s", exc)
                    self._mark_all_failed(str(exc))
                elapsed = time.monotonic() - started
                wait = max(1.0, self.interval - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
        self.counters.save(force=True)
