#!/bin/bash

# Managed IOC entry point for the beamline softIOC/procServ infrastructure.
#
# This mirrors the legacy /epics/iocs/hiden deployment shape while running the
# current pixi-based caproto IOC. Listen on all interfaces to receive broadcast
# searches on Linux; direct-IP binding can exclude those searches.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${REPO_DIR}"

# Client-side CA defaults are useful for any local caget/caput calls started
# from the same process environment.
export EPICS_CA_SERVER_PORT="${EPICS_CA_SERVER_PORT:-5064}"
export EPICS_CA_REPEATER_PORT="${EPICS_CA_REPEATER_PORT:-5065}"
export EPICS_CA_AUTO_ADDR_LIST="${EPICS_CA_AUTO_ADDR_LIST:-NO}"
export EPICS_CA_ADDR_LIST="${EPICS_CA_ADDR_LIST:-10.66.59.255}"

# Server-side caproto/Channel Access settings.
export EPICS_CAS_AUTO_BEACON_ADDR_LIST="${EPICS_CAS_AUTO_BEACON_ADDR_LIST:-NO}"
export EPICS_CAS_BEACON_ADDR_LIST="${EPICS_CAS_BEACON_ADDR_LIST:-10.66.59.255}"
# This also exposes the server on INST; beacons remain on the EPICS subnet.
export EPICS_CAS_INTF_ADDR_LIST="${EPICS_CAS_INTF_ADDR_LIST:-0.0.0.0}"

PIXI_BIN="${PIXI_BIN:-pixi}"
if command -v "${PIXI_BIN}" >/dev/null 2>&1; then
    exec "${PIXI_BIN}" run --locked ioc
fi

echo "ERROR: pixi not found. Set PIXI_BIN to its absolute path or fix the service PATH." >&2
echo "Install with 'pixi install --locked' in ${REPO_DIR} before deployment." >&2
exit 1
