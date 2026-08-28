"""ICMP reachability for the real switch, independent of the MSP API.

The proxy's SNMP identity lives on loopback, so an NMS polling it learns
nothing about whether the *switch* is alive -- that came entirely from the API's
``online`` field. When the API is unavailable (rate limited, network trouble)
that field goes stale, and there is no other evidence either way.

Pinging the switch directly supplies that evidence at no API cost, which is
what makes serving cached data safe: the counters are admittedly old, but
``fwSwitchOnline`` and ``fwProxyIcmpStatus`` still reflect reality, so a
stale-but-alive switch is distinguishable from one that has actually gone away.

**Reachability is tri-state, and the third state matters.** A check that could
not be *carried out* (unresolvable name, no ICMP permission, resolver not up
yet after a reboot) is evidence of nothing, and is deliberately not folded into
"down": doing so would report a fake outage every time the host rebooted before
its resolver was ready. Unknown leaves the debounce counters untouched, so a
persistent inability to check surfaces as staleness -- which is what it is --
rather than as an outage.

Two mechanisms, tried in order:

1. **An unprivileged ICMP datagram socket** (``SOCK_DGRAM``/``IPPROTO_ICMP``),
   available to any process whose GID falls in ``net.ipv4.ping_group_range``.
   Preferred because it needs no subprocess, no ``PATH`` lookup and no parsing
   of locale-dependent output, and uses only ``AF_INET`` -- which matters under
   a hardened unit with ``RestrictAddressFamilies``.
2. **The system ``ping`` binary**, for hosts where that sysctl is restricted.

Verified working inside this project's systemd sandbox (``NoNewPrivileges``,
``PrivateDevices``, ``CapabilityBoundingSet=``, ``RestrictAddressFamilies=
AF_INET AF_INET6``) via both paths.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import subprocess
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

#: Tri-state reachability. None is "could not check", not "down".
UP, DOWN, UNKNOWN = True, False, None

#: SNMP enum published as fwProxyIcmpStatus.
ICMP_UP, ICMP_DOWN, ICMP_UNKNOWN, ICMP_DISABLED = 1, 2, 3, 4

ICMP_ECHO_REQUEST = 8
DEFAULT_TIMEOUT = 2.0

#: `ping` exits 2 for "other error", which in practice is almost always an
#: unresolvable hostname -- a different statement from exit 1, which means the
#: host was addressed and did not answer.
PING_EXIT_OTHER_ERROR = 2
_RTT_RE = re.compile(r"time[=<]([0-9.]+)\s*ms")


class Unattemptable(Exception):
    """The check could not be performed at all (maps to UNKNOWN)."""


def _icmp_socket_ping(host: str, timeout: float) -> float:
    """One echo via an unprivileged ICMP datagram socket. Returns RTT in ms.

    Raises :class:`Unattemptable` when the mechanism itself is unavailable, and
    :class:`TimeoutError` / :class:`OSError` when the host did not answer.
    """
    try:
        addr = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
    except socket.gaierror as exc:
        raise Unattemptable("cannot resolve %s: %s" % (host, exc)) from exc
    if not addr:
        raise Unattemptable("no A record for %s" % host)
    ip = addr[0][4][0]

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except (PermissionError, OSError) as exc:
        # net.ipv4.ping_group_range excludes this GID, or the kernel lacks
        # unprivileged ICMP. Caller falls back to the ping binary.
        raise Unattemptable("no unprivileged ICMP socket: %s" % exc) from exc

    with sock:
        sock.settimeout(timeout)
        # For SOCK_DGRAM ICMP the kernel assigns the identifier and computes
        # the checksum, and demultiplexes replies by that identifier -- so a
        # reply arriving on this socket is necessarily ours.
        packet = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, 0, 1) + b"fwproxy"
        started = time.monotonic()
        sock.sendto(packet, (ip, 0))
        sock.recvfrom(1024)
        return (time.monotonic() - started) * 1000.0


def _cli_ping(host: str, timeout: float) -> Optional[float]:
    """One echo via the system ``ping``. Returns RTT in ms, or None if no reply."""
    seconds = max(1, int(round(timeout)))
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(seconds), host],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=seconds + 2,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise Unattemptable("ping binary unusable: %s" % exc) from exc
    if result.returncode == PING_EXIT_OTHER_ERROR:
        raise Unattemptable("ping could not address %s" % host)
    if result.returncode != 0:
        return None
    match = _RTT_RE.search(result.stdout)
    return float(match.group(1)) if match else 0.0


def ping_once(
    host: str, timeout: float = DEFAULT_TIMEOUT
) -> Tuple[Optional[bool], Optional[float]]:
    """Single reachability check. Returns ``(state, rtt_ms)``.

    ``state`` is :data:`UP`, :data:`DOWN`, or :data:`UNKNOWN`.
    """
    if not host:
        return UNKNOWN, None
    try:
        return UP, _icmp_socket_ping(host, timeout)
    except Unattemptable as exc:
        log.debug("native ICMP unavailable (%s); falling back to ping binary", exc)
    except (TimeoutError, socket.timeout):
        return DOWN, None
    except OSError as exc:
        # Addressed but the network refused it. Not distinguishable from a
        # transient local problem, so treat as unknown rather than creeping
        # toward a down verdict on ambiguous evidence.
        log.debug("ICMP to %s failed: %s", host, exc)
        return UNKNOWN, None

    try:
        rtt = _cli_ping(host, timeout)
    except Unattemptable as exc:
        log.debug("ping to %s unattemptable: %s", host, exc)
        return UNKNOWN, None
    return (UP, rtt) if rtt is not None else (DOWN, None)


class ReachabilityMonitor:
    """Debounced tri-state reachability for one host.

    A single dropped packet must not flip the switch's reported state, so a
    confirmed transition needs consecutive agreeing checks. Recovery is
    deliberately faster than failure: coming back is unambiguous, going away
    is not.
    """

    def __init__(
        self,
        host: str,
        timeout: float = DEFAULT_TIMEOUT,
        fail_threshold: int = 3,
        recover_threshold: int = 1,
    ) -> None:
        self.host = host
        self.timeout = timeout
        self.fail_threshold = max(1, fail_threshold)
        self.recover_threshold = max(1, recover_threshold)
        #: Confirmed state; None until enough evidence accumulates.
        self.state: Optional[bool] = UNKNOWN
        self.rtt_ms: Optional[float] = None
        self.last_checked: Optional[float] = None
        self.last_reply: Optional[float] = None
        self._fails = 0
        self._oks = 0

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def snmp_status(self) -> int:
        if not self.enabled:
            return ICMP_DISABLED
        if self.state is UP:
            return ICMP_UP
        if self.state is DOWN:
            return ICMP_DOWN
        return ICMP_UNKNOWN

    def feed(self, observed: Optional[bool], rtt_ms: Optional[float] = None) -> None:
        """Apply one observation to the debounce counters."""
        if observed is UNKNOWN:
            # Evidence of nothing: leave the confirmed state and both counters
            # exactly as they were.
            return

        self.last_checked = time.time()
        if observed is UP:
            self.last_reply = self.last_checked
            self.rtt_ms = rtt_ms
            self._fails = 0
            self._oks += 1
            if self._oks >= self.recover_threshold:
                self.state = UP
        else:
            self._oks = 0
            self._fails += 1
            self.rtt_ms = None
            if self._fails >= self.fail_threshold:
                self.state = DOWN

    def check(self) -> Optional[bool]:
        """Perform one check and return the confirmed state afterwards."""
        if not self.enabled:
            return UNKNOWN
        observed, rtt = ping_once(self.host, self.timeout)
        if observed is UNKNOWN:
            # Still record that we tried, so staleness is visible.
            self.last_checked = time.time()
        self.feed(observed, rtt)
        return self.state
