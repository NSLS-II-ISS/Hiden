#!/bin/bash

# Managed IOC entry point for the beamline softIOC/procServ infrastructure.
#
# This mirrors the legacy /epics/iocs/hiden deployment shape while running the
# current pixi-based caproto IOC. Keep IOC-side CAS settings here so the server
# advertises on the EPICS interface, not the INST or default interface.

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${REPO_DIR}"

# Client-side CA defaults are useful for any local caget/caput calls started
# from the same process environment.
export EPICS_CA_SERVER_PORT="${EPICS_CA_SERVER_PORT:-5064}"
export EPICS_CA_REPEATER_PORT="${EPICS_CA_REPEATER_PORT:-5065}"
export EPICS_CA_AUTO_ADDR_LIST="${EPICS_CA_AUTO_ADDR_LIST:-NO}"
export EPICS_CA_ADDR_LIST="${EPICS_CA_ADDR_LIST:-10.66.59.30 10.66.59.255}"

# Server-side caproto/Channel Access settings.
export EPICS_CAS_AUTO_BEACON_ADDR_LIST="${EPICS_CAS_AUTO_BEACON_ADDR_LIST:-NO}"
export EPICS_CAS_BEACON_ADDR_LIST="${EPICS_CAS_BEACON_ADDR_LIST:-10.66.59.255}"
export EPICS_CAS_INTF_ADDR_LIST="${EPICS_CAS_INTF_ADDR_LIST:-10.66.59.30}"

if command -v pixi >/dev/null 2>&1; then
    exec pixi run ioc
fi

if [ -x "${REPO_DIR}/.pixi/envs/default/bin/python" ]; then
    exec "${REPO_DIR}/.pixi/envs/default/bin/python" hiden/cap2_aj2.py
fi

echo "ERROR: pixi was not found and .pixi/envs/default/bin/python is missing." >&2
echo "Run 'pixi install' in ${REPO_DIR}, or make pixi available to softioc-iss." >&2
exit 1
