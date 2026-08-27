"""Model parsing: the traps are in type coercion and missing fields."""

from __future__ import annotations

import time

from firewalla_snmp_proxy.model import Port, Switch, _mac_to_bytes


def test_ports_sort_numerically_not_lexically(switch):
    """Port numbers arrive as strings; lexical sort would put 10 before 2.

    This is the bug that would silently scramble every ifIndex in the agent.
    """
    numbers = [p.number for p in switch.ports]
    assert numbers == sorted(numbers)
    assert numbers == list(range(1, 11))


def test_link_down_port_reports_zero_speed(switch):
    """The API omits linkSpeed entirely on a down port."""
    port9 = next(p for p in switch.ports if p.number == 9)
    assert not port9.link_up
    assert port9.speed_mbps == 0
    assert port9.oper_status == 2


def test_link_up_port(switch):
    port1 = next(p for p in switch.ports if p.number == 1)
    assert port1.link_up
    assert port1.speed_mbps == 1000
    assert port1.oper_status == 1


def test_fanless_switch_reports_no_temperature(switch):
    """temperature: 0 must be treated as 'not instrumented', not 0 degrees.

    Publishing a literal zero would trip low-temperature alarms on most NMSes.
    """
    assert switch.raw["health"]["temperature"] == 0
    assert switch.temperature_c is None


def test_positive_temperature_is_reported():
    sw = Switch(raw={"health": {"temperature": 42.5}}, polled_at=time.time())
    assert sw.temperature_c == 42.5


def test_negative_temperature_treated_as_uninstrumented():
    sw = Switch(raw={"health": {"temperature": -1}}, polled_at=time.time())
    assert sw.temperature_c is None


def test_poe_detection_status_mapping(switch):
    by_num = {p.number: p for p in switch.ports}
    assert by_num[3].poe_status == "delivering"
    assert by_num[3].poe_detection_status == 3   # deliveringPower
    assert by_num[1].poe_status == "searching"
    assert by_num[1].poe_detection_status == 2   # searching


def test_poe_capable_uses_status_not_the_poe_flag(switch):
    """A port only reports poe:true while delivering.

    Keying capability on that flag would make PoE rows vanish from the table
    whenever a powered device was unplugged.
    """
    port1 = next(p for p in switch.ports if p.number == 1)
    assert "poe" not in port1.raw          # not delivering
    assert port1.poe_capable               # but still a PoE port


def test_sfp_ports_detected(switch):
    by_num = {p.number: p for p in switch.ports}
    assert by_num[9].is_sfp and not by_num[9].sfp["present"]
    assert by_num[10].is_sfp and by_num[10].sfp["present"]
    assert not by_num[1].is_sfp


def test_stp_discarding_maps_to_blocking(switch):
    port9 = next(p for p in switch.ports if p.number == 9)
    assert port9.stp_state_str == "discarding"
    assert port9.stp_port_state == 2  # blocking, the legacy BRIDGE-MIB value


def test_poe_milliwatts_conversion(switch):
    """Watts -> milliwatts, keeping the API's 0.1 W resolution as an integer."""
    port3 = next(p for p in switch.ports if p.number == 3)
    watts = port3.raw["poePower"]
    assert watts > 0, "fixture should have a port delivering power"
    assert port3.poe_milliwatts == int(round(watts * 1000))


def test_poe_milliwatts_is_zero_when_not_delivering(switch):
    port1 = next(p for p in switch.ports if p.number == 1)
    assert "poePower" not in port1.raw
    assert port1.poe_milliwatts == 0


def test_port_alias_labels(switch):
    by_num = {p.number: p for p in switch.ports}
    assert "Uplink" in switch.port_alias(by_num[1])
    assert switch.port_alias(by_num[2]) == "Trunk"
    assert switch.port_alias(by_num[8]) == "Access: IoTVLan"
    assert switch.port_alias(by_num[10]) == "Access: ExampleLAN"


def test_uptime_ticks_advance_with_wall_clock():
    """sysUpTime must tick between polls, not freeze.

    A static sysUpTime reads as an agent restart to some NMSes, which then
    discard counter deltas.
    """
    sw = Switch(raw={"systemStatus": {"uptime": 100}}, polled_at=time.time() - 5)
    assert sw.uptime_ticks >= 10500


def test_discontinuity_before_boot_returns_zero(switch):
    """statsSinceTs predating boot means no discontinuity in this uptime window.

    RFC 2863 uses 0 for 'no discontinuity known', which is the honest answer.
    """
    port1 = next(p for p in switch.ports if p.number == 1)
    assert switch.ticks_at_epoch(port1.stats_since) == 0


def test_discontinuity_after_boot_is_converted():
    now = time.time()
    sw = Switch(raw={"systemStatus": {"uptime": 1000}}, polled_at=now)
    # A reset 100s ago is 900s after boot, i.e. ~90000 hundredths. Tolerance
    # covers the sub-second truncation in the epoch cast.
    assert abs(sw.ticks_at_epoch(int(now - 100)) - 90000) <= 100


def test_missing_fields_yield_none_not_crash():
    sw = Switch(raw={}, polled_at=time.time())
    assert sw.serial is None
    assert sw.firmware_rev is None
    assert sw.uptime_seconds is None
    assert sw.uptime_ticks == 0
    assert sw.ports == []
    assert sw.temperature_c is None
    assert sw.poe_used_watts is None


def test_malformed_port_number_is_skipped():
    sw = Switch(raw={"ports": [{"port": "not-a-number"}, {"port": "3"}]})
    assert [p.number for p in sw.ports] == [3]


def test_mac_to_bytes():
    assert _mac_to_bytes("20:6D:31:00:00:01") == b"\x20\x6d\x31\x00\x00\x01"
    assert _mac_to_bytes("20-6D-31-00-00-01") == b"\x20\x6d\x31\x00\x00\x01"
    assert _mac_to_bytes(None) == b""
    assert _mac_to_bytes("garbage") == b""
    assert _mac_to_bytes("ZZ:6D:31:00:00:01") == b""
