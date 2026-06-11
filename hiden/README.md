# Direct Python IOC

This README documents the direct Python option:

- `cap2.py`
- `massoft_client.py`

Use the repository root `README.md` for the pixi option:

- `cap2_aj2.py`
- `massoft_client_aj2.py`

## When To Use This Option

Use `cap2.py` when you want the smaller IOC implementation without pixi task management or the extended `_aj2` commissioning PVs.

This option still reads `hiden_config.json`, exposes the core Hiden RGA PVs, and uses MASsoft sockets through `massoft_client.py`.

## Runtime Config

Default config file:

```bash
hiden/hiden_config.json
```

Override it with:

```bash
export HIDEN_CONFIG=/path/to/hiden_config.json
```

PowerShell:

```powershell
$env:HIDEN_CONFIG = "C:\path\to\hiden_config.json"
```

Important config keys:

```text
massoft.host
massoft.port
massoft.experiment_directory
massoft.retry_s
massoft.command_timeout_s
massoft.link_chunk_timeout_s
massoft.link_burst_gap_s
massoft.enable_keepalive
ioc.default_experiment
ioc.default_view
ioc.update_period_s
ioc.start_links_on_open_exp
```

## Start The IOC

From the repository root:

```bash
python hiden/cap2.py
```

List PVs:

```bash
python hiden/cap2.py --list-pvs -q
```

If you are using pixi only for dependencies, but still want the direct IOC code path:

```bash
pixi run python hiden/cap2.py
```

## EPICS Environment

For bash on the IOC host:

```bash
export EPICS_CA_SERVER_PORT="5064"
export EPICS_CA_REPEATER_PORT="5065"
export EPICS_CA_ADDR_LIST="10.66.59.227"
export EPICS_CA_AUTO_ADDR_LIST="NO"
export EPICS_CAS_AUTO_BEACON_ADDR_LIST="NO"
export EPICS_CAS_BEACON_ADDR_LIST="10.66.59.227"
```

For PowerShell:

```powershell
$env:EPICS_CA_SERVER_PORT = "5064"
$env:EPICS_CA_REPEATER_PORT = "5065"
$env:EPICS_CA_ADDR_LIST = "10.66.59.227"
$env:EPICS_CA_AUTO_ADDR_LIST = "NO"
$env:EPICS_CAS_AUTO_BEACON_ADDR_LIST = "NO"
$env:EPICS_CAS_BEACON_ADDR_LIST = "10.66.59.227"
```

## Core PVs

Experiment and scan controls:

```text
XF:08IDB-SE{RGA:1}:ExpName
XF:08IDB-SE{RGA:1}:View
XF:08IDB-SE{RGA:1}:OpenExp
XF:08IDB-SE{RGA:1}:Go
XF:08IDB-SE{RGA:1}:Abort
XF:08IDB-SE{RGA:1}:Close
XF:08IDB-SE{RGA:1}:Acquire
```

Diagnostics:

```text
XF:08IDB-SE{RGA:1}:Connected
XF:08IDB-SE{RGA:1}:Status
XF:08IDB-SE{RGA:1}:LastError
XF:08IDB-SE{RGA:1}:DataAge
XF:08IDB-SE{RGA:1}:StatusAge
```

Data readbacks:

```text
XF:08IDB-SE{RGA:1}P:MID1-I ... XF:08IDB-SE{RGA:1}P:MID10-I
XF:08IDB-VA{RGA:1}Mass:MID1 ... XF:08IDB-VA{RGA:1}Mass:MID10
```

## Operating Sequence

Open the experiment:

```bash
caput -S XF:08IDB-SE{RGA:1}:ExpName "file56.exp"
caput XF:08IDB-SE{RGA:1}:View 1
caput XF:08IDB-SE{RGA:1}:OpenExp 1
caget XF:08IDB-SE{RGA:1}:Connected
caget -S XF:08IDB-SE{RGA:1}:Status
caget -S XF:08IDB-SE{RGA:1}:LastError
```

Start the MASsoft scan:

```bash
caput XF:08IDB-SE{RGA:1}:Go 1
```

Start publishing IOC data from MASsoft hot-links:

```bash
caput XF:08IDB-SE{RGA:1}:Acquire 1
```

Stop publishing without closing the MASsoft experiment:

```bash
caput XF:08IDB-SE{RGA:1}:Acquire 0
```

Abort a running scan safely:

```bash
caput XF:08IDB-SE{RGA:1}:Abort 1
caget -S XF:08IDB-SE{RGA:1}:Status
```

Close the experiment and disconnect sockets:

```bash
caput XF:08IDB-SE{RGA:1}:Close 1
caget XF:08IDB-SE{RGA:1}:Connected
```

## Monitoring

```bash
camonitor -S XF:08IDB-SE{RGA:1}:Status
camonitor XF:08IDB-SE{RGA:1}:DataAge
camonitor XF:08IDB-SE{RGA:1}P:MID1-I
camonitor XF:08IDB-SE{RGA:1}P:MID2-I
camonitor -S XF:08IDB-SE{RGA:1}:LastError
```

## Implementation Notes

`cap2.py` is the direct Caproto IOC. It defines PVs, handles user puts, and publishes the latest parsed MASsoft data into EPICS PVs.

`massoft_client.py` is the MASsoft socket client. It follows the MASsoft TCP/IP rule that each command must receive its response before the next command is sent. It also uses dedicated sockets for hot-links, because a socket that has started `-lStatus` or `-lData` should only be read from after that point.

The direct IOC lifecycle is:

```text
OpenExp -> Go -> Acquire=1 -> Abort if needed -> Close
```

`OpenExp` connects sockets, associates the experiment file, and reads legends for the mass PVs. `Acquire=1` starts status/data hot-links if they were not already started. `Abort` sends `-xAbort` and waits for a `Stopped*` status. `Close` stops IOC publishing first, then aborts if needed, sends `-xClose`, and marks the IOC disconnected.

The direct option does not expose the `_aj2` generic commissioning PVs such as `RawCmd`, `XName`, `LItem`, `RestartLinks`, or `DataRawLine`.
