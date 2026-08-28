"""Read-only client for the Firewalla MSP cloud API.

Only stdlib HTTP is used, so the sole runtime dependency of this project is
pysnmp. That keeps ``pipx install`` fast and avoids dragging in a TLS stack
that differs from the system Python's.

Two defensive behaviours are deliberate and non-obvious:

1. **Unknown paths return HTTP 200 with the MSP single-page app's index.html**,
   not a 404. Status code alone therefore cannot tell you whether an endpoint
   exists. Every response is checked for a JSON content type and rejected
   otherwise, which turns a silent "empty data" bug into a clear error.

2. **Mutating endpoints are refused client-side.** The MSP API exposes port
   ``reset-stats``, ``power-cycle`` and ``restart``, plus switch ``reboot``,
   ``upgrade`` and ``DELETE``. A monitoring tool must never issue those --
   ``reset-stats`` would destroy counter history and the others drop traffic.
   This client can only ever issue GETs, and additionally refuses known-unsafe
   path segments so a future bug or a bad config cannot reach them.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: Path segments that mutate switch state. Refused regardless of method.
FORBIDDEN_SEGMENTS = frozenset(
    {
        "reset-stats",
        "power-cycle",
        "restart",
        "reboot",
        "upgrade",
        "switch-branch",
        "check-status",
        "force-sync",
        "detach",
        "attach",
    }
)

DEFAULT_TIMEOUT = 20.0
USER_AGENT = "firewalla-snmp-proxy/2.1 (+https://github.com/kdesch5000/firewalla-snmp-proxy)"


class MspError(RuntimeError):
    """Any failure talking to the MSP API."""


class MspAuthError(MspError):
    """Token rejected (HTTP 401/403)."""


class MspNotJson(MspError):
    """Endpoint returned non-JSON -- almost always the SPA index.html fallback,
    meaning the path does not exist on this MSP tenant."""


class MspRateLimited(MspError):
    """Quota exhausted (HTTP 429), or the API is shedding load (HTTP 503).

    Distinct from a generic :class:`MspError` because the correct response is
    different in kind: not "retry on the usual schedule and log a warning" but
    "stop calling for a while". Retrying a 429 at the normal interval keeps the
    quota pinned and can extend the lockout, so the poller backs off on this.

    :attr:`retry_after` carries the server's own guidance when it sends a
    ``Retry-After`` header, which is always preferable to a guess.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header into seconds from now.

    RFC 9110 allows either delta-seconds or an HTTP-date, and real APIs send
    both, so both are handled. Anything unparseable returns None so the caller
    falls back to its own backoff schedule rather than trusting a bad value.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # An HTTP-date is by definition GMT; treat a naive result as such
        # rather than as local time, which would skew by the UTC offset.
        when = when.replace(tzinfo=datetime.timezone.utc)
    delta = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return max(0.0, delta)


class MspClient:
    """Minimal, read-only MSP API client."""

    def __init__(
        self,
        domain: str,
        token: str,
        timeout: float = DEFAULT_TIMEOUT,
        verify_tls: bool = True,
    ) -> None:
        if not domain:
            raise ValueError("MSP domain is required (e.g. dn-abc123.firewalla.net)")
        if not token:
            raise ValueError("MSP API token is required")
        # Accept a bare hostname or a full URL and normalize to a base URL.
        if "://" in domain:
            parsed = urllib.parse.urlparse(domain)
            domain = parsed.netloc or parsed.path
        self.domain = domain.strip("/")
        self._token = token.strip()
        self.timeout = timeout
        self._ctx: Optional[ssl.SSLContext] = None
        if not verify_tls:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        self.last_latency: Optional[float] = None

    # -- plumbing --------------------------------------------------------
    def _url(self, path: str, params: Optional[Dict[str, str]] = None) -> str:
        path = path.lstrip("/")
        url = "https://%s/%s" % (self.domain, path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def get(self, path: str, params: Optional[Dict[str, str]] = None) -> Any:
        """GET ``path`` and return decoded JSON.

        Raises :class:`MspNotJson` if the response is not JSON, which is how a
        nonexistent endpoint presents itself on this API.
        """
        segments = {s for s in path.split("?")[0].split("/") if s}
        unsafe = segments & FORBIDDEN_SEGMENTS
        if unsafe:
            raise MspError(
                "refusing to request %r: contains state-changing segment(s) %s; "
                "this client is read-only by design" % (path, sorted(unsafe))
            )

        url = self._url(path, params)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "Token %s" % self._token)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)

        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MspAuthError(
                    "MSP API rejected the token (HTTP %d). Regenerate it in the "
                    "Firewalla MSP web UI under account settings." % exc.code
                ) from exc
            if exc.code in (429, 503):
                retry_after = parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
                raise MspRateLimited(
                    "HTTP %d from %s" % (exc.code, url), retry_after
                ) from exc
            raise MspError("HTTP %d from %s" % (exc.code, url)) from exc
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            raise MspError("cannot reach %s: %s" % (url, exc)) from exc
        finally:
            self.last_latency = time.monotonic() - started

        if "json" not in ctype:
            raise MspNotJson(
                "%s returned Content-Type %r instead of JSON. On this API an "
                "unknown path returns the web app's HTML with HTTP 200, so this "
                "almost certainly means the endpoint does not exist for your "
                "tenant (or your MSP domain is wrong)." % (url, ctype or "unset")
            )
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MspNotJson("%s returned undecodable JSON: %s" % (url, exc)) from exc

    # -- endpoints -------------------------------------------------------
    def boxes(self) -> List[Dict[str, Any]]:
        """All boxes on the account. There is no single-box GET on this API."""
        data = self.get("v2/boxes")
        return data if isinstance(data, list) else []

    def topology(self, gid: str) -> Dict[str, Any]:
        """``/v2/boxes/<gid>/topology`` -- boxes and switches, ports included.

        This single call carries every per-port field the agent needs, which is
        why it is the poll loop's primary request.
        """
        data = self.get("v2/boxes/%s/topology" % gid)
        return data if isinstance(data, dict) else {"nodes": []}

    def switch_detail(self, gid: str, mac: str) -> Dict[str, Any]:
        """``/v2/boxes/<gid>/switches/<mac>`` -- adds serial, uptime, firmware."""
        data = self.get("v2/boxes/%s/switches/%s" % (gid, mac))
        return data if isinstance(data, dict) else {}

    def switch_settings(self, gid: str) -> Dict[str, Any]:
        """``/v2/boxes/<gid>/switch-settings`` -- flow control and STP mode."""
        try:
            data = self.get("v2/boxes/%s/switch-settings" % gid)
        except MspNotJson:
            # Optional endpoint; absence must not fail a poll.
            return {}
        return data if isinstance(data, dict) else {}

    def devices(self, gid: str) -> List[Dict[str, Any]]:
        """Host records. Used only to resolve network UUID -> friendly name."""
        data = self.get("v2/devices", {"box": gid})
        return data if isinstance(data, list) else []

    def network_names(self, gid: str) -> Dict[str, str]:
        """Map network/VLAN UUID -> name, joined to ports via ``settings.intf``.

        The topology endpoint gives ports a network UUID but no name, and there
        is no networks endpoint on v2, so names are harvested from the device
        list where each host carries ``network: {id, name}``.
        """
        out: Dict[str, str] = {}
        try:
            for dev in self.devices(gid):
                net = dev.get("network") or {}
                if net.get("id") and net.get("name"):
                    out[str(net["id"])] = str(net["name"])
        except MspRateLimited:
            # Must not be swallowed: the poller needs to see this to back off,
            # and network names are the least important thing in a cycle.
            raise
        except MspError as exc:
            log.warning("could not resolve network names: %s", exc)
        return out

    def find_switches(self, gid: str) -> List[Dict[str, Any]]:
        """Switch nodes from the topology response.

        Selects on the topology node's ``type == "switch"``. The *device*
        record's ``deviceType`` is deliberately not used: it has churned three
        times in four weeks (``firewalla`` -> ``switch`` -> ``fwsw-B``) and is
        not a dependable key.
        """
        nodes = self.topology(gid).get("nodes") or []
        return [n for n in nodes if str(n.get("type", "")).lower() == "switch"]
