"""Counter ramping: present a slow poll cadence at the NMS's faster cadence.

The MSP API is the only source of port counters, and polling it hard gets the
token rate-limited (HTTP 429), so the poll interval has to be minutes, not
seconds. But an NMS polls on *its* own schedule -- Observium's poller is a
fixed 5-minute cron -- and the two cadences do not divide evenly.

Served raw, a 15-minute refresh against a 5-minute poller produces a sawtooth:
two polls see byte-identical counters and compute a rate of zero, the third
sees 900s of delta and divides it by 300s, overstating the rate by 3x. Every
individual sample is wrong; only after RRA consolidation does the average come
back to the truth.

This module fixes that by spreading each newly-learned increment evenly across
the window *following* the poll that revealed it. Given counter values C1 at
T1 and C2 at T2, a request at time t is served::

    C1 + (C2 - C1) * (t - T2) / (T2 - T1)      clamped to [C1, C2]

So the increment learned at T2 is paid out over the next (T2 - T1) seconds.
Consequences, all deliberate:

* **Totals are exact.** Nothing is invented or discarded; the same bytes are
  reported, just distributed across the window instead of dumped in one sample.
* **The window is measured, not configured.** ``T2 - T1`` is observed, so if a
  rate-limit backoff stretches the real interval to 40 minutes, the ramp
  stretches with it automatically. No coupling to ``poll_interval``.
* **Output is monotonically non-decreasing**, which SNMP counters must be. At a
  window boundary the new ``prev`` is the old ``cur``, which is >= everything
  served during the old window, so the seam can only step up, never back.
* **It lags by one window.** The graph is up to one poll interval behind. This
  is the price of not knowing the future: at time T2 we know how many bytes
  arrived since T1, but nothing about what is arriving now.
* **Sub-window bursts are flattened.** A 30-second 900 Mbps burst inside a
  15-minute window reads as ~30 Mbps sustained. The MSP API genuinely does not
  carry the shape of traffic within a window, so the alternative is not better
  resolution -- it is the sawtooth above, which is a worse lie.

Idle ports and a frozen upstream both plateau at the true counter value, so a
stalled API reads as zero traffic rather than as invented traffic.

State is in memory only. A restart is already a discontinuity in the NMS's eyes
and the first post-restart sample serves the raw value, so persisting it would
add a failure mode without removing one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

log = logging.getLogger(__name__)

#: When the observed window exceeds this multiple of the configured poll
#: interval, stop ramping and serve the raw value. Smearing a multi-hour
#: outage's worth of bytes across an equally long ramp would hide the recovery.
DEFAULT_MAX_WINDOW_FACTOR = 2.5


@dataclass
class _Segment:
    """One counter's two most recent observations."""

    prev: int
    cur: int
    prev_at: float
    cur_at: float


class CounterRamp:
    """Interpolates monotonic counters between upstream refreshes.

    Keyed identically to :class:`~firewalla_snmp_proxy.counters.CounterStore`,
    and fed the *monotonic* (post-offset) value, so reset handling has already
    happened by the time a value arrives here.
    """

    def __init__(self, max_window: float) -> None:
        #: Observed windows longer than this disable ramping for that sample.
        self.max_window = float(max_window)
        self._state: Dict[str, _Segment] = {}

    def value(self, key: str, current: int, now: Optional[float] = None) -> int:
        """Interpolated value for ``key`` given the latest monotonic reading.

        A change in ``current`` is what advances the window -- not the poll
        loop firing. That means a poll which returns identical counters (an
        idle port, or an upstream that has stopped updating) correctly plateaus
        instead of ramping toward a value that never arrived.
        """
        current = int(current)
        now = time.time() if now is None else now
        seg = self._state.get(key)

        if seg is None:
            self._state[key] = _Segment(current, current, now, now)
            return current

        if current != seg.cur:
            if current < seg.cur:
                # CounterStore folds resets into an offset, so a decrease here
                # should be unreachable. Resync hard rather than ever serve a
                # decreasing counter, which would read as a wrap to the NMS.
                log.warning(
                    "ramp %s saw monotonic value decrease (%d -> %d); resyncing",
                    key, seg.cur, current,
                )
                seg.prev = seg.cur = current
                seg.prev_at = seg.cur_at = now
                return current
            seg.prev, seg.prev_at = seg.cur, seg.cur_at
            seg.cur, seg.cur_at = current, now

        window = seg.cur_at - seg.prev_at
        if window <= 0 or window > self.max_window:
            return seg.cur

        frac = (now - seg.cur_at) / window
        if frac >= 1.0:
            return seg.cur
        if frac <= 0.0:
            return seg.prev
        # Scale the delta, never the absolute value: deltas are small enough
        # that float multiplication is exact, whereas a 64-bit octet count
        # would lose precision past 2^53.
        return seg.prev + int((seg.cur - seg.prev) * frac)

    def forget(self, mac: str) -> None:
        """Drop state for a switch that has left the topology."""
        prefix = "%s|" % mac
        for k in [k for k in self._state if k.startswith(prefix)]:
            del self._state[k]
