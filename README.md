# firewalla-snmp-proxy

**Monitor your Firewalla Switch with any SNMP monitoring system.**

The Firewalla Switch SE has no SNMP agent, so tools like Observium, LibreNMS,
Zabbix, Checkmk, PRTG and Grafana can't see it. But the Firewalla MSP cloud API
*does* expose full per-port data. This proxy polls that API and republishes it
as a standards-compliant SNMP agent — one UDP port per switch — so your existing
monitoring system discovers it like any other managed switch.

You get real per-port traffic graphs, error counters, PoE draw, link speeds,
STP state, SFP transceiver detail, and a topology link to your Firewalla box.

```
┌──────────────┐   HTTPS    ┌──────────────────┐   SNMP    ┌─────────────┐
│ Firewalla    │ ─────────► │ firewalla-       │ ◄──────── │ Observium   │
│ MSP cloud    │  poll      │ snmp-proxy       │  v1/v2c   │ LibreNMS    │
│ API          │            │ (your Linux box) │           │ Zabbix, ... │
└──────────────┘            └──────────────────┘           └─────────────┘
```

Nothing is installed on the Firewalla itself, and the proxy is **strictly
read-only** — see [Safety](#safety).

---

## Requirements

- A Linux host with **Python 3.9+**, reachable by your monitoring system.
- A **Firewalla MSP** account. The per-port data comes from the MSP cloud API,
  so a standalone box with no MSP subscription has nothing to read.
- Only two Python dependencies (`pysnmp`, `PyYAML`) — both pulled in
  automatically below.

## Quick start

Not on PyPI yet, so install from a clone. Cloning is worth it anyway: you get
`install.sh` for the systemd setup and the vendor MIB to load into your NMS.

```bash
# 1. Install
git clone https://github.com/kdesch5000/firewalla-snmp-proxy
cd firewalla-snmp-proxy

sudo python3 -m venv /opt/firewalla-snmp-proxy-venv
sudo /opt/firewalla-snmp-proxy-venv/bin/pip install .
sudo ln -sf /opt/firewalla-snmp-proxy-venv/bin/firewalla-snmp-proxy /usr/local/bin/

firewalla-snmp-proxy --version

# 2. Get an API token from the Firewalla MSP web console
#    (account settings -> API tokens), then:
export FIREWALLA_MSP_TOKEN=<your token>

# 3. Discover your switches and write a config
firewalla-snmp-proxy init --domain dn-abc123.firewalla.net

# 4. Check it works
firewalla-snmp-proxy check -c config.yaml

# 5. Run it
firewalla-snmp-proxy run -c config.yaml
```

A venv rather than a bare `pip install` because current distros are
[PEP 668](https://peps.python.org/pep-0668/)-managed and will refuse to install
into system Python. If you would rather not manage one, `pipx` reads straight
from the repo:

```bash
sudo apt install pipx
sudo pipx --global install git+https://github.com/kdesch5000/firewalla-snmp-proxy
```

Step 4 binds no sockets, so it is safe to run while something else is already
listening on the port.

Then, from anywhere that can reach the proxy:

```bash
snmpwalk -v2c -c public 127.0.0.1:16100 1.3.6.1.2.1.2.2
```

`--domain` is the hostname of your MSP web console: if you reach MSP at
`https://dn-abc123.firewalla.net/`, use `dn-abc123.firewalla.net`.

If the walk times out, check `listen.address` — it defaults to `127.0.0.1`,
which accepts local traffic only. See
[Monitoring system recipes](#monitoring-system-recipes) if your NMS is on
another host.

### Install as a service

From the same clone:

```bash
sudo ./install.sh --service          # or: --service --user snmpproxy
```

The systemd unit is **generated** from the detected binary path, service user
and config location — there is nothing to hand-edit. It creates a locked-down
`firewalla-snmp-proxy` system user, a 0640 root-owned environment file for your
token, and a state directory for counter persistence:

| | |
|---|---|
| Config | `/etc/firewalla-snmp-proxy/config.yaml` |
| Token (`EnvironmentFile`) | `/etc/firewalla-snmp-proxy/env` |
| Counter state | `/var/lib/firewalla-snmp-proxy/counters.json` |

Put your token in the env file as `FIREWALLA_MSP_TOKEN=<token>`, then generate
the config at the system path and start it:

```bash
export FIREWALLA_MSP_TOKEN=<token>
sudo firewalla-snmp-proxy init --domain <your>.firewalla.net \
     -o /etc/firewalla-snmp-proxy/config.yaml --force
firewalla-snmp-proxy check -c /etc/firewalla-snmp-proxy/config.yaml
sudo systemctl enable --now firewalla-snmp-proxy
journalctl -u firewalla-snmp-proxy -f
```

`install.sh` prints these same steps when it finishes, and
`sudo ./install.sh --uninstall` reverses it.

The token is never accepted as a command-line argument, so it cannot leak into
your shell history or `ps` output. It is resolved from `FIREWALLA_MSP_TOKEN`,
then `token_file`, then the config file — with a warning if the file holding it
is group- or world-readable.

---

## What you get

Almost everything lands in a **standard MIB**, which is the whole point: your
monitoring system already knows how to graph an `ifEntry`, so per-port traffic
works with zero custom configuration.

| Firewalla data | Published as | MIB |
|---|---|---|
| `rxBytes` / `txBytes` | `ifHCInOctets` / `ifHCOutOctets` (64-bit) + 32-bit equivalents | IF-MIB |
| unicast / multicast / broadcast frames | `ifHCIn*Pkts` / `ifHCOut*Pkts` | IF-MIB |
| `rxErrorFrames` / `txErrorFrames` | `ifInErrors` / `ifOutErrors` | IF-MIB |
| `rxDiscardFrames` / `txDiscardFrames` | `ifInDiscards` / `ifOutDiscards` | IF-MIB |
| `linkUp` | `ifOperStatus` | IF-MIB |
| `linkSpeed` | `ifSpeed` + `ifHighSpeed` | IF-MIB |
| port role / VLAN name | `ifAlias` (e.g. `Access: IoTVLan`) | IF-MIB |
| `statsSinceTs` | `ifCounterDiscontinuityTime` | IF-MIB |
| `poeStatus` | `pethPsePortDetectionStatus` | POWER-ETHERNET-MIB |
| `poeMode` | `pethPsePortType` | POWER-ETHERNET-MIB |
| total PoE draw | `pethMainPseConsumptionPower` | POWER-ETHERNET-MIB |
| `stp.portState` | `dot1dStpPortState` | BRIDGE-MIB |
| switch MAC, port count | `dot1dBaseBridgeAddress`, `dot1dBaseNumPorts` | BRIDGE-MIB |
| serial, hardware rev, firmware rev | `entPhysicalTable` | ENTITY-MIB |
| SFP cage / transceiver | `entPhysicalTable` (module entities) | ENTITY-MIB |
| `uplink` | `lldpRemTable` — **draws the topology link** | LLDP-MIB |
| model, firmware, uptime | `sysDescr`, `sysUpTime` | SNMPv2-MIB |

Four things have no standard home, so they live in a small vendor subtree
(`mibs/FIREWALLA-SNMP-PROXY-MIB.txt`, load it into your NMS for named output):

| Firewalla data | Object | Why it isn't standard |
|---|---|---|
| `poePower` per port | `fwPortPoePower` (mW) | RFC 3621 models PoE *status* but has no per-port wattage object at all |
| `stp.portRole` | `fwPortStpRole` | Lives in IEEE8021-SPANNING-TREE-MIB, which few NMSes implement |
| `sfpInfo` | `fwPortSfp*` | No standard transceiver MIB in common use |
| ACL usage | `fwSwitchAcl*` | Entirely Firewalla-specific |

Plus a **proxy health group** (`fwProxy*`) — poll status, seconds since last
successful poll, API latency, last error. This is what you alert on: without it
you can't tell an idle switch from a proxy that lost API access and is serving
frozen counters. See [Monitoring the proxy itself](#monitoring-the-proxy-itself).

---

## Monitoring system recipes

The proxy is a normal SNMP v2c agent, so "add a device" works as usual. The one
non-default thing is the **port** — each switch listens on its own, starting at
16100.

Some systems can only poll UDP 161. If yours is one, either run the proxy with
`listen.base_port: 161` (one switch only, and it needs a privileged port), or
give the proxy host an extra IP and DNAT 161 to 16100.

### Observium

Observium keys devices on hostname, so give the proxy a name in `/etc/hosts`
(or DNS) first:

```
127.0.0.1   fwswitch
```

```bash
cd /opt/observium
./add_device.php fwswitch public v2c 16100
./discovery.php -h fwswitch
./poller.php -h fwswitch
```

For named vendor objects, copy the MIB in and rediscover:

```bash
cp mibs/FIREWALLA-SNMP-PROXY-MIB.txt /opt/observium/mibs/rfc/
```

### LibreNMS

```bash
cd /opt/librenms
./addhost.php fwswitch public v2c 16100
./discovery.php -h fwswitch
```

Or in the web UI: **Devices → Add Device**, set **Port** to `16100` and
SNMP version to **v2c**.

### Zabbix

Create a host with an **SNMP agent** interface, IP of the proxy host and port
`16100`. Set the macro `{$SNMP_COMMUNITY}` to your community, then attach the
built-in template **"Network Generic Device by SNMP"** — it will discover the
ports through IF-MIB automatically.

### Checkmk

```bash
cmk -v --add-host fwswitch
```

Set the SNMP port to 16100 in the host's properties (**SNMP** → **Port**), then
run a service discovery. The `if64` check plugin picks up the ports.

### PRTG

Add an **SNMP Library** or **SNMP Traffic** sensor against the proxy host, and
set the port to 16100 in the device's SNMP settings.

### Plain snmpwalk (start here when debugging)

```bash
# Everything
snmpwalk -v2c -c public 127.0.0.1:16100 1

# Just the interfaces
snmpwalk -v2c -c public 127.0.0.1:16100 1.3.6.1.2.1.2.2

# With the vendor MIB loaded
snmpwalk -m +FIREWALLA-SNMP-PROXY-MIB -M +./mibs \
         -v2c -c public 127.0.0.1:16100 1.3.6.1.4.1.99999
```

If a plain walk is clean and in order, every NMS above will work — that's the
real smoke test.

---

## Monitoring the proxy itself

The proxy serves the *last known good* data if the MSP API becomes unreachable.
That's deliberate — blanking counters would look like a dead switch — but it
means you should alert on the proxy's own health:

| Object | OID | Meaning |
|---|---|---|
| `fwProxyPollStatus` | `.1.3.6.1.4.1.99999.1.3.0` | `1` ok, `2` stale, `3` never polled |
| `fwProxySecondsSincePoll` | `.1.3.6.1.4.1.99999.1.2.0` | seconds since last good poll |
| `fwProxyLastError` | `.1.3.6.1.4.1.99999.1.5.0` | last error text |
| `fwProxyApiLatency` | `.1.3.6.1.4.1.99999.1.4.0` | last API round-trip, ms |

**Alert on `fwProxyPollStatus != 1`.** `stale(2)` means no successful poll for
three poll intervals.

---

## Counters, resets and why your graphs stay sane

SNMP counters must only ever increase; wrapping is fine and every NMS handles
it. Firewalla's port counters, though, can be **reset to zero** — by a firmware
event, or by someone clicking "reset statistics" in the MSP UI. A naive proxy
would republish that as a huge negative delta, which your NMS renders as a
garbage spike or a gap.

This proxy handles it two ways at once:

1. **Persistent monotonic offsets** (`state_file`). On a detected reset, the
   last value is folded into an offset, so what you see over SNMP keeps rising
   across resets, proxy restarts and switch reboots.
2. **`ifCounterDiscontinuityTime`** is published from Firewalla's
   `statsSinceTs`. NMSes that honour it discard the suspect delta themselves.

Resets are detected from *both* an advancing `statsSinceTs` and a decreasing
raw value, because neither alone is sufficient — if enough traffic passes
between polls, a reset counter can already be back *above* its previous reading,
and only the timestamp reveals it.

Deleting `state_file` is safe but causes one spurious spike on next start.

---

## Safety

This is a monitoring tool and it is aggressively read-only:

- **The API client can only issue GETs.** It also refuses, client-side, any URL
  containing a state-changing path segment (`reboot`, `upgrade`, `reset-stats`,
  `power-cycle`, `restart`, `switch-branch`, `check-status`, `force-sync`,
  `detach`, `attach`, `delete`). The MSP API really does expose those; a
  monitoring tool must never reach them. `reset-stats` in particular would
  destroy your counter history, and `power-cycle` drops traffic.
- **SNMP SET is refused** — by access control, and again in the instrumentation
  layer. There is no code path from SNMP to the Firewalla API.
- **Nothing is installed on the Firewalla or the switch.**
- The systemd unit runs as a dedicated non-root user under
  `ProtectSystem=strict` with an empty capability set and exactly one writable
  path (the state directory).

### Your API token

The token grants full read access to your Firewalla account, so treat it like a
password:

- It is **never accepted as a command-line argument** — that would leak it into
  your shell history and into `ps` output for every other user on the box.
- Precedence is `FIREWALLA_MSP_TOKEN` → `msp.token_file` → `msp.token`.
- The proxy warns if a file containing your token is group- or world-readable.
- `install.sh` puts it in a 0640 root-owned `EnvironmentFile`, so your
  `config.yaml` stays free of credentials and can be shared in a bug report.

---

## How fresh is the data?

Your NMS is polling a **cloud API**, not the switch. This is the most important
thing to understand before you set alert thresholds, so here are measured
numbers rather than hand-waving.

**Port counters update roughly every 5 minutes.** Sampling the API every 20s for
13 minutes, `rxBytes` on a busy trunk changed exactly 3 times — at 123s, 409s and
717s, i.e. intervals of 286s and 308s. Between those points the API returns byte
for byte the same value.

That is a property of the Firewalla cloud, not of this proxy, and it has
consequences no amount of proxy polling can fix:

- **Your effective graph resolution is ~5 minutes**, however fast your NMS polls.
- **Rate spikes shorter than the poll interval are invisible.** The counters are
  cumulative and accurate, so totals over an hour are right; a 30-second burst
  gets averaged across its bucket.
- **Don't build fast link-flap detection on this.** A port going down appears on
  the first poll after the cloud notices.

`/topology` and `/switches/<mac>` were observed changing at the *same instant*
with *identical* values, so they share one cache — there is no fresher endpoint
to prefer, and the proxy makes one topology call per cycle rather than one per
switch.

`fwProxyApiLatency` reports what each API round-trip actually costs.

### Rate limits, and why `poll_interval` defaults to 15 minutes

**The MSP API enforces a request quota.** Each poll cycle costs several calls
(`/topology`, `/switch-settings`, `/switches/<mac>`, plus a periodic
`/devices`), so a fast interval burns through it steadily. Once the quota is
exhausted the API returns **HTTP 429 to everything** until it resets.

That failure is dangerous precisely because it is quiet. The proxy keeps
answering SNMP, the counters simply stop advancing, and every rate your NMS
derives from them is a legitimate-looking **zero**. Nothing looks broken; the
graph just goes flat. This is not hypothetical — it is what prompted the 2.1.0
release, after a 429 lockout silently flatlined a port's graph for hours.

Two mechanisms address it:

**1. A 15-minute default interval.** `poll_interval: 900` keeps well clear of
the quota. The old 60s default was chosen to minimise latency behind the
cloud's ~5-minute data cadence, which was the wrong thing to optimise: it
bought a few minutes of freshness at the cost of ~4,300 API calls a day.

**2. Exponential backoff on 429.** On a rate-limit response the poller stops
issuing requests entirely until the backoff expires — retrying through a 429
keeps the quota pinned and can extend the lockout. Backoff starts at
`poll_interval`, doubles per consecutive 429, is jittered (so several proxies
sharing a token don't retry in lockstep and re-trip the quota together), and is
capped by `max_backoff_seconds` (default 3600). A `Retry-After` header from the
API always wins over the computed delay. Recovery needs no operator: the first
successful cycle resets the schedule.

While backed off, `fwProxyLastError` says so explicitly and
`fwProxySecondsSincePoll` keeps climbing — **alert on those**, not on traffic
rates, which cannot distinguish a rate limit from a quiet switch.

### Surviving an API outage without going down

Backing off politely is not enough on its own, because the SNMP sockets are
built from the port layout — and that used to come only from a live `/topology`
call. So a proxy restarted mid-lockout stayed up but never *listened*, and the
NMS saw the device as **down** for the whole lockout.

Two things prevent that:

**A topology cache.** Every successful poll writes the merged API payload to
`topology_cache`. If the API is unavailable at startup, agents are rebuilt from
that file and serve immediately, publishing the **full** port set. Counters are
frozen at their cached values, so derived rates read zero — which is honest, and
`fwProxyServingCache` is what tells you the difference between that and an idle
switch. The poller keeps retrying in the background and upgrades to live data
the moment the API recovers.

Serving a *truncated* ifTable during a lockout was considered and rejected:
Observium marks ports absent from ifTable as deleted, which would discard the
very port history the cache exists to protect. Either every port is served or
none is — which is why a first-ever run with no cache still has to wait.

**An ICMP reachability check.** `ping_host` pings the real switch on its own
cadence (`ping_interval`, default 60s), independently of the poll loop. This is
what makes serving cached data *safe*: the counters are admittedly old, but
`fwSwitchOnline` and `fwProxyIcmpStatus` still reflect reality, so a
stale-but-alive switch stays distinguishable from one that has actually gone
away. It costs no API quota, so it keeps working through a lockout — during
which it is the only live signal you have.

Reachability is **tri-state**, and the third state matters. A check that could
not be *carried out* — unresolvable name, no ICMP permission — is `unknown`, not
`down`; folding the two together would report a fake outage every time the host
rebooted before its resolver was ready. Transitions are debounced, and
asymmetrically: `ping_fail_threshold` defaults to 3 while
`ping_recover_threshold` defaults to 1, because coming back is unambiguous and
going away is not.

No privileges are needed. An unprivileged ICMP datagram socket is used where
`net.ipv4.ping_group_range` permits, falling back to the `ping` binary
otherwise; both paths work under this project's hardened systemd unit.

Prefer a **hostname** over a literal IP for `ping_host` — if the switch's
address changes via DHCP, a hardcoded IP silently starts reporting a dead
switch.

What each state looks like to an NMS:

| Situation | `fwProxyPollStatus` | `fwProxyServingCache` | `fwProxyIcmpStatus` | Rates |
|---|---|---|---|---|
| Normal | `ok(1)` | `false(2)` | `up(1)` | real |
| API rate limited, switch alive | `stale(2)` | `true(1)` if restarted | `up(1)` | zero |
| API rate limited, switch gone | `error(3)` | `true(1)` if restarted | `down(2)` | zero |
| Ping target unresolvable | unchanged | — | `unknown(3)` | — |
| No `ping_host` set | age-based | — | `disabled(4)` | — |

Two things are deliberately *not* faked. Per-port `ifOperStatus` keeps its
last-known value while serving cache, because ICMP to the switch says nothing
about individual port link state — reporting `down(2)` would invent an outage
and `unknown(4)` would churn port-state history. And recovery from a long
lockout lands the accumulated delta in one overstated sample, because the ramp
is skipped past `max_ramp_seconds`; spreading an hour of old traffic across the
following hour would mask what the switch is doing *now*, and RRA consolidation
fixes the long-run average either way.

### Counter smoothing: a slow poll for a fast NMS

A 15-minute interval creates a cadence mismatch, because most NMSes poll faster
and on a schedule you can't change — Observium's poller is a fixed 5-minute
cron. Served raw, that produces a **sawtooth**: two of every three polls see
byte-identical counters and derive a rate of zero, and the third divides 900s of
traffic by 300s, overstating the rate by 3x. Every individual sample is wrong,
and only after RRA consolidation does the average return to the truth.

`counter_smoothing: ramp` (the default) fixes this in the proxy, so you don't
have to touch your NMS's polling at all. Each newly-learned increment is spread
evenly across the window *following* the poll that revealed it, so a 5-minute
poller sees a third of it each time and derives the same correct rate every
sample. Specifically, for counter values `C1` at `T1` and `C2` at `T2`, a
request at `t` is served `C1 + (C2 - C1) * (t - T2) / (T2 - T1)`, clamped.

What this buys and what it costs:

- **Byte totals stay exact.** Nothing is invented or discarded; the same bytes
  are redistributed across the window instead of dumped into one sample.
- **The window is measured, not configured.** `T2 - T1` is observed, so if a
  backoff stretches the real interval to 40 minutes, the ramp stretches with it
  automatically.
- **Output stays monotonically non-decreasing**, as SNMP counters must be.
- **The graph lags by up to one interval.** At `T2` we know how many bytes
  arrived since `T1` but nothing about what is arriving now, so this is the
  price of not extrapolating.
- **Sub-window bursts are flattened.** A 30-second 900 Mbps burst inside a
  15-minute window reads as ~30 Mbps sustained. The API does not carry
  within-window traffic shape, so the alternative isn't better resolution — it's
  the sawtooth above, which is a worse misrepresentation.
- **A stalled API still reads as zero, not as invented traffic.** The ramp
  advances on counter *changes*, not on the poll loop firing, so an idle port
  and a frozen upstream both correctly plateau.

Set `counter_smoothing: raw` to publish values unmodified — correct if your NMS
polls at or slower than `poll_interval`. `max_ramp_seconds` caps the observed
window past which ramping is skipped (0 derives it as 2.5x `poll_interval`), so
a multi-hour outage's accumulated bytes aren't smeared across an equally long
ramp, which would hide the recovery.

## Configuration

Generated by `init`; see [`config.example.yaml`](config.example.yaml) for every
option with comments. The essentials:

```yaml
msp:
  domain: "dn-abc123.firewalla.net"
poll_interval: 900         # 15 min; see "Rate limits" above
counter_smoothing: ramp    # or "raw"; see "Counter smoothing" above
max_backoff_seconds: 3600  # ceiling on 429 backoff
topology_cache: "/var/lib/firewalla-snmp-proxy/topology.json"
ping_host: "switch.example.com"   # live reachability; prefer a hostname
ping_interval: 60
listen:
  address: "127.0.0.1"     # 0.0.0.0 to allow a remote NMS
  base_port: 16100         # one port per switch
community: "public"
enterprise_oid: "1.3.6.1.4.1.99999"
state_file: "/var/lib/firewalla-snmp-proxy/counters.json"
switches:
  - mac: "20:6D:31:00:00:01"
    port: 16100
```

### Replacing an existing SNMP proxy

If you already monitor the switch through some other proxy, your NMS keys its
device and OS detection on `sysObjectID`. Changing it makes the NMS treat this
as a brand-new device and abandon the historical port graphs. To migrate in
place, pin the old value:

```yaml
sys_object_id: "1.3.6.1.4.1.8072.3.2.10.99.2"   # whatever the old proxy reported
community: "your-old-community"
listen:
  base_port: 16100                               # the port your NMS already polls
```

Port RRDs are matched on `ifDescr`/`ifIndex`. This proxy names ports `Port 1` …
`Port N` with `ifIndex` equal to the physical port number, so if your old proxy
did the same, history carries over with no further work.

### The enterprise OID

`99999` is a **placeholder** in unassigned IANA space. Assignments are currently
around 66649, so it won't collide for many years, and SNMP doesn't validate
enterprise numbers — everything works.

If you'd rather register your own (free, at
[iana.org/assignments/enterprise-numbers](https://www.iana.org/assignments/enterprise-numbers)),
change `enterprise_oid` **and** the matching `::= { enterprises 99999 }` line in
`mibs/FIREWALLA-SNMP-PROXY-MIB.txt`. Note this gives the four vendor objects new
OIDs, so your NMS will start fresh graphs for them; everything in the standard
MIBs is unaffected.

---

## Multiple switches

`init` finds them all and assigns consecutive ports:

```yaml
switches:
  - mac: "20:6D:31:00:00:01"
    port: 16100
  - mac: "20:6D:31:00:00:02"
    port: 16101
```

One process serves all of them. Each gets its own UDP port rather than sharing
one port with different community strings, because some monitoring systems
identify a device by `IP:port` and would otherwise merge them.

---

## What this deliberately does *not* publish

Absent beats invented. If the API doesn't supply a value, the OID isn't
instantiated at all — a sparse table is legal SNMP and reads honestly, whereas
a plausible zero shows up on your dashboard as fact.

- **No temperature sensor.** The Switch SE is fanless and reports
  `temperature: 0` with `fanStatus: "none"` — it has no thermal sensor.
  Publishing 0 °C would trip low-temperature alarms. If a future model reports a
  real reading, the sensor appears automatically.
- **No `ifPhysAddress`** — the API gives no per-port MAC.
- **No PoE fault counters** (`pethPsePortMPSAbsentCounter` and friends) — zeros
  would read as "no PoE fault has ever occurred", which can't be substantiated.
- **No `pethMainPsePower`** (the PoE budget) — the API reports `budgetUtil` but
  never the actual budget.
- **No Q-BRIDGE-MIB / VLAN tables** — ports carry a Firewalla network *UUID*,
  not an 802.1Q VLAN ID, so populating them would mean inventing tag numbers.
  The network name is in `ifAlias` and `fwPortNetworkName` instead.
- **No link up/down traps.** The proxy polls on an interval and can't observe a
  transition as it happens, so `ifLinkUpDownTrapEnable` reports `disabled(2)`.

Two values are passed through but deliberately uninterpreted, because their
meaning is undocumented and the obvious reading is wrong:
`fwSwitchPoeBudgetUtil` (observed as 114 alongside 10.5 W of draw — not a
percentage) and `fwSwitchAclMax` (observed as 256 against a count of 1229, so it
appears to bound only the *control* ACLs).

---

## Troubleshooting

**`check` fails with "endpoint does not exist"**
Your MSP domain is probably wrong. On this API an unknown path returns HTTP 200
with the web console's HTML rather than a 404, so the client validates the
content type. Confirm the hostname you use to reach MSP in a browser.

**"MSP API rejected the token"**
Regenerate it in the MSP web console. Tokens are per-account, not per-box.

**"No Firewalla switches found"**
The account has no switch attached. This proxy needs a Firewalla Switch; it
doesn't proxy the firewall box itself.

**`snmpwalk` times out**
Check `listen.address`. The default `127.0.0.1` only accepts local traffic — set
`0.0.0.0` for a remote NMS, and open the UDP port in your firewall.

**Counters flat at zero**
Look at `fwProxyPollStatus` and `fwProxyLastError`; the poll is probably
failing. `journalctl -u firewalla-snmp-proxy -f` has the detail.

**Vendor OIDs show as numbers, not names**
The MIB isn't loaded in your NMS. Copy `mibs/FIREWALLA-SNMP-PROXY-MIB.txt` into
its MIB directory. Standard MIB objects resolve without it.

**A negative spike in a traffic graph**
The Firewalla reset its counters and `state_file` wasn't writable, so offsets
couldn't persist. Check permissions on the state directory.

---

## Development

```bash
git clone https://github.com/kdesch5000/firewalla-snmp-proxy
cd firewalla-snmp-proxy
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The tests need no network and no Firewalla account — switch data comes from
fixtures in `tests/fixtures/`. The end-to-end tests bind a loopback UDP port and
drive the agent with a real SNMP client, so they exercise actual BER encoding,
community authentication and GETNEXT walks.

Layout:

```
firewalla_snmp_proxy/
  oid.py            sorted OID tree; all walk ordering lives here
  model.py          normalizes MSP API JSON into a Switch/Port model
  counters.py       persistent monotonic counter offsets
  msp_api.py        read-only MSP API client (stdlib HTTP only)
  mibs/             one module per MIB
  agent.py          pysnmp wiring, one agent per switch
  poller.py         periodic refresh
  cli.py            init / check / walk / run
mibs/               the shipped vendor MIB
```

If you're adding a MIB module, the rule is: **register nothing you can't
substantiate.** Values are registered as callables closing over a mutable
context, so the tree is built once but always serves live data.

---

## Acknowledgements

Built against the undocumented Firewalla MSP switch endpoints
(`/v2/boxes/<gid>/topology` and `/v2/boxes/<gid>/switches/<mac>`), which are not
in the published API reference. They may change without notice; the model layer
is defensive about missing fields for exactly that reason.

Not affiliated with or endorsed by Firewalla.

## License

MIT — see [LICENSE](LICENSE).
