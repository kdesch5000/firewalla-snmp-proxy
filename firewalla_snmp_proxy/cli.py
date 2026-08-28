"""Command-line interface.

Four subcommands, matching the order an operator actually needs them:

``init``    discover switches via the API and write a ready-to-use config
``check``   validate config + API + every OID renders, without binding sockets
``run``     serve SNMP (the systemd entry point)
``walk``    dump the whole tree as text, for eyeballing or diffing
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from typing import Dict, List, Optional

from . import __version__
from .agent import SwitchAgent
from .config import (
    DEFAULT_ENTERPRISE_OID,
    TOKEN_ENV,
    Config,
    ConfigError,
    SwitchConfig,
    load,
)
from .counters import CounterStore
from .ramp import CounterRamp
from .mibs import SwitchContext
from .model import Switch
from .msp_api import MspClient, MspError, MspRateLimited
from .poller import Poller, rate_limit_delay

log = logging.getLogger("firewalla_snmp_proxy")

DEFAULT_CONFIG_PATHS = (
    "/etc/firewalla-snmp-proxy/config.yaml",
    os.path.expanduser("~/.config/firewalla-snmp-proxy/config.yaml"),
    "config.yaml",
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _find_config(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        if not os.path.exists(explicit):
            raise ConfigError("config file not found: %s" % explicit)
        return explicit
    for path in DEFAULT_CONFIG_PATHS:
        if os.path.exists(path):
            return path
    return None


def _client(cfg: Config) -> MspClient:
    return MspClient(cfg.domain, cfg.token, verify_tls=cfg.verify_tls)


def _resolve_gid(client: MspClient, cfg: Config) -> str:
    if cfg.box_gid:
        return cfg.box_gid
    boxes = client.boxes()
    if not boxes:
        raise ConfigError("no Firewalla boxes visible to this token")
    if len(boxes) > 1:
        names = ", ".join(
            "%s (%s)" % (b.get("name"), b.get("gid")) for b in boxes
        )
        raise ConfigError(
            "this token sees %d boxes: %s\nSet msp.box_gid in the config to "
            "choose one." % (len(boxes), names)
        )
    return str(boxes[0]["gid"])


def _build_contexts(cfg: Config, client: MspClient, gid: str) -> Dict[str, SwitchAgent]:
    """Fetch each configured switch once and build its agent (unbound)."""
    counters = CounterStore(cfg.state_file)
    # One ramp shared by every agent: its state is keyed by switch MAC, and a
    # per-agent instance would reset whenever the tree was rebuilt.
    ramp = (
        CounterRamp(cfg.ramp_window)
        if cfg.counter_smoothing == "ramp"
        else None
    )
    networks = client.network_names(gid)
    settings = client.switch_settings(gid)
    nodes = {
        str(n.get("id") or n.get("mac") or "").upper(): n
        for n in client.find_switches(gid)
    }

    agents: Dict[str, SwitchAgent] = {}
    for sc in cfg.switches:
        mac = sc.normalized_mac
        node = nodes.get(mac)
        if node is None:
            raise ConfigError(
                "switch %s is configured but not present in the MSP topology. "
                "Available: %s" % (mac, ", ".join(sorted(nodes)) or "none")
            )
        merged = dict(node)
        try:
            merged.update(client.switch_detail(gid, mac) or {})
        except MspError as exc:
            log.warning("could not fetch detail for %s: %s", mac, exc)

        import time as _time

        switch = Switch(
            raw=merged,
            settings=settings,
            networks=networks,
            polled_at=_time.time(),
            name_override=sc.name,
        )
        ctx = SwitchContext(
            switch=switch,
            counters=counters,
            enterprise_oid=cfg.enterprise_oid,
            sys_object_id=cfg.sys_object_id,
            sys_contact=cfg.sys_contact,
            sys_location=cfg.sys_location,
            proxy_version=__version__,
            stale_after=max(300.0, cfg.poll_interval * 3.0),
            ramp=ramp,
        )
        ctx.poll_count = 1
        ctx.last_poll_ok = _time.time()
        ctx.api_latency_ms = int((client.last_latency or 0.0) * 1000)
        agents[mac] = SwitchAgent(
            ctx, cfg.listen_address, sc.port, cfg.community_for(sc)
        )
    return agents


# -- init ----------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token and args.token_file:
        with open(args.token_file, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    if not token:
        print(
            "No API token supplied.\n\n"
            "Generate one in the Firewalla MSP web console (account settings ->\n"
            "API tokens), then either:\n\n"
            "    export %s=<token>\n"
            "    firewalla-snmp-proxy init --domain <your>.firewalla.net\n\n"
            "or put it in a file readable only by you and pass --token-file.\n\n"
            "The token is intentionally not accepted as a command-line argument:\n"
            "that would leak it into your shell history and into 'ps' output for\n"
            "every other user on the machine." % TOKEN_ENV,
            file=sys.stderr,
        )
        return 2

    domain = args.domain or os.environ.get("FIREWALLA_MSP_DOMAIN", "")
    if not domain:
        print(
            "--domain is required, e.g. --domain dn-abc123.firewalla.net\n"
            "It is the hostname in the URL of your MSP web console.",
            file=sys.stderr,
        )
        return 2

    client = MspClient(domain, token)
    try:
        boxes = client.boxes()
    except MspError as exc:
        print("Cannot reach the MSP API: %s" % exc, file=sys.stderr)
        return 1

    if not boxes:
        print("No Firewalla boxes visible to this token.", file=sys.stderr)
        return 1

    print("# Discovered %d box(es):" % len(boxes))
    lines: List[str] = []
    total_switches = 0
    for box in boxes:
        gid = str(box.get("gid"))
        print(
            "#   %s  model=%s  version=%s  gid=%s"
            % (box.get("name"), box.get("model"), box.get("version"), gid)
        )
        try:
            switches = client.find_switches(gid)
        except MspError as exc:
            print("#     ! could not read topology: %s" % exc)
            continue
        if not switches:
            print("#     (no switches attached)")
        for sw in switches:
            port = args.base_port + total_switches
            total_switches += 1
            nports = len(sw.get("ports") or [])
            print(
                "#     switch %s  %s  model=%s  ports=%d  -> UDP %d"
                % (sw.get("id"), sw.get("name"), sw.get("model"), nports, port)
            )
            lines.append(
                "  - mac: \"%s\"\n    port: %d\n    # %s"
                % (sw.get("id"), port, sw.get("name"))
            )

    if not lines:
        print(
            "\nNo Firewalla switches found on this account. This proxy needs at "
            "least one\nswitch (e.g. a Firewalla Switch SE) attached to a box.",
            file=sys.stderr,
        )
        return 1

    gid_line = ""
    if len(boxes) > 1:
        gid_line = "  # Multiple boxes found; set the one you want:\n  box_gid: \"%s\"\n" % boxes[0].get("gid")

    config_text = """# firewalla-snmp-proxy configuration
# Generated by 'firewalla-snmp-proxy init'.

msp:
  domain: "%s"
%s  # The token is read from the %s environment variable by default.
  # To keep it out of this file entirely, leave 'token' unset and use the
  # systemd EnvironmentFile (see install.sh) or set token_file below.
  # token_file: /etc/firewalla-snmp-proxy/token
  # token: "..."

# How often to refresh from the MSP API, in seconds. The MSP API enforces a
# request quota, and each cycle costs several calls, so this is the main knob
# for staying under it: 900 (15 min) is a safe default, and the minimum
# accepted is 15. Polling faster than the API's own stat refresh gains nothing.
poll_interval: 900

# How counters are presented between MSP refreshes.
#
#   ramp - spread each newly-learned increment evenly over the following
#          window, so an NMS polling faster than poll_interval still computes
#          sane rates instead of a 0/0/3x sawtooth. Totals stay exact; the
#          graph lags by up to one window and sub-window bursts are averaged.
#   raw  - publish the last value fetched, unmodified.
counter_smoothing: ramp

# Observed-window ceiling past which ramping is skipped, in seconds. 0 derives
# it from poll_interval (2.5x), which is usually what you want.
max_ramp_seconds: 0

# Ceiling on exponential backoff after an HTTP 429, in seconds. A Retry-After
# header from the API always takes precedence over the computed delay.
max_backoff_seconds: 3600

listen:
  # 127.0.0.1 is the safe default. Change to 0.0.0.0 to let an NMS on another
  # host poll this proxy, and firewall the port accordingly.
  address: 127.0.0.1
  base_port: %d

# SNMPv1/v2c community. This grants read-only access to switch data.
community: "public"

# Base OID for the vendor subtree (per-port PoE watts, STP role, SFP detail,
# ACL counts). This is a placeholder in unassigned IANA space; see the README.
enterprise_oid: "%s"

sys_contact: ""
sys_location: ""

# Persistent counter offsets. Keeps SNMP counters monotonic across proxy
# restarts and across Firewalla counter resets. Must be writable by the
# service user.
state_file: "%s"

switches:
%s
""" % (
        domain,
        gid_line,
        TOKEN_ENV,
        args.base_port,
        DEFAULT_ENTERPRISE_OID,
        args.state_file,
        "\n".join(lines),
    )

    if args.output == "-":
        print()
        print(config_text)
        return 0

    if os.path.exists(args.output) and not args.force:
        print(
            "\n%s already exists; refusing to overwrite. Use --force to replace "
            "it, or --output - to print to stdout." % args.output,
            file=sys.stderr,
        )
        return 1

    directory = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(directory, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(config_text)
    os.chmod(args.output, 0o640)
    print("\n# Wrote %s" % args.output)
    print("# Next: firewalla-snmp-proxy check -c %s" % args.output)
    return 0


# -- check ---------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    path = _find_config(args.config)
    if not path:
        print(
            "No config file found. Looked in:\n  %s\nRun 'firewalla-snmp-proxy "
            "init' first." % "\n  ".join(DEFAULT_CONFIG_PATHS),
            file=sys.stderr,
        )
        return 2
    cfg = load(path)
    cfg.validate()
    print("config      : %s" % path)
    print("msp domain  : %s" % cfg.domain)
    print("poll interval: %ds" % cfg.poll_interval)
    print("smoothing   : %s%s" % (
        cfg.counter_smoothing,
        " (ramp window %ds)" % int(cfg.ramp_window)
        if cfg.counter_smoothing == "ramp" else "",
    ))
    print("enterprise  : %s" % cfg.enterprise_oid)

    client = _client(cfg)
    gid = _resolve_gid(client, cfg)
    print("box gid     : %s" % gid)
    print("api latency : %d ms" % int((client.last_latency or 0) * 1000))

    if not cfg.switches:
        print("\nNo switches configured.", file=sys.stderr)
        return 1

    agents = _build_contexts(cfg, client, gid)
    failures = 0
    for mac, agent in agents.items():
        sw = agent.ctx.switch
        tree = agent.tree
        oids = tree.oids
        ordered = oids == sorted(oids)
        bad = []
        for oid in oids:
            try:
                tree.get(oid)
            except Exception as exc:  # noqa: BLE001 - report, don't abort
                bad.append((oid, exc))
        print(
            "\nswitch %s (%s)\n  model      : %s  firmware %s  S/N %s"
            % (mac, sw.name, sw.model_name, sw.firmware_rev, sw.serial)
        )
        print("  ports      : %d (%d up)" % (
            len(sw.ports), sum(1 for p in sw.ports if p.link_up)))
        print("  uptime     : %s s" % sw.uptime_seconds)
        print("  listen     : %s:%d community=%s" % (
            cfg.listen_address, agent.listen_port, cfg.community_for(
                next(s for s in cfg.switches if s.normalized_mac == mac))))
        print("  oids       : %d" % len(oids))
        print("  ordering   : %s" % ("ok" if ordered else "BROKEN"))
        print("  rendering  : %s" % ("ok" if not bad else "%d FAILED" % len(bad)))
        if sw.temperature_c is None:
            print("  temperature: not instrumented (sensor omitted, as intended)")
        if not ordered or bad:
            failures += 1
            for oid, exc in bad[:5]:
                print("    %s -> %s" % (".".join(map(str, oid)), exc))

    print("\n%s" % ("OK" if not failures else "%d switch(es) FAILED" % failures))
    return 0 if not failures else 1


# -- walk ----------------------------------------------------------------
def cmd_walk(args: argparse.Namespace) -> int:
    path = _find_config(args.config)
    if not path:
        print("No config file found.", file=sys.stderr)
        return 2
    cfg = load(path)
    cfg.validate()
    client = _client(cfg)
    gid = _resolve_gid(client, cfg)
    agents = _build_contexts(cfg, client, gid)

    for mac, agent in agents.items():
        if args.switch and args.switch.upper() != mac:
            continue
        print("# %s (%s) - %d objects" % (mac, agent.ctx.switch.name, len(agent.tree)))
        for oid in agent.tree.oids:
            value = agent.tree.get(oid)
            print("%s = %s" % (".".join(map(str, oid)), _fmt(value)))
    return 0


def _fmt(value) -> str:
    try:
        from pysnmp.proto import rfc1902

        if isinstance(value, rfc1902.OctetString):
            raw = bytes(value)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return "HEX: " + raw.hex(" ")
            if text and not text.isprintable():
                return "HEX: " + raw.hex(" ")
            return '"%s"' % text
    except ImportError:  # pragma: no cover
        pass
    return str(value)


# -- run -----------------------------------------------------------------
async def _startup(cfg: Config, client: MspClient):
    """Resolve the box and build agents, waiting out any rate limit.

    Startup necessarily hits the API before the poller exists, so it needs its
    own backoff. Exiting on a 429 instead would be actively harmful: systemd's
    ``Restart=on-failure`` with a 15s ``RestartSec`` would crash-loop against
    the rate-limited API, spending several calls every 15 seconds and keeping
    the quota pinned indefinitely. Retrying in-process, slowly, cannot.
    """
    strikes = 0
    while True:
        try:
            gid = _resolve_gid(client, cfg)
            return gid, _build_contexts(cfg, client, gid)
        except MspRateLimited as exc:
            strikes += 1
            delay = rate_limit_delay(
                exc, cfg.poll_interval, strikes, float(cfg.max_backoff_seconds)
            )
            log.warning(
                "MSP API rate limited during startup (attempt %d): %s -- "
                "retrying in %ds (~%s). Staying up rather than exiting, so "
                "systemd cannot restart us straight back into the rate limit.",
                strikes, exc, int(delay),
                time.strftime("%H:%M:%S", time.localtime(time.time() + delay)),
            )
            await asyncio.sleep(delay)


async def _serve(cfg: Config) -> int:
    client = _client(cfg)
    gid, agents = await _startup(cfg, client)

    counters = CounterStore(cfg.state_file)
    for agent in agents.values():
        agent.ctx.counters = counters
        agent.start()

    poller = Poller(
        client, gid, agents, counters, cfg.poll_interval,
        max_backoff=float(cfg.max_backoff_seconds),
    )

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _shutdown(signame: str) -> None:
        log.info("received %s, shutting down", signame)
        poller.stop()
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown, sig.name)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    poll_task = asyncio.create_task(poller.run())
    log.info(
        "serving %d switch(es); poll interval %ds; smoothing %s; "
        "rate-limit backoff capped at %ds",
        len(agents), cfg.poll_interval, cfg.counter_smoothing,
        cfg.max_backoff_seconds,
    )
    await stopping.wait()
    poll_task.cancel()
    for agent in agents.values():
        agent.stop()
    counters.save(force=True)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    path = _find_config(args.config)
    if not path:
        print(
            "No config file found. Run 'firewalla-snmp-proxy init' first.",
            file=sys.stderr,
        )
        return 2
    cfg = load(path)
    cfg.validate()
    if not cfg.switches:
        print("No switches configured in %s" % path, file=sys.stderr)
        return 1
    return asyncio.run(_serve(cfg))


# -- entry point ---------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="firewalla-snmp-proxy",
        description=(
            "Expose Firewalla Switch port data over SNMP, so any SNMP "
            "monitoring system can graph a switch that has no SNMP agent."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="discover switches and write a config file")
    p_init.add_argument("--domain", help="MSP domain, e.g. dn-abc123.firewalla.net")
    p_init.add_argument("--token-file", help="file containing the API token")
    p_init.add_argument(
        "-o", "--output", default="config.yaml", help="config path, or - for stdout"
    )
    p_init.add_argument("--base-port", type=int, default=16100)
    p_init.add_argument(
        "--state-file", default="/var/lib/firewalla-snmp-proxy/counters.json"
    )
    p_init.add_argument("--force", action="store_true", help="overwrite existing config")
    p_init.set_defaults(func=cmd_init)

    p_check = sub.add_parser(
        "check", help="validate config and API access without binding sockets"
    )
    p_check.add_argument("-c", "--config")
    p_check.set_defaults(func=cmd_check)

    p_walk = sub.add_parser("walk", help="print the whole OID tree as text")
    p_walk.add_argument("-c", "--config")
    p_walk.add_argument("--switch", help="limit to one switch MAC")
    p_walk.set_defaults(func=cmd_walk)

    p_run = sub.add_parser("run", help="serve SNMP (systemd entry point)")
    p_run.add_argument("-c", "--config")
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except (ConfigError, MspError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
