# Contributing

Thanks for taking a look. Bug reports and switch-model coverage are the two
most useful things you can bring.

## Before you file a bug

Run these and include the output:

```bash
firewalla-snmp-proxy --version
firewalla-snmp-proxy check -c /etc/firewalla-snmp-proxy/config.yaml
snmpwalk -v2c -c <community> 127.0.0.1:<port> 1.3.6.1.4.1.99999
```

That last one is the proxy's own health subtree — `fwProxyPollStatus`,
`fwProxyLastError`, `fwProxyIcmpStatus` and `fwProxyServingCache` between them
explain most "my graphs went flat" reports without further digging.

**Scrub before posting.** Output contains your switch MAC, serial and IP, and
`--debug` includes request URLs with your MSP domain in them. Never paste an
API token.

## Development

```bash
git clone https://github.com/kdesch5000/firewalla-snmp-proxy
cd firewalla-snmp-proxy
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest -q
```

Tests are offline — no network, no real switch, no MSP token. Fixtures in
`tests/fixtures/` are sanitized captures of real API responses.

## What a good patch looks like

- **Tests come with it.** Every behaviour in this project is covered; a change
  without a test will be asked for one.
- **Say why in the comment, not what.** The codebase leans on comments that
  record the reasoning behind a non-obvious choice, because most of the tricky
  parts here are about what an NMS does with the data rather than what the code
  does. Match that.
- **Keep it read-only.** Patches that add a mutating API call, an SNMP `SET`,
  or anything installed on the Firewalla itself will be declined. See
  [SECURITY.md](SECURITY.md).
- **CI must be green on Python 3.9 through 3.13.** 3.9 is the floor declared in
  `pyproject.toml`; if you need newer syntax, raise the floor in that file in
  the same PR rather than letting the metadata drift.

## Adding support for another switch model

The proxy is written against the Firewalla Switch SE. Other models should
mostly work, since everything comes from the same MSP `/topology` payload — but
the fields vary.

The most useful contribution is a **sanitized capture** of your switch's
payload as a new file in `tests/fixtures/`: replace MACs with the
`20:6D:31:00:00:0X` pattern, drop serials and IPs, and keep the structure
intact. That lets model differences be handled with a test rather than
guesswork.

## Releases

Maintainer notes: bump `version` in `pyproject.toml` and `__version__` in
`firewalla_snmp_proxy/__init__.py` together, add a `CHANGELOG.md` entry, then
tag `vX.Y.Z`. The release workflow refuses to publish if the tag and the
packaged version disagree.
