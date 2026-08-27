"""MIB mapping: the right values land at the right OIDs, in standard MIBs."""

from __future__ import annotations

from pysnmp.proto import rfc1902

IF_ENTRY = (1, 3, 6, 1, 2, 1, 2, 2, 1)
IFX_ENTRY = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1)
PETH_PORT = (1, 3, 6, 1, 2, 1, 105, 1, 1, 1)
PETH_MAIN = (1, 3, 6, 1, 2, 1, 105, 1, 3, 1)
STP_PORT = (1, 3, 6, 1, 2, 1, 17, 2, 15, 1)
ENT_PHYS = (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1)
LLDP_REM = (1, 0, 8802, 1, 1, 2, 1, 4, 1, 1)
VENDOR = (1, 3, 6, 1, 4, 1, 99999)


def val(tree, oid):
    return tree.get(oid)


# -- system --------------------------------------------------------------
def test_sysdescr_identifies_vendor_and_model(tree):
    descr = str(val(tree, (1, 3, 6, 1, 2, 1, 1, 1, 0)))
    assert "Firewalla" in descr
    assert "Firewalla-Switch-SE" in descr
    assert "1.12.0" in descr  # firmware


def test_sysservices_is_layer2(tree):
    assert int(val(tree, (1, 3, 6, 1, 2, 1, 1, 7, 0))) == 2


def test_sysuptime_is_populated(tree):
    assert int(val(tree, (1, 3, 6, 1, 2, 1, 1, 3, 0))) > 0


# -- IF-MIB --------------------------------------------------------------
def test_ifnumber_matches_port_count(tree, switch):
    assert int(val(tree, (1, 3, 6, 1, 2, 1, 2, 1, 0))) == len(switch.ports)


def test_ifoperstatus_reflects_link_state(tree):
    assert int(val(tree, IF_ENTRY + (8, 1))) == 1   # up
    assert int(val(tree, IF_ENTRY + (8, 9))) == 2   # down


def test_ifspeed_is_bits_per_second(tree):
    assert int(val(tree, IF_ENTRY + (5, 1))) == 1_000_000_000
    assert int(val(tree, IFX_ENTRY + (15, 1))) == 1000  # ifHighSpeed, Mbit/s


def test_ifspeed_saturates_at_gauge32_max():
    """A 10G port must saturate, not wrap, per RFC 2863."""
    from firewalla_snmp_proxy.mibs.ifmib import GAUGE32_MAX, _if_speed

    assert _if_speed(10000) == GAUGE32_MAX
    assert _if_speed(2500) == 2_500_000_000  # 2.5G still fits


def test_64bit_octets_carry_full_value(tree, switch):
    port1 = next(p for p in switch.ports if p.number == 1)
    assert int(val(tree, IFX_ENTRY + (6, 1))) == port1.counter("rxBytes")
    assert int(val(tree, IFX_ENTRY + (10, 1))) == port1.counter("txBytes")


def test_32bit_octets_are_the_64bit_value_folded(tree, switch):
    port1 = next(p for p in switch.ports if p.number == 1)
    expected = port1.counter("rxBytes") & 0xFFFFFFFF
    assert int(val(tree, IF_ENTRY + (10, 1))) == expected


def test_error_counters_are_mapped(tree):
    assert val(tree, IF_ENTRY + (14, 1)) is not None   # ifInErrors
    assert val(tree, IF_ENTRY + (20, 1)) is not None   # ifOutErrors
    assert val(tree, IF_ENTRY + (13, 1)) is not None   # ifInDiscards
    assert val(tree, IF_ENTRY + (19, 1)) is not None   # ifOutDiscards


def test_deprecated_nucast_is_multicast_plus_broadcast(tree, switch):
    port1 = next(p for p in switch.ports if p.number == 1)
    expected = (
        port1.counter("rxMulticastFrames") + port1.counter("rxBroadcastFrames")
    ) & 0xFFFFFFFF
    assert int(val(tree, IF_ENTRY + (12, 1))) == expected


def test_ifalias_carries_vlan_name(tree):
    assert "IoTVLan" in str(val(tree, IFX_ENTRY + (18, 8)))
    assert "Uplink" in str(val(tree, IFX_ENTRY + (18, 1)))


def test_ifname_is_the_bare_port_number(tree):
    assert str(val(tree, IFX_ENTRY + (1, 10))) == "10"


def test_unavailable_ifmib_objects_are_not_published(tree):
    """Absent beats invented.

    ifPhysAddress (no per-port MAC), ifInUnknownProtos and ifOutQLen are not
    supplied by the API, so they must not appear at all.
    """
    assert val(tree, IF_ENTRY + (6, 1)) is None    # ifPhysAddress
    assert val(tree, IF_ENTRY + (15, 1)) is None   # ifInUnknownProtos
    assert val(tree, IF_ENTRY + (21, 1)) is None   # ifOutQLen


def test_counter_discontinuity_time_is_published(tree):
    assert val(tree, IFX_ENTRY + (19, 1)) is not None


# -- POWER-ETHERNET-MIB --------------------------------------------------
def test_poe_detection_status_enum(tree):
    assert int(val(tree, PETH_PORT + (6, 1, 3))) == 3  # deliveringPower
    assert int(val(tree, PETH_PORT + (6, 1, 1))) == 2  # searching


def test_non_poe_ports_have_no_peth_row(tree, switch):
    """SFP ports report no PoE state, so they get no PoE row at all."""
    port10 = next(p for p in switch.ports if p.number == 10)
    assert not port10.poe_capable
    assert val(tree, PETH_PORT + (6, 1, 10)) is None


def test_main_pse_consumption_in_whole_watts(tree, switch):
    expected = int(round(switch.poe_used_watts))
    assert int(val(tree, PETH_MAIN + (4, 1))) == expected


def test_pse_budget_is_not_guessed(tree):
    """pethMainPsePower is omitted: the API never reports the actual budget."""
    assert val(tree, PETH_MAIN + (2, 1)) is None


def test_unknown_poe_counters_are_omitted(tree):
    """Zeros would read as 'no PoE fault has ever occurred'."""
    for col in (8, 11, 12, 13, 14):
        assert val(tree, PETH_PORT + (col, 1, 1)) is None


# -- BRIDGE-MIB ----------------------------------------------------------
def test_stp_port_state_mapping(tree):
    assert int(val(tree, STP_PORT + (3, 1))) == 5  # forwarding
    assert int(val(tree, STP_PORT + (3, 9))) == 2  # discarding -> blocking


def test_bridge_address_and_port_count(tree, switch):
    assert bytes(val(tree, (1, 3, 6, 1, 2, 1, 17, 1, 1, 0))) == switch.mac_bytes
    assert int(val(tree, (1, 3, 6, 1, 2, 1, 17, 1, 2, 0))) == len(switch.ports)


def test_stp_group_absent_when_stp_disabled(switch, ctx):
    from firewalla_snmp_proxy.tree_builder import build_tree

    ctx.switch.settings = {"stp": {"enable": False}}
    tree = build_tree(ctx)
    assert tree.get(STP_PORT + (3, 1)) is None
    # base bridge info is still valid without STP
    assert tree.get((1, 3, 6, 1, 2, 1, 17, 1, 2, 0)) is not None


# -- ENTITY-MIB ----------------------------------------------------------
def test_chassis_inventory(tree, switch):
    assert str(val(tree, ENT_PHYS + (11, 1))) == switch.serial
    assert str(val(tree, ENT_PHYS + (8, 1))) == switch.hardware_rev
    assert str(val(tree, ENT_PHYS + (9, 1))) == switch.firmware_rev
    assert int(val(tree, ENT_PHYS + (5, 1))) == 3  # chassis class


def test_sfp_entity_only_for_sfp_ports(tree):
    assert val(tree, ENT_PHYS + (5, 210)) is not None  # port 10 has a cage
    assert val(tree, ENT_PHYS + (5, 201)) is None      # port 1 does not


def test_sfp_module_is_marked_field_replaceable(tree):
    assert int(val(tree, ENT_PHYS + (16, 210))) == 1  # true
    assert int(val(tree, ENT_PHYS + (16, 110))) == 2  # fixed port: false


# -- ENTITY-SENSOR-MIB ---------------------------------------------------
def test_no_temperature_sensor_on_fanless_switch(tree):
    """The whole sensor subtree must be absent, not zero-valued."""
    for col in range(1, 9):
        assert val(tree, (1, 3, 6, 1, 2, 1, 99, 1, 1, 1, col, 1)) is None


def test_temperature_sensor_appears_when_instrumented(ctx):
    from firewalla_snmp_proxy.tree_builder import build_tree

    ctx.switch.raw["health"]["temperature"] = 41.5
    tree = build_tree(ctx)
    # entPhySensorValue, precision 1 -> tenths of a degree
    assert int(tree.get((1, 3, 6, 1, 2, 1, 99, 1, 1, 1, 4, 1))) == 415
    assert int(tree.get((1, 3, 6, 1, 2, 1, 99, 1, 1, 1, 1, 1))) == 8  # celsius


# -- LLDP-MIB ------------------------------------------------------------
def test_uplink_published_as_lldp_neighbour(tree, switch):
    """This is what makes an NMS draw the switch-to-Firewalla topology link."""
    local_port = switch.uplink_local_port
    idx = (0, local_port, 1)
    assert int(val(tree, LLDP_REM + (4,) + idx)) == 4  # macAddress subtype
    assert bytes(val(tree, LLDP_REM + (5,) + idx)) != b""
    assert str(val(tree, LLDP_REM + (7,) + idx)) == switch.uplink["port"]


def test_lldp_index_columns_are_not_instantiated(tree, switch):
    """lldpRemTimeMark/LocalPortNum/Index are not-accessible per the MIB."""
    idx = (0, switch.uplink_local_port, 1)
    for col in (1, 2, 3):
        assert val(tree, LLDP_REM + (col,) + idx) is None


def test_no_lldp_neighbour_without_uplink(ctx):
    from firewalla_snmp_proxy.tree_builder import build_tree

    ctx.switch.raw.pop("uplink", None)
    tree = build_tree(ctx)
    assert tree.get(LLDP_REM + (4, 0, 1, 1)) is None


# -- vendor subtree ------------------------------------------------------
def test_per_port_poe_wattage_in_milliwatts(tree, switch):
    port3 = next(p for p in switch.ports if p.number == 3)
    assert int(val(tree, VENDOR + (3, 1, 1, 2, 3))) == port3.poe_milliwatts


def test_vendor_port_table_depth_matches_shipped_mib(tree):
    """fwPortEntry is .3.1.1, so columns are .3.1.1.<col>.<index>.

    Serving them one level shallower still walks cleanly but makes every column
    resolve as a bare 'fwPortTable.<col>' against the shipped MIB file.
    """
    assert val(tree, VENDOR + (3, 1, 1, 1, 1)) is not None   # fwPortIndex.1
    assert val(tree, VENDOR + (3, 1, 2, 1)) is None          # wrong depth


def test_stp_role_in_vendor_subtree(tree):
    assert str(val(tree, VENDOR + (3, 1, 1, 5, 1))) == "designated"


def test_acl_counts_all_four_published(tree, switch):
    for col, key in ((15, "count"), (16, "max"), (17, "tracking"), (18, "control")):
        assert int(val(tree, VENDOR + (2, col, 0))) == switch.acl[key]


def test_proxy_health_group(tree):
    assert int(val(tree, VENDOR + (1, 3, 0))) == 1   # fwProxyPollStatus ok
    assert str(val(tree, VENDOR + (1, 1, 0))) == "0.1.0-test"


def test_poll_status_goes_stale(ctx, tree):
    import time

    ctx.last_poll_ok = time.time() - 10_000
    assert int(tree.get(VENDOR + (1, 3, 0))) == 2  # stale


def test_poll_status_error_when_never_polled(ctx, tree):
    ctx.last_poll_ok = None
    assert int(tree.get(VENDOR + (1, 3, 0))) == 3  # error


def test_sfp_columns_only_for_sfp_ports(tree):
    assert val(tree, VENDOR + (3, 1, 1, 11, 10)) is not None  # port 10 SFP
    assert val(tree, VENDOR + (3, 1, 1, 11, 1)) is None       # port 1 copper


# -- whole-tree invariants ----------------------------------------------
def test_every_oid_renders_without_error(tree):
    for oid in tree.oids:
        assert tree.get(oid) is not None, oid


def test_tree_is_strictly_ordered(tree):
    assert tree.oids == sorted(tree.oids)
    assert len(tree.oids) == len(set(tree.oids))


def test_all_values_are_snmp_types(tree):
    for oid in tree.oids:
        value = tree.get(oid)
        assert isinstance(value, rfc1902.univ.base.Asn1Item), (oid, type(value))


# -- sysObjectID override (migration support) ----------------------------
def test_sysobjectid_defaults_into_enterprise_subtree(tree, ctx):
    from pysnmp.proto import rfc1902

    oid = val(tree, (1, 3, 6, 1, 2, 1, 1, 2, 0))
    assert tuple(oid) == ctx.enterprise + (2,)


def test_sysobjectid_can_be_pinned_for_drop_in_replacement(switch):
    """Replacing an existing proxy must be able to keep the old identity.

    Monitoring systems key OS detection on sysObjectID, so changing it makes an
    NMS treat the device as new and abandon its historical RRDs.
    """
    from firewalla_snmp_proxy.counters import CounterStore
    from firewalla_snmp_proxy.mibs import SwitchContext
    from firewalla_snmp_proxy.tree_builder import build_tree

    legacy = "1.3.6.1.4.1.8072.3.2.10.99.2"
    ctx = SwitchContext(
        switch=switch, counters=CounterStore(None), sys_object_id=legacy
    )
    tree = build_tree(ctx)
    assert ".".join(str(x) for x in tree.get((1, 3, 6, 1, 2, 1, 1, 2, 0))) == legacy
    # ENTITY-MIB entPhysicalVendorType must agree with it.
    assert ".".join(
        str(x) for x in tree.get((1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 3, 1))
    ) == legacy


def test_leading_dot_in_sysobjectid_is_tolerated(switch):
    from firewalla_snmp_proxy.counters import CounterStore
    from firewalla_snmp_proxy.mibs import SwitchContext
    from firewalla_snmp_proxy.tree_builder import build_tree

    ctx = SwitchContext(
        switch=switch, counters=CounterStore(None), sys_object_id=".1.3.6.1.4.1.999"
    )
    tree = build_tree(ctx)
    assert tuple(tree.get((1, 3, 6, 1, 2, 1, 1, 2, 0))) == (1, 3, 6, 1, 4, 1, 999)


def test_config_name_override_wins_over_api_hostname(switch_raw, switch_settings):
    """A `name:` override must change what the NMS actually displays.

    sysName otherwise prefers the API's hostname, so an override that left
    sysName untouched would silently do nothing visible.
    """
    import time

    from firewalla_snmp_proxy.counters import CounterStore
    from firewalla_snmp_proxy.mibs import SwitchContext
    from firewalla_snmp_proxy.model import Switch
    from firewalla_snmp_proxy.tree_builder import build_tree

    raw = dict(switch_raw)
    raw["hostname"] = "api-supplied-name"
    ctx = SwitchContext(
        switch=Switch(
            raw=raw, settings=switch_settings, polled_at=time.time(),
            name_override="my-switch",
        ),
        counters=CounterStore(None),
    )
    tree = build_tree(ctx)
    assert str(tree.get((1, 3, 6, 1, 2, 1, 1, 5, 0))) == "my-switch"
    assert str(tree.get((1, 0, 8802, 1, 1, 2, 1, 3, 3, 0))) == "my-switch"


def test_name_override_survives_a_poll_cycle(switch_raw, switch_settings):
    """Regression: the poller rebuilds the Switch every cycle.

    An override applied once at startup was silently discarded by the first
    poll, so sysName reverted to the API's hostname a minute after start.
    """
    import time

    from firewalla_snmp_proxy.counters import CounterStore
    from firewalla_snmp_proxy.mibs import SwitchContext
    from firewalla_snmp_proxy.model import Switch
    from firewalla_snmp_proxy.tree_builder import build_tree

    ctx = SwitchContext(
        switch=Switch(raw=dict(switch_raw), polled_at=time.time(),
                      name_override="pinned-name"),
        counters=CounterStore(None),
    )
    tree = build_tree(ctx)
    assert str(tree.get((1, 3, 6, 1, 2, 1, 1, 5, 0))) == "pinned-name"

    # Simulate exactly what Poller.poll_once does.
    ctx.switch = Switch(
        raw=dict(switch_raw), settings=switch_settings, polled_at=time.time(),
        name_override=ctx.name_override,
    )
    assert str(tree.get((1, 3, 6, 1, 2, 1, 1, 5, 0))) == "pinned-name"
