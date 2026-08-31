"""Tests for the systemd unit installer.

The point of this module is that `pipx install` and a repo clone reach the same
end state, so these tests pin the parts that would let them drift.
"""

import os

import pytest

from firewalla_snmp_proxy import service


def test_binary_under_home_is_refused():
    """ProtectHome=yes would make this fail with 203/EXEC at runtime."""
    with pytest.raises(service.ServiceError) as exc:
        service._check_exec_path("/home/someone/.local/bin/firewalla-snmp-proxy")
    msg = str(exc.value)
    assert "ProtectHome" in msg
    assert "pipx --global install" in msg, "the error must name the fix"


def test_binary_under_root_home_is_refused():
    with pytest.raises(service.ServiceError):
        service._check_exec_path("/root/.local/bin/firewalla-snmp-proxy")


def test_system_paths_are_accepted():
    for path in ("/usr/local/bin/firewalla-snmp-proxy", "/usr/bin/firewalla-snmp-proxy"):
        assert service._check_exec_path(path) == path


def test_unit_template_renders_a_valid_looking_unit():
    unit = service.UNIT_TEMPLATE.format(
        name="firewalla-snmp-proxy", when="2026-01-01T00:00:00", user="svcuser",
        env_file="/etc/x/env", binary="/usr/local/bin/x",
        config_file="/etc/x/config.yaml", state_dir="/var/lib/x",
    )
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    # Survives a reboot, restarts on crash, and can be stopped by hand.
    assert "WantedBy=multi-user.target" in unit
    assert "Restart=on-failure" in unit
    assert "ExecStart=/usr/local/bin/x run --config /etc/x/config.yaml" in unit
    assert "User=svcuser" in unit
    # The sandbox must keep the one writable path, or counters cannot persist.
    assert "ReadWritePaths=/var/lib/x" in unit
    assert "ProtectSystem=strict" in unit


def test_find_mib_locates_it_from_the_repo_layout():
    """Wheel and checkout both put mibs/ one level above the package."""
    found = service.find_mib()
    assert found is not None
    assert found.endswith(service.MIB_NAME)
    assert os.path.isfile(found)


def test_config_is_ready_requires_both_config_and_a_nonempty_token(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    env = tmp_path / "env"
    monkeypatch.setattr(service, "CONFIG_FILE", str(cfg))
    monkeypatch.setattr(service, "ENV_FILE", str(env))

    assert service.config_is_ready() is False          # neither exists

    cfg.write_text("msp:\n  domain: x\n")
    assert service.config_is_ready() is False          # no env file

    env.write_text("FIREWALLA_MSP_TOKEN=\n")
    assert service.config_is_ready() is False          # token is blank

    env.write_text("FIREWALLA_MSP_TOKEN=abc123\n")
    assert service.config_is_ready() is True

    cfg.unlink()
    assert service.config_is_ready() is False          # token but no config


def test_install_and_uninstall_require_root(monkeypatch):
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000)
    with pytest.raises(service.ServiceError, match="must run as root"):
        service.install()
    with pytest.raises(service.ServiceError, match="must run as root"):
        service.uninstall()


def test_locate_binary_reports_how_to_install_when_absent(monkeypatch):
    monkeypatch.setattr(service.os, "access", lambda p, m: False)
    monkeypatch.setattr(service.shutil, "which", lambda n: None)
    with pytest.raises(service.ServiceError, match="pipx --global install"):
        service.locate_binary()


def test_locate_binary_prefers_usr_local_bin(monkeypatch):
    monkeypatch.setattr(
        service.os, "access",
        lambda p, m: p == "/usr/local/bin/firewalla-snmp-proxy",
    )
    monkeypatch.setattr(service.shutil, "which",
                        lambda n: "/home/u/.local/bin/firewalla-snmp-proxy")
    assert service.locate_binary() == "/usr/local/bin/firewalla-snmp-proxy"
