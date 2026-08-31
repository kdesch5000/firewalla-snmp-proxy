#!/usr/bin/env bash
#
# Thin shim over `firewalla-snmp-proxy install-service`.
#
# The real implementation lives in the Python package (see
# firewalla_snmp_proxy/service.py) so that installing from PyPI and installing
# from a clone reach exactly the same end state: a systemd unit that is enabled
# at boot and can be started and stopped by hand. Keeping a second copy of the
# logic here is how the two paths would drift apart.
#
#   sudo ./install.sh --service
#   sudo ./install.sh --service --user snmpproxy
#   sudo ./install.sh --uninstall
#
set -euo pipefail

SERVICE_NAME="firewalla-snmp-proxy"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

ACTION="install"
PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)   ACTION="install"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --purge)     PASSTHROUGH+=(--purge); shift ;;
        --user)      PASSTHROUGH+=(--user "${2:?--user needs a value}"); shift 2 ;;
        --no-start)  PASSTHROUGH+=(--no-start); shift ;;
        -h|--help)   usage ;;
        *)           die "unknown option: $1 (try --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "must run as root (use sudo)"

BIN=""
for candidate in \
    "/usr/local/bin/${SERVICE_NAME}" \
    "/usr/bin/${SERVICE_NAME}" \
    "$(command -v ${SERVICE_NAME} 2>/dev/null || true)"
do
    [[ -n "$candidate" && -x "$candidate" ]] && { BIN="$candidate"; break; }
done

if [[ -z "$BIN" ]]; then
    die "cannot find the '${SERVICE_NAME}' executable.

Install it system-wide first (it must not live under /home or /root -- the
service sandbox sets ProtectHome=yes and cannot execute anything there):

    sudo pipx --global install ${SERVICE_NAME}

or, from this checkout:

    sudo pipx --global install ."
fi

if [[ "$ACTION" == "install" ]]; then
    exec "$BIN" install-service --binary "$BIN" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"
else
    exec "$BIN" uninstall-service "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"
fi
