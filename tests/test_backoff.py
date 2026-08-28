"""Rate-limit handling: 429 detection, Retry-After, and poller backoff.

No network access; the MSP client is faked at the method level.
"""

from __future__ import annotations

import asyncio
import email.utils
import datetime
import io
import time
import urllib.error

import pytest

from firewalla_snmp_proxy.counters import CounterStore
from firewalla_snmp_proxy.msp_api import (
    MspClient,
    MspError,
    MspRateLimited,
    parse_retry_after,
)
from firewalla_snmp_proxy.poller import JITTER, Poller


# -- Retry-After parsing --------------------------------------------------
def test_retry_after_delta_seconds():
    assert parse_retry_after("120") == 120.0


def test_retry_after_http_date():
    # An HTTP-date has one-second resolution, so the result is 300 minus
    # however far into the current second the clock happens to be. Assert the
    # range rather than an exact value; an equality here fails ~99% of runs.
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=300)
    assert 299.0 <= parse_retry_after(email.utils.format_datetime(when)) <= 300.0


def test_retry_after_in_the_past_clamps_to_zero():
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


@pytest.mark.parametrize("value", [None, "", "soon", "not-a-date"])
def test_retry_after_unparseable_returns_none(value):
    assert parse_retry_after(value) is None


# -- client raises the right type -----------------------------------------
def _http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://x/y", code, "boom", headers or {}, io.BytesIO(b"")
    )


@pytest.mark.parametrize("code", [429, 503])
def test_client_raises_rate_limited(monkeypatch, code):
    client = MspClient("dn-test.firewalla.net", "tok")

    def boom(*a, **kw):
        raise _http_error(code, {"Retry-After": "42"})

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(MspRateLimited) as ei:
        client.get("v2/boxes")
    assert ei.value.retry_after == 42.0


def test_client_rate_limited_without_header_has_no_retry_after(monkeypatch):
    client = MspClient("dn-test.firewalla.net", "tok")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **kw: (_ for _ in ()).throw(_http_error(429))
    )
    with pytest.raises(MspRateLimited) as ei:
        client.get("v2/boxes")
    assert ei.value.retry_after is None


def test_other_http_errors_are_not_rate_limits(monkeypatch):
    client = MspClient("dn-test.firewalla.net", "tok")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **kw: (_ for _ in ()).throw(_http_error(500))
    )
    with pytest.raises(MspError) as ei:
        client.get("v2/boxes")
    assert not isinstance(ei.value, MspRateLimited)


def test_network_names_does_not_swallow_a_rate_limit(monkeypatch):
    """It swallows ordinary errors by design, which would hide a 429."""
    client = MspClient("dn-test.firewalla.net", "tok")
    monkeypatch.setattr(
        MspClient, "devices",
        lambda self, gid: (_ for _ in ()).throw(MspRateLimited("429", 10)),
    )
    with pytest.raises(MspRateLimited):
        client.network_names("gid")


def test_network_names_still_swallows_ordinary_errors(monkeypatch):
    client = MspClient("dn-test.firewalla.net", "tok")
    monkeypatch.setattr(
        MspClient, "devices",
        lambda self, gid: (_ for _ in ()).throw(MspError("nope")),
    )
    assert client.network_names("gid") == {}


# -- poller backoff schedule ---------------------------------------------
class FakeClient:
    """Counts calls and can be told to rate-limit."""

    def __init__(self, rate_limited=False, retry_after=None):
        self.rate_limited = rate_limited
        self.retry_after = retry_after
        self.calls = 0
        self.last_latency = 0.01

    def _maybe_raise(self):
        self.calls += 1
        if self.rate_limited:
            raise MspRateLimited("HTTP 429", self.retry_after)

    def network_names(self, gid):
        self._maybe_raise()
        return {}

    def find_switches(self, gid):
        self._maybe_raise()
        return []

    def switch_settings(self, gid):
        self._maybe_raise()
        return {}

    def switch_detail(self, gid, mac):
        self._maybe_raise()
        return {}


def _poller(client, interval=900, max_backoff=3600.0):
    return Poller(client, "gid", {}, CounterStore(None), interval, max_backoff)


def test_retry_after_wins_over_the_computed_schedule():
    p = _poller(FakeClient())
    p._strikes = 5  # would otherwise be a very long exponential delay
    assert p.backoff_delay(MspRateLimited("429", 30.0)) == 30.0


def test_retry_after_is_still_capped():
    p = _poller(FakeClient(), max_backoff=600.0)
    assert p.backoff_delay(MspRateLimited("429", 99_999.0)) == 600.0


def test_backoff_grows_exponentially_from_the_poll_interval():
    p = _poller(FakeClient(), interval=900)
    exc = MspRateLimited("429")
    for strikes, expected in ((1, 900), (2, 1800)):
        p._strikes = strikes
        delay = p.backoff_delay(exc)
        assert JITTER[0] * expected <= delay <= JITTER[1] * expected


def test_backoff_is_capped():
    p = _poller(FakeClient(), interval=900, max_backoff=3600.0)
    p._strikes = 20
    assert p.backoff_delay(MspRateLimited("429")) == 3600.0


def test_backoff_is_jittered():
    """Identical proxies must not retry in lockstep and re-trip the quota."""
    p = _poller(FakeClient(), interval=900)
    p._strikes = 1
    delays = {p.backoff_delay(MspRateLimited("429")) for _ in range(25)}
    assert len(delays) > 1


def test_entering_backoff_sets_the_deadline_and_reports_it():
    client = FakeClient()
    p = _poller(client)
    delay = p._enter_backoff(MspRateLimited("429", 120.0))
    assert delay == 120.0
    assert p.in_backoff
    assert p._backoff_until > time.monotonic()


def test_in_backoff_is_false_once_the_deadline_passes():
    p = _poller(FakeClient())
    p._backoff_until = time.monotonic() - 1
    assert not p.in_backoff


# -- poller backoff behaviour in the loop --------------------------------
def test_poll_once_propagates_a_rate_limit():
    """Contrast with an ordinary error, which poll_once degrades locally."""
    p = _poller(FakeClient(rate_limited=True, retry_after=60))
    with pytest.raises(MspRateLimited):
        p.poll_once()


def test_poll_once_degrades_an_ordinary_topology_error():
    class Ordinary(FakeClient):
        def find_switches(self, gid):
            raise MspError("transient")

    p = _poller(Ordinary())
    assert p.poll_once() is False  # no exception


def test_no_requests_are_issued_while_backed_off():
    """The whole point: stop calling, rather than eat another 429 each cycle."""
    client = FakeClient(rate_limited=True, retry_after=3600)
    p = _poller(client, interval=900)
    p._backoff_until = time.monotonic() + 100

    async def go():
        task = asyncio.create_task(p.run())
        await asyncio.sleep(0.2)
        p.stop()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())
    assert client.calls == 0


def test_strikes_reset_after_a_successful_poll():
    client = FakeClient()
    p = _poller(client)
    p._strikes = 4

    async def go():
        task = asyncio.create_task(p.run())
        await asyncio.sleep(0.2)
        p.stop()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())
    assert p._strikes == 0
    assert client.calls > 0


def test_consecutive_rate_limits_escalate_the_deadline():
    client = FakeClient(rate_limited=True)
    p = _poller(client, interval=10, max_backoff=3600.0)
    first = p._enter_backoff(MspRateLimited("429"))
    second = p._enter_backoff(MspRateLimited("429"))
    third = p._enter_backoff(MspRateLimited("429"))
    assert p._strikes == 3
    assert first < second < third


# -- startup backoff ------------------------------------------------------
def test_rate_limit_delay_is_shared_by_startup_and_poll_loop():
    """One implementation, so the two paths cannot drift apart."""
    from firewalla_snmp_proxy.poller import rate_limit_delay

    exc = MspRateLimited("429", 45.0)
    p = _poller(FakeClient(), interval=900, max_backoff=3600.0)
    assert p.backoff_delay(exc) == rate_limit_delay(exc, 900, 0, 3600.0) == 45.0


def test_startup_waits_out_a_rate_limit_instead_of_exiting(monkeypatch):
    """The crash-loop guard.

    Exiting on a 429 at startup would let systemd's Restart=on-failure spend
    several API calls every RestartSec, pinning the quota indefinitely. Startup
    must instead stay in-process and retry slowly.
    """
    import firewalla_snmp_proxy.cli as cli
    from firewalla_snmp_proxy.config import Config, SwitchConfig

    cfg = Config(
        domain="dn-test.firewalla.net", token="tok",
        poll_interval=900, max_backoff_seconds=3600,
        switches=[SwitchConfig(mac="AA:BB:CC:DD:EE:FF", port=16100)],
    )

    attempts = {"n": 0}

    def flaky_gid(client, c):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise MspRateLimited("HTTP 429", 0.01)
        return "gid-1"

    monkeypatch.setattr(cli, "_resolve_gid", flaky_gid)
    monkeypatch.setattr(cli, "_fetch_payload", lambda c, cl, g: {"AA": {}})
    monkeypatch.setattr(
        cli, "_build_agents",
        lambda c, p, **kw: {"AA": object()},
    )

    # No snapshot passed: with no cache to fall back on, startup must retry.
    gid, agents = asyncio.run(cli._startup(cfg, FakeClient()))
    assert gid == "gid-1"
    assert attempts["n"] == 3  # retried rather than raising
    assert agents


def test_startup_propagates_non_rate_limit_errors(monkeypatch):
    """A bad token or bad config must still fail loudly and immediately."""
    import firewalla_snmp_proxy.cli as cli
    from firewalla_snmp_proxy.config import Config

    cfg = Config(domain="d", token="t", poll_interval=900)

    def broken(client, c):
        raise MspError("token rejected")

    monkeypatch.setattr(cli, "_resolve_gid", broken)
    with pytest.raises(MspError):
        asyncio.run(cli._startup(cfg, FakeClient()))
