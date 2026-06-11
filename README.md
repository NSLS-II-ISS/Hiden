# Hiden RGA IOC

This repository keeps two runnable IOC options:

- Direct Python option: `hiden/cap2.py` with `hiden/massoft_client.py`. See `hiden/README.md`.
- Pixi option: `hiden/cap2_aj2.py` with `hiden/massoft_client_aj2.py`. This README documents that path.

## Pixi IOC Option

Use this option when running from the managed pixi environment on the IOC host.

Network topology:

```text
IOC server INST:       10.66.58.30
IOC server EPICS:      10.66.59.30
MASsoft Windows INST:  10.66.58.227:5026
MASsoft Windows EPICS: 10.66.59.227
EPICS broadcast:       10.66.59.255
```

The IOC talks to MASsoft on the INST subnet at `10.66.58.227:5026`. Caproto publishes PVs on the EPICS subnet and sends beacons to `10.66.59.255`.

The pixi task in `pixi.toml` runs:

```bash
python cap2_aj2.py
```

with working directory:

```bash
hiden
```

Start the IOC:

```bash
cd /path/to/repo/Hiden
pixi run ioc
```

List PVs without starting normal operation:

```bash
pixi run python hiden/cap2_aj2.py --list-pvs -q
```

Override the MASsoft target if needed:

```bash
pixi run python hiden/cap2_aj2.py --mas-host 10.66.58.227 --mas-port 5026
```

## Pixi Environment

The EPICS Channel Access variables are set in `pixi.toml` under `[activation.env]`:

```toml
EPICS_CA_SERVER_PORT = "5064"
EPICS_CA_REPEATER_PORT = "5065"
EPICS_CA_ADDR_LIST = "10.66.59.255"
EPICS_CA_AUTO_ADDR_LIST = "NO"
EPICS_CAS_AUTO_BEACON_ADDR_LIST = "NO"
EPICS_CAS_BEACON_ADDR_LIST = "10.66.59.255"
```

Runtime MASsoft and IOC defaults come from:

```bash
hiden/hiden_config.json
```

To use a different config:

```bash
export HIDEN_CONFIG=/path/to/hiden_config.json
pixi run ioc
```

## Operating Sequence

Open the experiment template:

```bash
caput -S XF:08IDB-SE{RGA:1}:ExpName "file56.exp"
caput XF:08IDB-SE{RGA:1}:View 1
caput XF:08IDB-SE{RGA:1}:OpenExp 1
caget XF:08IDB-SE{RGA:1}:Connected
caget -S XF:08IDB-SE{RGA:1}:Status
```

Start scan and publish data:

```bash
caput XF:08IDB-SE{RGA:1}:Go 1
caput XF:08IDB-SE{RGA:1}:Acquire 1
```

Abort or close safely:

```bash
caput XF:08IDB-SE{RGA:1}:Abort 1
caput XF:08IDB-SE{RGA:1}:Close 1
```

The `_aj2` IOC also keeps backward-compatible controls:

```bash
caput XF:08IDB-SE{RGA:1}:RunExp 1
caput XF:08IDB-SE{RGA:1}:AbortExp 1
caput XF:08IDB-SE{RGA:1}:CloseExp 1
```

Monitor useful readbacks:

```bash
camonitor -S XF:08IDB-SE{RGA:1}:Status
camonitor XF:08IDB-SE{RGA:1}:DataAge
camonitor XF:08IDB-SE{RGA:1}P:MID1-I
camonitor -S XF:08IDB-SE{RGA:1}:DataRawLine
camonitor -S XF:08IDB-SE{RGA:1}:LastError
```

## Extended `_aj2` Controls

Raw command:

```bash
caput -S XF:08IDB-SE{RGA:1}:RawCmd -- "-xFilename"
caput XF:08IDB-SE{RGA:1}:RawSend 1
caget -S XF:08IDB-SE{RGA:1}:RawResp
```

Generic `-x*` command:

```bash
caput -S XF:08IDB-SE{RGA:1}:XName "Status"
caput XF:08IDB-SE{RGA:1}:XSend 1
caget -S XF:08IDB-SE{RGA:1}:XResp
```

Generic one-shot `-l*` command:

```bash
caput -S XF:08IDB-SE{RGA:1}:LItem "Data"
caput XF:08IDB-SE{RGA:1}:LView 1
caput XF:08IDB-SE{RGA:1}:LFetch 1
caget -S XF:08IDB-SE{RGA:1}:LResp
```

Restart status/data hot-links:

```bash
caput XF:08IDB-SE{RGA:1}:RestartLinks 1
```

## Implementation Notes

`cap2_aj2.py` is the pixi production IOC wrapper. It exposes the PVs, keeps `OpenExp`, `Go`, `Abort`, `Close`, and `Acquire` as momentary controls, and adds diagnostic/generic command PVs for commissioning.

`massoft_client_aj2.py` owns the MASsoft protocol work. It uses one command socket plus dedicated status and data hot-link sockets. A socket that has entered hot-link mode is not reused for normal commands; the client reconnects link sockets before restarting links. This avoids leaving MASsoft in a mixed command/listen state after aborts, restarts, or file changes.

The intended lifecycle is:

```text
OpenExp -> Go or RunExp -> Acquire=1 -> Abort/AbortExp if needed -> Close/CloseExp
```

`OpenExp` prepares the experiment and metadata. `Acquire=1` starts data publishing from hot-links. `Close` and `CloseExp` use safe abort/close sequencing and then disconnect local sockets because MASsoft drops file-associated sockets after `-xClose`.
