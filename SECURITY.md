# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/kdesch5000/firewalla-snmp-proxy/security/advisories/new)
rather than opening a public issue. I'll acknowledge within a few days.

## What this software has access to

This matters more than usual here, so it is worth being explicit.

**A Firewalla MSP API token.** The proxy authenticates to the MSP cloud with a
personal access token that can read your entire network inventory — every
device, its MAC and IP, traffic totals, alarms and rules. Treat the token as a
credential of the same weight as a router password.

- The token is read from the `FIREWALLA_MSP_TOKEN` environment variable, or
  from `msp.token` in the config file. Prefer the environment variable, and
  prefer systemd's `EnvironmentFile=` (mode `0640`, owned `root:<service
  group>`) over putting it in the config, so it does not land in a file you
  might later paste into a bug report.
- `config.yaml`, `config.local.yaml`, `token` and `counters.json` are
  gitignored. If you fork this repo, keep those entries.
- The token is never logged. Bug reports should still be skimmed before
  posting — `--debug` output includes request URLs.

**An SNMP listener.** The proxy binds one UDP port per switch, defaulting to
`127.0.0.1`. SNMP v1/v2c has no encryption and the community string is
effectively a cleartext password, so **do not bind it to a public interface**.
If your NMS is on another host, prefer an SSH tunnel or a firewalled management
VLAN over exposing the port.

## Read-only by design

The MSP client refuses mutating HTTP verbs at the client layer, not merely by
convention — there is no code path that can `POST`, `PUT`, `PATCH` or `DELETE`
against the Firewalla API. The proxy also implements no SNMP `SET`. Nothing is
installed on the Firewalla itself. See the "Safety" section of the README.

## Supported versions

Fixes land on the latest minor release. Given the size of this project, older
versions are not backported.
