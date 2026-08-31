"""Serving cached topology when the MSP API is unavailable.

The behaviour under test is the one that matters operationally: the NMS must
keep seeing the device, with its **full** port set, rather than going down --
while being told clearly that the counters are stale.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

import firewalla_snmp_proxy.cli as cli
from firewalla_snmp_proxy.config import Config, SwitchConfig
from firewalla_snmp_proxy.mibs import POLL_ERROR, POLL_OK, POLL_STALE, SwitchContext
from firewalla_snmp_proxy.msp_api import MspAuthError, MspError, MspRateLimited
from firewalla_snmp_proxy.reachability import DOWN, UNKNOWN, UP, ReachabilityMonitor
from firewalla_snmp_proxy.snapshot import TopologySnapshot
from firewalla_snmp_proxy.tree_builder import build_tree

from .conftest import NETWORKS

MAC = "20:6D:31:00:00:01"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        domain="dn-test.firewalla.net", token="tok",
        poll_interval=900, max_backoff_seconds=3600,
        state_file=str(tmp_path / "counters.json"),
        topology_cache=str(tmp_path / "topology.json"),
        switches=[SwitchConfig(mac=MAC, port=16100)],
    )


@pytest.fixture
def seeded_cache(cfg, switch_raw, switch_settings):
    """A cache as the poller would have written it, 40 minutes ago."""
    snap = TopologySnapshot(cfg.topology_cache)
    snap.save(
        "gid-1",
        {MAC: {"raw": switch_raw, "settings": switch_settings, "networks": NETWORKS}},
        saved_at=time.time() - 2400,
    )
    return snap


class _Client:
    last_latency = 0.01


def _fail(exc):
    def raiser(*a, **kw):
        raise exc
    return raiser


# -- the core behaviour ---------------------------------------------------
def test_rate_limited_startup_serves_cache_instead_of_failing(
    cfg, seeded_cache, monkeypatch
):
    monkeypatch.setattr(cli, "_resolve_gid", _fail(MspRateLimited("HTTP 429", 3600)))
    gid, agents = asyncio.run(
        cli._startup(cfg, _Client(), snapshot=seeded_cache)
    )
    assert gid == "gid-1"
    assert list(agents) == [MAC.upper()]


def test_cached_agents_publish_the_full_port_set(
    cfg, seeded_cache, switch_raw, monkeypatch
):
    """The reason a truncated ifTable was rejected: Observium deletes ports
    it stops hearing about, which would discard the graph history."""
    monkeypatch.setattr(cli, "_resolve_gid", _fail(MspRateLimited("HTTP 429")))
    _, agents = asyncio.run(cli._startup(cfg, _Client(), snapshot=seeded_cache))
    ctx = agents[MAC.upper()].ctx
    assert len(ctx.switch.ports) == len(switch_raw["ports"])

    tree = build_tree(ctx)
    for port in ctx.switch.ports:
        # ifHCInOctets.<ifIndex> must exist for every cached port.
        assert tree.get((1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 6, port.number)) is not None


def test_cached_startup_reports_the_real_data_age_not_a_fresh_poll(
    cfg, seeded_cache, monkeypatch
):
    monkeypatch.setattr(cli, "_resolve_gid", _fail(MspRateLimited("HTTP 429")))
    _, agents = asyncio.run(cli._startup(cfg, _Client(), snapshot=seeded_cache))
    ctx = agents[MAC.upper()].ctx
    assert ctx.serving_cache is True
    # ~2400s old, so it must not claim a recent successful poll.
    assert ctx.seconds_since_poll() > 2000
    assert "cached" in ctx.last_error.lower()


def test_cached_startup_does_not_inflate_poll_count(cfg, seeded_cache, monkeypatch):
    monkeypatch.setattr(cli, "_resolve_gid", _fail(MspRateLimited("HTTP 429")))
    _, agents = asyncio.run(cli._startup(cfg, _Client(), snapshot=seeded_cache))
    assert agents[MAC.upper()].ctx.poll_count == 0


def test_live_startup_writes_the_cache(cfg, switch_raw, switch_settings, monkeypatch):
    payload = {
        MAC.upper(): {
            "raw": switch_raw, "settings": switch_settings, "networks": NETWORKS,
        }
    }
    monkeypatch.setattr(cli, "_resolve_gid", lambda cl, c: "gid-live")
    monkeypatch.setattr(cli, "_fetch_payload", lambda c, cl, g: payload)

    snap = TopologySnapshot(cfg.topology_cache)
    gid, agents = asyncio.run(cli._startup(cfg, _Client(), snapshot=snap))
    assert gid == "gid-live"
    assert agents[MAC.upper()].ctx.serving_cache is False

    reloaded = TopologySnapshot(cfg.topology_cache).load()
    assert reloaded["gid"] == "gid-live"
    assert MAC.upper() in reloaded["switches"]


def test_no_cache_and_hard_error_still_fails_fast(cfg, monkeypatch):
    """A bad token must not be retried forever behind a rate-limit code path."""
    monkeypatch.setattr(cli, "_resolve_gid", _fail(MspAuthError("token rejected")))
    snap = TopologySnapshot(cfg.topology_cache)  # nothing written
    with pytest.raises(MspError):
        asyncio.run(cli._startup(cfg, _Client(), snapshot=snap))


def test_cache_covers_a_plain_api_outage_not_just_rate_limits(
    cfg, seeded_cache, monkeypatch
):
    """Connection failures deserve the same treatment as a 429."""
    monkeypatch.setattr(
        cli, "_resolve_gid", _fail(MspError("cannot reach https://...: timed out"))
    )
    gid, agents = asyncio.run(cli._startup(cfg, _Client(), snapshot=seeded_cache))
    assert agents[MAC.upper()].ctx.serving_cache is True


def test_corrupt_cache_is_ignored_rather_than_crashing(cfg, monkeypatch):
    with open(cfg.topology_cache, "w") as fh:
        fh.write("{not json")
    monkeypatch.setattr(cli, "_resolve_gid", _fail(MspAuthError("nope")))
    with pytest.raises(MspError):
        asyncio.run(cli._startup(cfg, _Client(), snapshot=TopologySnapshot(cfg.topology_cache)))


# -- reachability drives status, not the stale API field -----------------
def _ctx_with(switch, reach, *, cached=False, last_poll_age=0.0):
    from firewalla_snmp_proxy.counters import CounterStore

    ctx = SwitchContext(
        switch=switch, counters=CounterStore(None),
        enterprise_oid="1.3.6.1.4.1.99999", proxy_version="test",
        stale_after=2700.0, reach=reach,
    )
    ctx.last_poll_ok = time.time() - last_poll_age
    ctx.serving_cache = cached
    return ctx


def test_icmp_up_keeps_status_stale_not_error_during_a_lockout(switch):
    """Stale data from a switch we can still ping is degraded, not failed."""
    reach = ReachabilityMonitor("h")
    reach.feed(UP, 1.0)
    ctx = _ctx_with(switch, reach, cached=True, last_poll_age=4000.0)
    assert ctx.poll_status() == POLL_STALE
    assert ctx.switch_online() is True


def test_icmp_down_escalates_to_error(switch):
    reach = ReachabilityMonitor("h", fail_threshold=1)
    reach.feed(DOWN)
    ctx = _ctx_with(switch, reach, cached=True, last_poll_age=4000.0)
    assert ctx.poll_status() == POLL_ERROR
    assert ctx.switch_online() is False


def test_icmp_down_overrides_a_stale_api_online_true(switch):
    """The API's 'online' is only as fresh as the last poll; ICMP is now."""
    assert switch.online is True  # fixture says online
    reach = ReachabilityMonitor("h", fail_threshold=1)
    reach.feed(DOWN)
    ctx = _ctx_with(switch, reach, cached=True, last_poll_age=4000.0)
    assert ctx.switch_online() is False


def test_unknown_icmp_falls_back_to_the_api_field(switch):
    reach = ReachabilityMonitor("h")
    assert reach.state is UNKNOWN
    ctx = _ctx_with(switch, reach, last_poll_age=0.0)
    assert ctx.switch_online() is bool(switch.online)
    assert ctx.poll_status() == POLL_OK


def test_disabled_ping_leaves_behaviour_unchanged(switch):
    ctx = _ctx_with(switch, None, last_poll_age=0.0)
    assert ctx.switch_online() is bool(switch.online)
    assert ctx.poll_status() == POLL_OK


# -- new vendor OIDs ------------------------------------------------------
def test_vendor_oids_report_icmp_and_cache_state(switch):
    reach = ReachabilityMonitor("h")
    reach.feed(UP, 1.75)
    ctx = _ctx_with(switch, reach, cached=True, last_poll_age=4000.0)
    tree = build_tree(ctx)
    base = (1, 3, 6, 1, 4, 1, 99999, 1)

    assert int(tree.get(base + (7, 0))) == 1              # fwProxyIcmpStatus up
    assert int(tree.get(base + (8, 0))) == 1750           # fwProxyIcmpRtt, µs
    assert int(tree.get(base + (9, 0))) == 1              # fwProxyServingCache true
    # fwSwitchOnline reflects ICMP.
    assert int(tree.get((1, 3, 6, 1, 4, 1, 99999, 2, 9, 0))) == 1


def test_serving_cache_reads_false_on_live_data(switch):
    ctx = _ctx_with(switch, None, cached=False)
    tree = build_tree(ctx)
    assert int(tree.get((1, 3, 6, 1, 4, 1, 99999, 1, 9, 0))) == 2


def test_icmp_oids_report_disabled_when_no_ping_configured(switch):
    ctx = _ctx_with(switch, None)
    tree = build_tree(ctx)
    assert int(tree.get((1, 3, 6, 1, 4, 1, 99999, 1, 7, 0))) == 4  # disabled
    assert int(tree.get((1, 3, 6, 1, 4, 1, 99999, 1, 8, 0))) == 0


# -- dark probing ---------------------------------------------------------
def test_dark_startup_probes_faster_than_a_long_retry_after(cfg, monkeypatch):
    """With no cache the proxy serves nothing, so it must not sit on a
    static Retry-After: 3600 long after the quota has cleared."""
    cfg.dark_probe_max_seconds = 300
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)
        raise KeyboardInterrupt  # break the loop after the first wait

    monkeypatch.setattr(
        cli, "_resolve_gid", _fail(MspRateLimited("HTTP 429", 3600.0))
    )
    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            cli._startup(
                cfg, _Client(), snapshot=TopologySnapshot(cfg.topology_cache)
            )
        )
    assert waits == [300.0], waits


def test_cached_startup_never_reaches_the_dark_probe_path(
    cfg, seeded_cache, monkeypatch
):
    """When a cache exists the proxy is serving, so it can wait politely."""
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(
        cli, "_resolve_gid", _fail(MspRateLimited("HTTP 429", 3600.0))
    )
    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    _, agents = asyncio.run(cli._startup(cfg, _Client(), snapshot=seeded_cache))
    assert not slept, "startup slept instead of serving cache"
    assert agents[MAC.upper()].ctx.serving_cache is True


def test_topology_cache_write_failure_warns_only_once(tmp_path, caplog):
    """A bad state directory is static, so it must not warn on every poll."""
    import logging
    from firewalla_snmp_proxy.snapshot import TopologySnapshot

    unwritable = tmp_path / "nope"
    unwritable.mkdir()
    unwritable.chmod(0o500)  # r-x: cannot create the temp file inside
    snap = TopologySnapshot(str(unwritable / "topology.json"))

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            snap.save("gid", {})

    warnings = [r for r in caplog.records if "could not persist topology cache" in r.message]
    assert len(warnings) == 1, "expected exactly one warning, got %d" % len(warnings)
    unwritable.chmod(0o700)  # let pytest clean up


def test_counter_store_write_failure_warns_only_once(tmp_path, caplog):
    import logging
    from firewalla_snmp_proxy.counters import CounterStore

    unwritable = tmp_path / "nope2"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    store = CounterStore(str(unwritable / "counters.json"))

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            store.save(force=True)

    warnings = [r for r in caplog.records if "could not persist counter state" in r.message]
    assert len(warnings) == 1, "expected exactly one warning, got %d" % len(warnings)
    unwritable.chmod(0o700)


def test_default_state_dir_is_per_user_when_var_lib_is_not_writable(monkeypatch, tmp_path):
    """Regression: init hardcoded /var/lib, so `pipx install` + run as yourself
    warned on every poll and silently discarded counters and cache."""
    from firewalla_snmp_proxy import config as cfgmod

    monkeypatch.setattr(cfgmod.os, "access", lambda p, m: False)
    monkeypatch.setattr(cfgmod.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert cfgmod.default_state_dir() == str(tmp_path / "firewalla-snmp-proxy")
    assert cfgmod.default_state_file().endswith("firewalla-snmp-proxy/counters.json")
    assert cfgmod.default_topology_cache().endswith("firewalla-snmp-proxy/topology.json")


def test_default_state_dir_is_var_lib_for_root_and_for_a_writable_dir(monkeypatch):
    from firewalla_snmp_proxy import config as cfgmod

    monkeypatch.setattr(cfgmod.os, "access", lambda p, m: False)
    monkeypatch.setattr(cfgmod.os, "geteuid", lambda: 0)
    assert cfgmod.default_state_dir() == cfgmod.SERVICE_STATE_DIR

    monkeypatch.setattr(cfgmod.os, "access", lambda p, m: True)
    monkeypatch.setattr(cfgmod.os, "geteuid", lambda: 1000)
    assert cfgmod.default_state_dir() == cfgmod.SERVICE_STATE_DIR
