"""MSP API client: safety guards and error handling. No network access."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from firewalla_snmp_proxy.msp_api import (
    FORBIDDEN_SEGMENTS,
    MspAuthError,
    MspClient,
    MspError,
    MspNotJson,
)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def client():
    return MspClient("dn-example.firewalla.net", "test-token")


def patch_urlopen(monkeypatch, response):
    calls = []

    def fake(req, timeout=None, context=None):
        calls.append(req)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


# -- safety --------------------------------------------------------------
@pytest.mark.parametrize("segment", sorted(FORBIDDEN_SEGMENTS))
def test_every_mutating_segment_is_refused(client, segment, monkeypatch):
    """A monitoring tool must never be able to reach these.

    reset-stats would destroy counter history; reboot/power-cycle drop traffic.
    """
    calls = patch_urlopen(monkeypatch, FakeResponse(b"{}"))
    with pytest.raises(MspError, match="read-only"):
        client.get("v2/boxes/gid/switches/mac/%s" % segment)
    assert calls == [], "no HTTP request should have been attempted"


def test_safe_paths_are_allowed(client, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse(json.dumps({"nodes": []}).encode()))
    assert client.get("v2/boxes/gid/topology") == {"nodes": []}


def test_forbidden_check_is_segment_exact_not_substring(client, monkeypatch):
    """'rebooted' as a path segment is not 'reboot'; don't over-block."""
    patch_urlopen(monkeypatch, FakeResponse(b"{}"))
    client.get("v2/boxes/gid/rebooted")  # must not raise


# -- the SPA fallback trap ----------------------------------------------
def test_html_response_raises_even_with_http_200(client, monkeypatch):
    """Unknown paths on this API return HTTP 200 with the web app's HTML.

    Status code alone cannot detect a missing endpoint, so the content type is
    what the client trusts.
    """
    patch_urlopen(
        monkeypatch, FakeResponse(b"<!DOCTYPE html><html>", "text/html")
    )
    with pytest.raises(MspNotJson, match="does not exist"):
        client.get("v2/boxes/gid/topology")


def test_undecodable_json_raises(client, monkeypatch):
    patch_urlopen(monkeypatch, FakeResponse(b"{broken", "application/json"))
    with pytest.raises(MspNotJson):
        client.get("v2/boxes/gid/topology")


# -- errors --------------------------------------------------------------
@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_are_actionable(client, monkeypatch, code):
    patch_urlopen(
        monkeypatch,
        urllib.error.HTTPError("url", code, "denied", {}, None),
    )
    with pytest.raises(MspAuthError, match="Regenerate"):
        client.get("v2/boxes")


def test_other_http_errors_raise_msp_error(client, monkeypatch):
    patch_urlopen(
        monkeypatch, urllib.error.HTTPError("url", 500, "boom", {}, None)
    )
    with pytest.raises(MspError, match="HTTP 500"):
        client.get("v2/boxes")


def test_unreachable_host_raises_msp_error(client, monkeypatch):
    patch_urlopen(monkeypatch, urllib.error.URLError("no route"))
    with pytest.raises(MspError, match="cannot reach"):
        client.get("v2/boxes")


# -- parsing -------------------------------------------------------------
def test_find_switches_selects_on_topology_node_type(client, monkeypatch):
    """Selection uses the topology node's type, not deviceType.

    deviceType has churned three times in four weeks (firewalla -> switch ->
    fwsw-B) and is not a dependable key.
    """
    body = {
        "nodes": [
            {"type": "box", "id": "AA"},
            {"type": "switch", "id": "BB", "deviceType": "fwsw-B"},
            {"type": "switch", "id": "CC", "deviceType": "something-new"},
        ]
    }
    patch_urlopen(monkeypatch, FakeResponse(json.dumps(body).encode()))
    found = client.find_switches("gid")
    assert [s["id"] for s in found] == ["BB", "CC"]


def test_network_names_harvested_from_devices(client, monkeypatch):
    body = [
        {"network": {"id": "u1", "name": "LAN"}},
        {"network": {"id": "u2", "name": "IoT"}},
        {"network": {"id": "u1", "name": "LAN"}},
        {},
    ]
    patch_urlopen(monkeypatch, FakeResponse(json.dumps(body).encode()))
    assert client.network_names("gid") == {"u1": "LAN", "u2": "IoT"}


def test_switch_settings_absence_is_not_fatal(client, monkeypatch):
    """An optional endpoint going missing must not fail a poll."""
    patch_urlopen(monkeypatch, FakeResponse(b"<html>", "text/html"))
    assert client.switch_settings("gid") == {}


def test_domain_accepts_full_url():
    assert MspClient("https://dn-x.firewalla.net/", "t").domain == "dn-x.firewalla.net"


def test_missing_credentials_rejected():
    with pytest.raises(ValueError):
        MspClient("", "token")
    with pytest.raises(ValueError):
        MspClient("domain", "")
