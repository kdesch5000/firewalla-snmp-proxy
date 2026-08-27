"""Normalized model of a Firewalla switch, parsed from MSP API JSON.

Everything the MIB layer needs is resolved here, so the MIB modules never touch
raw API dicts. The API is undocumented and its field names have already churned
several times, so all access is defensive: a missing field yields ``None`` and
the corresponding SNMP object is simply not instantiated, rather than being
published as a plausible-looking zero.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# pethPsePortDetectionStatus (RFC 3621)
POE_DETECTION = {
    "disabled": 1,
    "searching": 2,
    "delivering": 3,
    "deliveringpower": 3,
    "fault": 4,
    "test": 5,
    "otherfault": 6,
}

# dot1dStpPortState (RFC 1493). RSTP "discarding" collapses onto blocking(2),
# which is what RSTP-aware NMSes expect from a legacy BRIDGE-MIB view.
STP_PORT_STATE = {
    "disabled": 1,
    "blocking": 2,
    "discarding": 2,
    "listening": 3,
    "learning": 4,
    "forwarding": 5,
    "broken": 6,
}


def _mac_to_bytes(mac: Optional[str]) -> bytes:
    """``"20:6D:31:00:00:01"`` -> 6 raw bytes, for MacAddress/PhysAddress."""
    if not mac:
        return b""
    parts = str(mac).replace("-", ":").split(":")
    if len(parts) != 6:
        return b""
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError:
        return b""


@dataclass
class Port:
    """One switch port, normalized."""

    number: int
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    # -- link ------------------------------------------------------------
    @property
    def link_up(self) -> bool:
        return bool(self.raw.get("linkUp"))

    @property
    def speed_mbps(self) -> int:
        """0 when the link is down -- the API omits linkSpeed entirely then."""
        return int(self.raw.get("linkSpeed") or 0)

    @property
    def oper_status(self) -> int:
        """ifOperStatus: up(1) / down(2)."""
        return 1 if self.link_up else 2

    # -- classification --------------------------------------------------
    @property
    def port_type(self) -> Optional[str]:
        return (self.raw.get("settings") or {}).get("type")

    @property
    def network_uuid(self) -> Optional[str]:
        return (self.raw.get("settings") or {}).get("intf")

    @property
    def is_sfp(self) -> bool:
        return "sfpInfo" in self.raw

    @property
    def sfp(self) -> Dict[str, Any]:
        return self.raw.get("sfpInfo") or {}

    # -- PoE -------------------------------------------------------------
    @property
    def poe_capable(self) -> bool:
        """A port is PoE-capable if it reports any PoE state at all.

        ``poe: true`` only appears while actually delivering, so relying on it
        would make ports flip out of the PoE table when a device unplugs.
        """
        return "poeStatus" in self.raw or "poe" in self.raw

    @property
    def poe_status(self) -> Optional[str]:
        return self.raw.get("poeStatus")

    @property
    def poe_detection_status(self) -> int:
        return POE_DETECTION.get(str(self.poe_status or "").lower(), 1)

    @property
    def poe_milliwatts(self) -> int:
        """Per-port draw in mW. RFC 3621 has no per-port power object, so this
        is surfaced in the vendor subtree; mW keeps it an integer."""
        return int(round(float(self.raw.get("poePower") or 0.0) * 1000))

    @property
    def poe_mode(self) -> Optional[str]:
        return self.raw.get("poeMode")

    # -- STP -------------------------------------------------------------
    @property
    def stp_port_state(self) -> int:
        stp = self.raw.get("stp") or {}
        return STP_PORT_STATE.get(str(stp.get("portState") or "").lower(), 1)

    @property
    def stp_role(self) -> Optional[str]:
        return (self.raw.get("stp") or {}).get("portRole")

    @property
    def stp_state_str(self) -> Optional[str]:
        return (self.raw.get("stp") or {}).get("portState")

    # -- counters --------------------------------------------------------
    @property
    def stats_since(self) -> Optional[int]:
        ts = self.raw.get("statsSinceTs")
        return int(ts) if ts is not None else None

    def counter(self, name: str) -> int:
        return int(self.raw.get(name) or 0)

    @property
    def in_non_unicast(self) -> int:
        return self.counter("rxMulticastFrames") + self.counter("rxBroadcastFrames")

    @property
    def out_non_unicast(self) -> int:
        return self.counter("txMulticastFrames") + self.counter("txBroadcastFrames")


@dataclass
class Switch:
    """One Firewalla switch, normalized from ``/topology`` + ``/switches/<mac>``."""

    raw: Dict[str, Any] = field(repr=False, default_factory=dict)
    settings: Dict[str, Any] = field(repr=False, default_factory=dict)
    networks: Dict[str, str] = field(default_factory=dict)  # uuid -> name
    polled_at: float = 0.0
    #: Operator-configured display name. Held on the model rather than patched
    #: into ``raw`` so it survives the poller rebuilding the Switch each cycle.
    name_override: Optional[str] = None

    # -- identity --------------------------------------------------------
    @property
    def mac(self) -> str:
        return str(self.raw.get("id") or self.raw.get("mac") or "")

    @property
    def mac_bytes(self) -> bytes:
        return _mac_to_bytes(self.mac)

    @property
    def name(self) -> str:
        if self.name_override:
            return self.name_override
        return str(self.raw.get("name") or self.mac or "firewalla-switch")

    @property
    def hostname(self) -> Optional[str]:
        """Preferred source for sysName.

        An operator override wins over the API's hostname: a configured name
        that did not change what the NMS displays would be surprising.
        """
        if self.name_override:
            return self.name_override
        return self.raw.get("hostname")

    @property
    def ip(self) -> Optional[str]:
        return self.raw.get("ip")

    @property
    def online(self) -> bool:
        return bool(self.raw.get("online"))

    @property
    def model(self) -> Optional[str]:
        return self.raw.get("model")

    def _sys(self, key: str) -> Any:
        return (self.raw.get("systemStatus") or {}).get(key)

    @property
    def model_name(self) -> Optional[str]:
        return self._sys("modelName") or self.model

    @property
    def serial(self) -> Optional[str]:
        return self._sys("serialNumber")

    @property
    def hardware_rev(self) -> Optional[str]:
        return self._sys("hardwareVersion")

    @property
    def firmware_rev(self) -> Optional[str]:
        return self._sys("firmwareVersion")

    @property
    def protocol_version(self) -> Optional[str]:
        return self._sys("protocolVersion")

    @property
    def software_rev(self) -> Optional[str]:
        """The switch's MSP-side agent version, distinct from its firmware."""
        return self.raw.get("version")

    @property
    def active_branch(self) -> Optional[str]:
        return self.raw.get("activeBranch")

    # -- uptime ----------------------------------------------------------
    @property
    def uptime_seconds(self) -> Optional[int]:
        up = self._sys("uptime")
        return int(up) if up is not None else None

    @property
    def uptime_ticks(self) -> int:
        """sysUpTime in hundredths of a second.

        Advances between polls using wall clock so sysUpTime ticks smoothly
        instead of freezing between API calls -- some NMSes treat a static
        sysUpTime as an agent restart and discard counter deltas.
        """
        up = self.uptime_seconds
        if up is None:
            return 0
        drift = max(0.0, time.time() - self.polled_at) if self.polled_at else 0.0
        return int((up + drift) * 100)

    @property
    def boot_epoch(self) -> Optional[float]:
        """Wall-clock time the switch booted; anchors counter discontinuities."""
        up = self.uptime_seconds
        if up is None or not self.polled_at:
            return None
        return self.polled_at - up

    def ticks_at_epoch(self, epoch: Optional[int]) -> int:
        """Convert an absolute epoch into a sysUpTime-relative TimeStamp.

        Used for ifCounterDiscontinuityTime. Returns 0 (the "no discontinuity
        known" value) when it cannot be anchored or predates boot.
        """
        boot = self.boot_epoch
        if epoch is None or boot is None:
            return 0
        delta = float(epoch) - boot
        if delta <= 0:
            return 0
        return int(delta * 100)

    # -- health / PoE / ACL ----------------------------------------------
    @property
    def health(self) -> Dict[str, Any]:
        return self.raw.get("health") or {}

    @property
    def healthy_str(self) -> Optional[str]:
        return self.health.get("healthy")

    @property
    def fan_status(self) -> Optional[str]:
        return self.health.get("fanStatus")

    @property
    def temperature_c(self) -> Optional[float]:
        """``None`` on fanless models.

        The SE reports ``temperature: 0`` with ``fanStatus: "none"``. Publishing
        a literal 0 degrees would trip low-temperature alarms on most NMSes, so
        a non-positive reading is treated as "not instrumented" and the sensor
        is omitted from ENTITY-SENSOR-MIB entirely.
        """
        for src in (self.health, self.raw.get("systemStatus") or {}):
            val = src.get("temperature")
            if val is not None and float(val) > 0:
                return float(val)
        return None

    @property
    def poe_used_watts(self) -> Optional[float]:
        poe = self.raw.get("poe") or {}
        return float(poe["used"]) if poe.get("used") is not None else None

    @property
    def poe_budget_util(self) -> Optional[int]:
        poe = self.raw.get("poe") or {}
        return int(poe["budgetUtil"]) if poe.get("budgetUtil") is not None else None

    @property
    def acl(self) -> Dict[str, Any]:
        return self.raw.get("acl") or {}

    # -- uplink ----------------------------------------------------------
    @property
    def uplink(self) -> Dict[str, Any]:
        return self.raw.get("uplink") or {}

    @property
    def uplink_local_port(self) -> Optional[int]:
        lp = self.uplink.get("localPort")
        try:
            return int(lp)
        except (TypeError, ValueError):
            return None

    # -- switch-wide settings -------------------------------------------
    @property
    def flow_control(self) -> Optional[bool]:
        val = self.settings.get("flowControl")
        return bool(val) if val is not None else None

    @property
    def stp_enabled(self) -> Optional[bool]:
        stp = self.settings.get("stp") or {}
        return bool(stp["enable"]) if stp.get("enable") is not None else None

    @property
    def stp_protocol(self) -> Optional[str]:
        return (self.settings.get("stp") or {}).get("protocol")

    # -- ports -----------------------------------------------------------
    @property
    def ports(self) -> List[Port]:
        """Ports sorted numerically -- the API returns port numbers as strings,
        so a lexical sort would order 10 before 2 and corrupt ifIndex."""
        out: List[Port] = []
        for raw in self.raw.get("ports") or []:
            try:
                num = int(raw.get("port"))
            except (TypeError, ValueError):
                continue
            out.append(Port(number=num, raw=raw))
        return sorted(out, key=lambda p: p.number)

    def network_name(self, uuid: Optional[str]) -> Optional[str]:
        return self.networks.get(uuid) if uuid else None

    def port_alias(self, port: Port) -> str:
        """ifAlias text: the operator-meaningful label for the port."""
        if self.uplink_local_port == port.number:
            peer = self.uplink.get("mac") or "upstream"
            return "Uplink to %s (%s)" % (peer, self.uplink.get("port") or "?")
        net = self.network_name(port.network_uuid)
        if port.port_type == "access":
            return "Access: %s" % (net or port.network_uuid or "unassigned")
        if port.port_type == "trunk":
            return "Trunk"
        return ""
