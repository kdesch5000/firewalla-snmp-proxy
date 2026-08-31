# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Changed

Documentation only. The install docs had grown by accretion — three install
variants scattered across two sections, service management as a single
sentence, and no answer at all to "how do I know there is a newer version?"

- **`## Install` is now one section with two clearly-labelled options**, PyPI
  and clone, stating up front that both end in the same place and that a clone
  is not needed to run the proxy or install the service. Why `--global`
  matters (`ProtectHome=yes`) is explained where you choose, not later.
- **Quick start is explicitly the foreground, try-it-out path**, and says so,
  ending with the fact that it dies with your shell.
- **`### Managing it`** — status, stop, start, restart, enable, disable and
  both journalctl forms, plus the fact that there is no reload signal and the
  warning not to restart during a rate-limit lockout.
- **`### Upgrading`** — how to check whether a newer version exists (PyPI JSON,
  `pip index versions`, GitHub release watching), how to apply it, how to
  confirm what is running via `sysDescr`, and the PyPI CDN lag that can quietly
  install the previous version right after a release.
- `## Uninstall` promoted to a top-level section covering the service, the
  `--purge` case, the never-installed-a-service case, and the reminder that
  the MSP token stays valid after the software is gone.

## [2.3.0] - 2026-08-31

Both ways of installing this now reach the same end state: a systemd unit that
is enabled at boot, restarts on failure, and can be started and stopped by hand.

**Why:** `pipx install` put a CLI on your PATH and nothing else. Becoming a
service required cloning the repo for a shell script, so the packaged install
and the cloned install produced different results — and, being two separate
implementations, were free to drift apart. 2.2.3 had just fixed a case where
they had.

### Added

- **`firewalla-snmp-proxy install-service`** and **`uninstall-service`**. No
  clone needed. `install-service` creates the system user, the config and state
  directories, a 0640 root-owned `EnvironmentFile` for the token, copies the
  vendor MIB to `/usr/share/snmp/mibs`, writes the unit, and runs
  `systemctl enable` so it survives a reboot. It starts the service when a
  config and a non-empty token are already present, and otherwise prints what
  is missing rather than leaving a unit that fails on start. `--no-start`,
  `--user` and `--binary` are available; `uninstall-service --purge` also
  removes `/etc/firewalla-snmp-proxy` and `/var/lib/firewalla-snmp-proxy`,
  which it keeps by default because one holds your token and the other holds
  the counter offsets.
- 9 tests covering the ProtectHome refusal, binary search precedence, unit
  rendering, MIB discovery under both layouts, and the readiness check that
  decides whether to start (221 total).

### Changed

- **`install.sh` is now a shim** over `install-service`, down from ~240 lines
  to 66. The documented `sudo ./install.sh --service` and `--uninstall` still
  work; there is simply no second implementation behind them any more.
- README gains an **Uninstall** section covering both the service and the
  shell-only case, including the point that your MSP token stays valid after
  the software is gone. The service section now states up front that the binary
  must be system-wide and why.

## [2.2.3] - 2026-08-31

### Fixed

- **`install.sh` could generate a unit that cannot start.** Its binary search
  included `/root/.local/bin` and `$HOME/.local/bin`, but the unit it writes
  sets `ProtectHome=yes`, which makes `/home`, `/root` and `/run/user`
  inaccessible to the service. A per-user `pipx install` therefore produced an
  `ExecStart` that failed with status 203/EXEC — while the same binary ran fine
  by hand, which makes it a genuinely confusing failure.

  `install.sh` now checks `/usr/local/bin` first and refuses outright if the
  binary it found sits under `/home` or `/root`, naming the fix
  (`sudo pipx --global install`) instead of writing a broken unit.

### Changed

- README's service section now leads with why you want a service at all, and
  states that the binary must be installed system-wide *before* running
  `install.sh`. The quick start says explicitly that `run` is a foreground
  command and links to the service section. Following the quick start and then
  the service section in order previously produced the 203/EXEC case above.

## [2.2.2] - 2026-08-31

### Fixed

- **`init` hardcoded `/var/lib/firewalla-snmp-proxy` into the generated
  config**, so the documented quick start — `pipx install`, then run as
  yourself — produced `Permission denied` on every poll and silently discarded
  both the counter offsets and the topology cache. The proxy looked like it was
  working while quietly losing the state that keeps counters monotonic across a
  restart and lets startup survive an API outage. Reported from a fresh install
  on a second Raspberry Pi.

  State paths are now resolved at run time: `/var/lib/firewalla-snmp-proxy`
  when it is writable or when running as root (so a deployed service is
  unchanged), otherwise `$XDG_STATE_HOME/firewalla-snmp-proxy`, normally
  `~/.local/state/firewalla-snmp-proxy`. `init` writes whichever applies, and
  keeps the cache beside the counters so `--state-file` moves both.
- **Persistence failures now warn once instead of on every poll.** An
  unwritable state directory is a static condition — wrong owner, read-only
  mount — so the old behaviour was an unbounded log of the same line.

### Changed

- README install instructions now use the PyPI package (`pipx install
  firewalla-snmp-proxy`) rather than a git URL, with a separate note on when
  cloning is still worth it (`install.sh`, and the MIB as a loose file). Adds a
  PyPI version badge.

### Added

- **Nagios recipe** in the monitoring-system section, marked untested. Unlike
  the other systems listed, Nagios alerts on thresholds rather than
  autodiscovering, so the recipe leads with the proxy's own health OIDs —
  `fwProxyPollStatus`, `fwProxySecondsSincePoll` and `fwProxyIcmpStatus` — as
  `check_snmp` invocations, since a frozen counter reads as a legitimate zero
  and traffic-based alerting cannot catch it. Per-port link state and pointers
  to `check_snmp_int.pl` and the Nagios XI SNMP wizard follow, plus a note to
  set the check interval against `poll_interval` rather than expecting
  minute-resolution data.

## [2.2.1] - 2026-08-31

Mostly project infrastructure and documentation, plus one Python 3.9 fix that
the new CI surfaced immediately. First release published to PyPI.

### Fixed

- **`Poller` could only be constructed from inside a running event loop.**
  `__init__` built `asyncio.Event()` eagerly, and on Python 3.9 that binds to
  the current loop at construction and raises `RuntimeError: There is no
  current event loop` when there isn't one. Production was unaffected — the
  poller is built inside `asyncio.run` — but the constraint was undocumented,
  invisible, and broke the test suite on the oldest supported interpreter.

  Stop state is now a plain flag plus a lazily-created Event. Beyond fixing
  3.9, this makes `stop()` safe to call *before* `run()` starts, which the
  Event alone could not express: a stop requested that early used to be lost.
  Three regression tests cover construction outside a loop, stop-before-run,
  and stop-during-run.

### Added

- **CI** (`.github/workflows/ci.yml`) — pytest on Python 3.9 through 3.13, plus
  a build job that runs `twine check` and asserts the vendor MIB is present in
  *both* the sdist and the wheel. The 3.9 job exists to keep
  `requires-python = ">=3.9"` honest rather than aspirational.
- **Release workflow** (`.github/workflows/release.yml`) — publishes to PyPI on
  a `v*` tag via Trusted Publishing (OIDC), so no API token is stored in the
  repository. Refuses to publish when the tag and the packaged version disagree.
- `SECURITY.md` — private reporting channel, and an explicit account of what
  the software has access to: an MSP token that can read the whole network
  inventory, and an SNMP listener speaking a cleartext protocol.
- `CONTRIBUTING.md` — what to include in a bug report, how to sanitize it, and
  how to contribute a fixture for a switch model other than the SE.
- Issue templates for bugs, features and switch-model reports, plus a contact
  link routing security reports to a private advisory.

### Security

- **Only a tag push can publish to PyPI.** The release workflow's
  `workflow_dispatch` trigger reaches the build job (a useful packaging dry
  run) but the publish job is gated on `refs/tags/`. A dispatch carries no tag,
  so the version-parity check cannot run against it — and a PyPI upload is
  irreversible, since a version number burned by mistake can never be reused.

### Changed

- **README documents what is actually known about the MSP API quota.** New
  "What is actually known about the quota" subsection: the limit is
  undocumented and evolving; ~4,300 calls/day tripped it; the lockout ran 6+
  hours against a `Retry-After: 3600`; and — measured 2026-08-31 — successful
  responses carry **no** `x-ratelimit-*` headers of any kind, so a client
  cannot read its remaining headroom. Also records the inference from the
  `x-amzn-*` / CloudFront headers that the API is fronted by AWS API Gateway,
  whose usage-plan quotas are DAY/WEEK/MONTH only — which explains why a canned
  hourly `Retry-After` coexists with a multi-hour lockout.
- README Requirements now says **MSP Lite is free for a single box**. The
  previous wording read as though a paid subscription were required, which is
  the most likely reason for someone to stop reading.
- README gains CI/Python/licence badges and a Contributing section.

## [2.2.0] - 2026-08-28

The API being unavailable no longer takes the device down.

**Why:** 2.1.0 stopped the proxy crash-looping through a rate limit, but it
still could not *bind* during one — the SNMP sockets are built from the port
layout, which only a live `/topology` call supplied. So a restart mid-lockout
left the proxy up but silent, and Observium (whose device hostname for this
proxy resolves to loopback, making SNMP response the sole up/down signal) marked
the device **down** for the length of the lockout.

### Added

- **Topology cache** — new `firewalla_snmp_proxy.snapshot` module. Every
  successful poll persists the merged per-switch API payload to
  `topology_cache` (default `/var/lib/firewalla-snmp-proxy/topology.json`,
  atomic, 0600). When the API is unavailable at startup, agents are rebuilt
  from it and begin serving immediately, publishing the **full** port set.

  Serving a truncated `ifTable` instead was rejected: Observium marks ports
  absent from `ifTable` as deleted, which would discard the port history the
  cache exists to protect. Either every port is served or none is — hence the
  remaining retry path when no cache exists yet (a first-ever run genuinely
  does not know the layout).

  Cached agents report honestly: `last_poll_ok` is the cache's own write time,
  not now, so `fwProxySecondsSincePoll` and `fwProxyPollStatus` show the real
  age. `poll_count` is 0 and `sysUpTime` is frozen at the cached value rather
  than advertising uptime nobody observed.

- **ICMP reachability** — new `firewalla_snmp_proxy.reachability` module and a
  `ping_host` option. Pings the real switch on its own cadence
  (`ping_interval`, default 60s), entirely independently of the poll loop,
  because it costs no API quota and during a lockout it is the *only* live
  signal about the switch.

  Reachability is **tri-state**. A check that could not be *carried out* —
  unresolvable name, no ICMP permission — is `unknown`, deliberately not
  folded into `down`: doing so would report a fake outage every time the host
  rebooted before its resolver was ready. Unknown moves no debounce counters.
  Transitions are debounced (`ping_fail_threshold` 3, `ping_recover_threshold`
  1 — asymmetric because coming back is unambiguous and going away is not).

  Needs no elevated privileges: an unprivileged ICMP datagram socket
  (`SOCK_DGRAM`/`IPPROTO_ICMP`) where `net.ipv4.ping_group_range` permits it,
  falling back to the `ping` binary. Both paths verified working inside this
  project's systemd sandbox (`NoNewPrivileges`, `PrivateDevices`,
  `CapabilityBoundingSet=`, `RestrictAddressFamilies=AF_INET AF_INET6`).

- **Three new vendor OIDs**, with MIB definitions:
  - `fwProxyIcmpStatus` (`.1.7`) — `up(1)`/`down(2)`/`unknown(3)`/`disabled(4)`
  - `fwProxyIcmpRtt` (`.1.8`) — microseconds, so sub-millisecond LAN
    round-trips are not truncated to zero
  - `fwProxyServingCache` (`.1.9`) — true(1) while serving cached data.
    Without this, frozen counters are indistinguishable from an idle switch.

- **`dark_probe_max_seconds`** (default 300) — backoff cap while there is no
  cache to serve, i.e. while the proxy is not listening at all and the NMS sees
  the device as down. The API's `Retry-After` can be a fixed hint rather than a
  real quota-reset time, so honouring `3600` literally in that state can mean
  sitting dark for an hour after the quota already cleared. Once a cache exists
  this branch is unreachable and backoff is fully polite again.
- 39 new tests (205 total) covering debounce semantics, the unknown-is-not-down
  rule, native/CLI ping dispatch, cache round-trip and corruption handling,
  full-port-set publication from cache, and the status/online precedence rules.

### Changed

- **`fwSwitchOnline` now prefers ICMP over the API's `online` field.** That
  field is only as fresh as the last successful poll, so during a lockout it can
  assert a switch is up hours after it stopped being so. ICMP wins when it has a
  confirmed verdict; the API value stands when ICMP is disabled or not yet
  debounced. MIB description updated accordingly.
- **`fwProxyPollStatus` is now reachability-aware**: stale data plus a
  pingable switch is `stale(2)` (degraded), while stale data plus no ICMP reply
  is `error(3)` (the switch may be gone). Collapsing both to one value wasted
  the only live signal available.
- `_build_contexts` split into `_fetch_payload` (API) and `_build_agents`
  (construction), so a live fetch and a cache load are interchangeable inputs.
  That equivalence is what makes API-free startup possible.
- Startup now falls back to cache for *any* `MspError`, not just rate limits —
  a connection failure deserves the same treatment. Genuine configuration
  errors (bad token, switch absent) still fail immediately when there is no
  cache to serve.

### Deliberately unchanged

- **Per-port `ifOperStatus` is left at its last-known value while serving
  cache.** ICMP to the switch says nothing about individual port link state, so
  both alternatives are worse: reporting `down(2)` invents an outage, and
  `unknown(4)` would churn port-state history in the NMS. The switch-level
  signals (`fwSwitchOnline`, `fwProxyIcmpStatus`, `fwProxyServingCache`) carry
  the truth instead.
- **Recovery from a long lockout produces one overstated sample.** When the gap
  exceeds `max_ramp_seconds` the ramp is skipped, so the accumulated delta lands
  in a single poll. The cap is deliberate — ramping an hour of old traffic over
  the following hour would mask what the switch is doing *now* — and RRA
  consolidation restores the correct long-run average either way.

## [2.1.0] - 2026-08-28

Survives the MSP API's request quota, and stops a slow poll interval from
producing garbage rates at a faster-polling NMS.

**Why:** a 429 lockout silently flatlined a port's graph for nearly three hours.
The proxy kept answering SNMP and the counters simply stopped advancing, so every
rate Observium derived was a legitimate-looking zero — nothing appeared broken.
The 60s poll interval had been spending ~4,300 API calls a day to buy a few
minutes of freshness behind a cloud that only refreshes data every ~5 minutes.

### Added

- **Exponential backoff on HTTP 429/503** (`poller.py`). On a rate-limit
  response the poller stops issuing requests entirely until the backoff
  expires; retrying through a 429 keeps the quota pinned and can extend the
  lockout. Backoff starts at `poll_interval`, doubles per consecutive strike,
  is jittered by ±20% (so proxies sharing a token don't retry in lockstep and
  re-trip the quota together), and is capped by the new `max_backoff_seconds`
  (default 3600). The first successful cycle resets the schedule, so recovery
  needs no operator.
- **`MspRateLimited` exception** with `Retry-After` parsing (`msp_api.py`),
  accepting both delta-seconds and HTTP-date forms per RFC 9110. The server's
  own guidance always wins over the computed delay. Raised for 503 as well as
  429, since both mean "stop calling".
- **Counter ramping** — new `firewalla_snmp_proxy.ramp` module, enabled by
  `counter_smoothing: ramp` (the default). Spreads each newly-learned increment
  evenly across the window following the poll that revealed it, so an NMS
  polling faster than `poll_interval` derives a steady, correct rate instead of
  a 0/0/3x sawtooth. Byte totals stay exact, output stays monotonic, and the
  ramp window is *measured* rather than configured — so it self-adapts when a
  backoff stretches the real interval. Costs one interval of lag and flattens
  sub-window bursts; the API does not carry within-window traffic shape, so
  that detail was never available to publish. `counter_smoothing: raw` restores
  the previous behaviour.
- **Startup backoff** (`cli.py:_startup`). Startup resolves the box and builds
  agents *before* the poller exists, so it needs its own backoff. Exiting on a
  429 there would be actively harmful: `Restart=on-failure` with a 15s
  `RestartSec` would crash-loop against the rate-limited API, spending several
  calls every 15 seconds and pinning the quota indefinitely. Startup now
  retries in-process on the shared backoff schedule and never exits for a rate
  limit. Non-rate-limit errors (bad token, switch absent from topology) still
  fail immediately and loudly.
- **`max_ramp_seconds`** — observed-window ceiling past which ramping is
  skipped for that sample (0 derives it as 2.5x `poll_interval`). Stops a long
  outage's accumulated bytes from being smeared across an equally long ramp,
  which would hide the recovery.
- 46 new tests (166 total), covering the interpolation math, monotonicity
  across irregular windows, 64-bit precision, `Retry-After` parsing, the
  backoff schedule and cap, and an end-to-end simulation of a 5-minute NMS
  against a 15-minute proxy interval asserting steady rates and exact
  conservation of octets.

### Changed

- **`poll_interval` default is now 900 (15 minutes)**, up from 60. The old
  default optimised for latency behind a data source that only updates every
  ~5 minutes, at the cost of quota headroom. Counter ramping is what makes an
  interval this long still produce usable graphs. Existing configs are
  unaffected — this only changes the default for new ones.
- `MspRateLimited` now propagates out of `poll_once()` from anywhere in the
  cycle, including the optional network-name and per-switch-detail calls.
  Previously `network_names()` swallowed all `MspError`s, which hid a 429 from
  the backoff logic entirely. All other errors are still degraded locally so
  one bad sub-request cannot cost a whole cycle.
- Startup log line and `check` output now report the smoothing mode and backoff
  cap.

### Fixed

- A rate limit hitting `switch_settings()` escaped `poll_once()` as an
  unhandled exception and was logged with a full traceback by the generic
  handler in `run()`, once per cycle, indefinitely. It is now a recognised
  state with a one-line warning naming the resume time.

### Known limitation

- **Restarting the proxy while rate limited leaves it not listening.** The SNMP
  sockets are bound by `_build_contexts`, which needs the topology to know how
  many ports to serve, so during a startup backoff there is no listener and the
  NMS sees the device as *down* rather than as *stale*. This is still an
  improvement on 2.0.0, which crash-looped in the same situation (also not
  listening, and hammering the API), but it is not the right end state.

  Serving a truncated `ifTable` instead was considered and rejected: Observium
  marks ports absent from `ifTable` as deleted, which would discard the very
  port history this release exists to protect. The correct fix is to cache the
  last-known-good topology and rebuild agents from it during a lockout, serving
  the full port set with `fwProxyPollStatus = error(3)`. Not implemented yet.

### Operational notes

- **Alert on `fwProxyLastError` and `fwProxySecondsSincePoll`, not on traffic
  rates.** A rate of zero cannot distinguish a rate-limited proxy from a quiet
  switch. During backoff `fwProxyLastError` says so explicitly and names the
  resume time.
- `fwProxyPollStatus` already derives its stale threshold from `poll_interval`
  (`max(300, 3x interval)`), so a 900s interval does not make it read stale.

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
