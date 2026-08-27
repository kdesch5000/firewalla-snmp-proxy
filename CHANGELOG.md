# Changelog

All notable changes to this project are documented here.

## [2.0.0] - 2026-08-27

First public release. Republishes Firewalla Switch data from the MSP cloud API
as a standards-compliant SNMP agent.

Numbering starts at 2.0.0 rather than 1.0.0 because this is a ground-up rewrite
that replaces an earlier, unreleased `snmpsim`-based proxy. That first-generation
tool served a static recording of 234 OIDs whose traffic counters were flat zero;
this one is a native pysnmp agent serving 755 OIDs with live counters. Nothing is
shared between the two codebases, and there is no upgrade path from the old one
beyond pointing your NMS at the new agent — see *Migrating from an snmpsim-based
proxy* below.

### Added

**SNMP agent**
- Native pysnmp-based agent, one UDP port per switch (default base 16100).
  A port per switch rather than one port with multiple communities, because
  some monitoring systems identify a device by `IP:port` and would merge them.
- SNMP v1 and v2c, GET / GETNEXT / GETBULK. SET is answered with an explicit
  `notWritable` rather than dropped, so a manager doesn't read a refusal as a
  dead agent.
- `noSuchInstance` vs `noSuchObject` distinguished correctly on failed GETs.
- OID tree rebuilt only when the port layout changes; values are live via
  callables, so an NMS never sees rows flicker mid-walk.

**MIB coverage** — standard MIBs first, so any NMS graphs the data with no
custom configuration:
- **IF-MIB**: full `ifTable` and `ifXTable`, including 64-bit `ifHC*` octet and
  packet counters, error and discard counters, `ifOperStatus`, `ifSpeed` /
  `ifHighSpeed` (saturating at Gauge32 max per RFC 2863), and `ifAlias` carrying
  the uplink target or `Access: <VLAN name>`.
- **`ifCounterDiscontinuityTime`** mapped from Firewalla's `statsSinceTs` — an
  exact semantic match to RFC 2863.
- **POWER-ETHERNET-MIB**: `pethPsePortDetectionStatus`, `pethPsePortType`, and
  `pethMainPseConsumptionPower`.
- **BRIDGE-MIB**: `dot1dStpPortState` (RSTP `discarding` → legacy
  `blocking(2)`), base bridge address and port table. The `dot1dStp` group is
  omitted entirely when STP is disabled on the switch.
- **ENTITY-MIB**: chassis with serial, hardware and firmware revisions, plus
  per-port entities and SFP transceiver modules marked field-replaceable.
- **ENTITY-SENSOR-MIB**: instantiated only when a positive temperature is
  reported. Omitted on the fanless Switch SE, which reports `temperature: 0`.
- **LLDP-MIB**: the switch's uplink published as an `lldpRemTable` neighbour, so
  Observium / LibreNMS / Zabbix draw the switch-to-Firewalla topology link
  automatically.
- **SNMPv2-MIB**: system group with a vendor-first `sysDescr` and a `sysUpTime`
  that ticks between polls rather than freezing.

**Vendor MIB** (`mibs/FIREWALLA-SNMP-PROXY-MIB.txt`) for the four things with no
standard home: per-port PoE wattage (RFC 3621 has no per-port power object),
STP port role, SFP transceiver detail, and ACL usage. Plus a proxy health group
(`fwProxyPollStatus`, `fwProxySecondsSincePoll`, `fwProxyApiLatency`,
`fwProxyLastError`) so an operator can tell an idle switch from a proxy that has
lost API access and is serving frozen counters.

**Counter integrity**
- Persistent monotonic offsets, so counters never regress across Firewalla
  counter resets, proxy restarts or switch reboots.
- Resets detected from *both* an advancing `statsSinceTs` and a decreasing raw
  value. Neither alone suffices: with enough traffic between polls a reset
  counter can already be above its previous reading, and only the timestamp
  reveals it.
- State written atomically (temp file + `os.replace`); a corrupt state file is
  ignored rather than fatal.

**MSP API client**
- Read-only by construction: issues only GETs, and refuses client-side any URL
  containing a state-changing segment (`reboot`, `upgrade`, `reset-stats`,
  `power-cycle`, `restart`, `switch-branch`, `check-status`, `force-sync`,
  `detach`, `attach`, `delete`).
- Validates response content type. On this API an unknown path returns HTTP 200
  with the web console's HTML, so a status-code check alone cannot detect a
  missing endpoint.
- Stdlib HTTP only, keeping the dependency tree to pysnmp and PyYAML.
- Switch discovery keys on the topology node's `type`, not the device record's
  `deviceType`, which changed three times in four weeks
  (`firewalla` → `switch` → `fwsw-B`).

**CLI and install**
- `init` discovers every switch on the account and writes a ready-to-use config
  with consecutive ports assigned.
- `check` validates config, API access, OID ordering and that every object
  renders, without binding sockets.
- `walk` dumps the whole tree as text for inspection or diffing.
- `run` is the systemd entry point.
- `install.sh --service` generates the systemd unit from the detected binary
  path, service user and config location — nothing to hand-edit. Creates a
  dedicated system user, a 0640 root-owned `EnvironmentFile` for the token, and
  a state directory. Hardened unit (`ProtectSystem=strict`, empty capability
  set, single writable path).
- API token never accepted as a command-line argument; resolved from
  `FIREWALLA_MSP_TOKEN`, then `token_file`, then the config file, with a warning
  when a file holding it is group- or world-readable.

**Tests** — 120 tests, no network or Firewalla account required.
- Walk ordering tested directly, including that port `2` sorts before `10`
  (a lexical sort would silently scramble every `ifIndex`).
- End-to-end tests bind a loopback UDP port and drive the agent with a real
  SNMP client, exercising BER encoding, community auth and GETNEXT walks.
- A regression test that the LLDP subtree at `1.0.8802` is reachable: scoping
  the agent's access-control view to `1.3.6.1` — the obvious choice — silently
  hides the whole neighbour table and with it the NMS topology link.

### Migrating from an snmpsim-based proxy

If you previously faked this switch with an `snmpsim` recording, three things
will cost you history if you miss them:

- **Pin `sys_object_id` to whatever your old agent emitted** (for snmpsim and
  net-snmp defaults, `1.3.6.1.4.1.8072.3.2.10.99.2`). Your NMS keys its OS
  detection on `sysObjectID`; change it and the device is re-detected as
  generic, losing its icon, any custom OS definition, and its port graphs. The
  `sys_object_id` config option exists for exactly this.
- **Keep the same `ifIndex` numbering.** Ports are matched on `ifIndex`, so a
  matching layout carries RRD history across with zero orphaned ports.
- **Disable the old agent's watchdog, not just its service.** An snmpsim
  deployment usually has a selfcheck timer that restarts it on a few minutes'
  cadence; leave that enabled and it will silently take the UDP port back from
  this proxy. Also drop any ping-monitor helper that injected reachability into
  `sysDescr` — this proxy takes liveness from the API's `online` field.

### Known limitations
- **Port counters are refreshed by the Firewalla cloud only every ~5 minutes**
  (measured: changes at 123s / 409s / 717s over a 13-minute sample, i.e. 286s
  and 308s apart). Effective graph resolution is therefore ~5 minutes no matter
  how fast either this proxy or your NMS polls, and sub-5-minute rate spikes are
  averaged away. Cumulative totals remain accurate. `/topology` and
  `/switches/<mac>` share one cache, so there is no fresher endpoint to use.
- Data comes from a cloud API, so state changes appear with tens of seconds of
  latency. Not suitable for fast link-flap detection.
- No SNMPv3. v1/v2c only.
- No traps or informs; `ifLinkUpDownTrapEnable` correctly reports `disabled(2)`
  since a polling proxy cannot observe a transition as it happens.
- The MSP switch endpoints are undocumented and may change without notice.
- The enterprise OID is a placeholder (`1.3.6.1.4.1.99999`) in unassigned IANA
  space, configurable via `enterprise_oid`.
