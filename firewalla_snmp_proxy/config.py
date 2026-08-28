"""Configuration loading and validation.

The token is the one genuinely sensitive value here, so it is resolvable from
three places in descending precedence: the ``FIREWALLA_MSP_TOKEN`` environment
variable, a separate token file, then the config file itself. It is
deliberately **not** accepted as a command-line argument -- that would leak it
into shell history and into any other user's ``ps`` output.

When the token does live in the config file, permissions are checked and a
warning is emitted if it is group- or world-readable.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

TOKEN_ENV = "FIREWALLA_MSP_TOKEN"

DEFAULT_BASE_PORT = 16100
#: 15 minutes. Each poll cycle costs several MSP API calls and the API
#: enforces a quota, so the default is set by what the quota tolerates
#: rather than by how fresh we would like the data to be. Counter ramping
#: (see firewalla_snmp_proxy.ramp) is what makes an interval this long
#: still produce usable rates at a 5-minute NMS.
DEFAULT_POLL_INTERVAL = 900
DEFAULT_COMMUNITY = "public"
#: Placeholder enterprise OID. IANA is currently issuing PENs around 66649, so
#: 99999 is unassigned and will remain so for the foreseeable future -- there is
#: no realistic collision risk. Change it here if you register your own.
DEFAULT_ENTERPRISE_OID = "1.3.6.1.4.1.99999"
DEFAULT_STATE_FILE = "/var/lib/firewalla-snmp-proxy/counters.json"

#: Below this, MSP API rate limits and pointless load become a concern; the API
#: itself only refreshes switch stats on the order of tens of seconds.
MIN_POLL_INTERVAL = 15

#: Ceiling on rate-limit backoff, in seconds.
DEFAULT_MAX_BACKOFF = 3600

#: Counter presentation. "ramp" interpolates between upstream refreshes so a
#: slow poll interval still produces sane rates at a faster-polling NMS; "raw"
#: publishes the last value fetched, which is correct but sawtooths when the
#: NMS polls faster than this proxy does. See firewalla_snmp_proxy.ramp.
SMOOTHING_MODES = ("ramp", "raw")
DEFAULT_SMOOTHING = "ramp"

#: Multiple of poll_interval past which ramping is abandoned for a sample.
DEFAULT_MAX_RAMP_FACTOR = 2.5


class ConfigError(RuntimeError):
    """Configuration is missing or invalid."""


@dataclass
class SwitchConfig:
    """One proxied switch."""

    mac: str
    port: int
    name: Optional[str] = None
    community: Optional[str] = None

    @property
    def normalized_mac(self) -> str:
        return self.mac.upper().replace("-", ":")


@dataclass
class Config:
    domain: str = ""
    token: str = ""
    poll_interval: int = DEFAULT_POLL_INTERVAL
    listen_address: str = "0.0.0.0"
    base_port: int = DEFAULT_BASE_PORT
    community: str = DEFAULT_COMMUNITY
    enterprise_oid: str = DEFAULT_ENTERPRISE_OID
    # Override for sysObjectID.0. Defaults to <enterprise_oid>.2.
    #
    # Exists for migrations: monitoring systems key their device/OS detection
    # on sysObjectID, so when replacing an existing proxy you can keep the old
    # value and the NMS treats it as the same device -- preserving its OS
    # definition, icon and historical port RRDs.
    sys_object_id: Optional[str] = None
    sys_contact: str = ""
    sys_location: str = ""
    state_file: str = DEFAULT_STATE_FILE
    counter_smoothing: str = DEFAULT_SMOOTHING
    # 0 means "derive from poll_interval" (DEFAULT_MAX_RAMP_FACTOR x interval).
    max_ramp_seconds: int = 0
    max_backoff_seconds: int = DEFAULT_MAX_BACKOFF
    box_gid: Optional[str] = None
    switches: List[SwitchConfig] = field(default_factory=list)
    verify_tls: bool = True
    source_path: Optional[str] = None

    # -- validation ------------------------------------------------------
    def validate(self) -> None:
        if not self.domain:
            raise ConfigError(
                "msp.domain is required, e.g. dn-abc123.firewalla.net "
                "(find it in the URL of your Firewalla MSP web console)"
            )
        if not self.token:
            raise ConfigError(
                "no MSP API token found. Set %s in the environment, or "
                "msp.token_file, or msp.token in the config file." % TOKEN_ENV
            )
        if self.poll_interval < MIN_POLL_INTERVAL:
            raise ConfigError(
                "poll_interval of %ds is too aggressive; minimum is %ds. The "
                "MSP API refreshes switch statistics on the order of tens of "
                "seconds, so polling faster gains nothing and risks rate limits."
                % (self.poll_interval, MIN_POLL_INTERVAL)
            )
        if self.counter_smoothing not in SMOOTHING_MODES:
            raise ConfigError(
                "counter_smoothing must be one of %s, got %r"
                % (", ".join(SMOOTHING_MODES), self.counter_smoothing)
            )
        if self.max_ramp_seconds < 0:
            raise ConfigError("max_ramp_seconds cannot be negative")
        if self.max_backoff_seconds < 1:
            raise ConfigError("max_backoff_seconds must be at least 1")
        try:
            parts = [int(x) for x in str(self.enterprise_oid).split(".")]
        except ValueError as exc:
            raise ConfigError(
                "enterprise_oid must be a dotted numeric OID, got %r"
                % (self.enterprise_oid,)
            ) from exc
        if len(parts) < 4 or parts[:3] != [1, 3, 6]:
            raise ConfigError(
                "enterprise_oid %r does not look like an OID under 1.3.6.1.4.1"
                % (self.enterprise_oid,)
            )
        if self.sys_object_id:
            try:
                parts = [int(x) for x in str(self.sys_object_id).lstrip(".").split(".")]
            except ValueError as exc:
                raise ConfigError(
                    "sys_object_id must be a dotted numeric OID, got %r"
                    % (self.sys_object_id,)
                ) from exc
            if len(parts) < 2:
                raise ConfigError("sys_object_id %r is too short" % (self.sys_object_id,))

        seen_ports: Dict[int, str] = {}
        for sc in self.switches:
            if sc.port in seen_ports:
                raise ConfigError(
                    "switches %s and %s are both configured on UDP port %d; "
                    "each switch needs its own port"
                    % (seen_ports[sc.port], sc.mac, sc.port)
                )
            seen_ports[sc.port] = sc.mac

    @property
    def ramp_window(self) -> float:
        """Observed-window ceiling past which ramping is skipped.

        Defaults to a multiple of ``poll_interval`` so it tracks the configured
        cadence, but can be pinned explicitly when backoff is expected to
        stretch the real interval well past that.
        """
        if self.max_ramp_seconds:
            return float(self.max_ramp_seconds)
        return float(self.poll_interval) * DEFAULT_MAX_RAMP_FACTOR

    def community_for(self, sc: SwitchConfig) -> str:
        return sc.community or self.community


# -- loading -------------------------------------------------------------
def _read_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError(
            "PyYAML is required to read config files; install with "
            "'pip install pyyaml'"
        ) from exc
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError("invalid YAML in %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError("%s must contain a YAML mapping at the top level" % path)
    return data


def _warn_if_readable(path: str) -> None:
    """Warn when a file holding a token is readable beyond its owner."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning(
            "%s contains an API token but is group/world readable; "
            "run: chmod 600 %s", path, path,
        )


def resolve_token(cfg_section: Dict[str, Any], config_path: Optional[str]) -> str:
    """Token precedence: environment, then token_file, then config file."""
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env

    token_file = cfg_section.get("token_file")
    if token_file:
        try:
            with open(str(token_file), "r", encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as exc:
            raise ConfigError("cannot read msp.token_file %s: %s" % (token_file, exc)) from exc
        if token:
            _warn_if_readable(str(token_file))
            return token

    inline = str(cfg_section.get("token") or "").strip()
    if inline and config_path:
        _warn_if_readable(config_path)
    return inline


def load(path: Optional[str] = None) -> Config:
    """Load configuration from ``path`` (YAML), applying env overrides."""
    data: Dict[str, Any] = {}
    if path:
        data = _read_yaml(path)

    msp = data.get("msp") or {}
    listen = data.get("listen") or {}

    cfg = Config(
        domain=str(msp.get("domain") or os.environ.get("FIREWALLA_MSP_DOMAIN") or ""),
        token=resolve_token(msp, path),
        poll_interval=int(data.get("poll_interval") or DEFAULT_POLL_INTERVAL),
        listen_address=str(listen.get("address") or "0.0.0.0"),
        base_port=int(listen.get("base_port") or DEFAULT_BASE_PORT),
        community=str(data.get("community") or DEFAULT_COMMUNITY),
        enterprise_oid=str(data.get("enterprise_oid") or DEFAULT_ENTERPRISE_OID),
        sys_object_id=(
            str(data["sys_object_id"]) if data.get("sys_object_id") else None
        ),
        sys_contact=str(data.get("sys_contact") or ""),
        sys_location=str(data.get("sys_location") or ""),
        state_file=str(data.get("state_file") or DEFAULT_STATE_FILE),
        counter_smoothing=str(
            data.get("counter_smoothing") or DEFAULT_SMOOTHING
        ).strip().lower(),
        max_ramp_seconds=int(data.get("max_ramp_seconds") or 0),
        max_backoff_seconds=int(
            data.get("max_backoff_seconds") or DEFAULT_MAX_BACKOFF
        ),
        box_gid=str(msp["box_gid"]) if msp.get("box_gid") else None,
        verify_tls=bool(data.get("verify_tls", True)),
        source_path=path,
    )

    for idx, entry in enumerate(data.get("switches") or []):
        if not isinstance(entry, dict) or not entry.get("mac"):
            raise ConfigError("switches[%d] must be a mapping with a 'mac' key" % idx)
        cfg.switches.append(
            SwitchConfig(
                mac=str(entry["mac"]),
                port=int(entry.get("port") or (cfg.base_port + idx)),
                name=str(entry["name"]) if entry.get("name") else None,
                community=str(entry["community"]) if entry.get("community") else None,
            )
        )
    return cfg
