# Hiden MASsoft Extended IOC: Operator Guide

This guide documents the tested workflow for:

- `massoft_client_extended.py`
- `cap2_extended.py`

## 0) Runtime JSON config

Both fixed and extended client/IOC variants load runtime settings from:

- `hiden/hiden_config.json`

You can override this file path with environment variable:

```powershell
$env:HIDEN_CONFIG='C:\path\to\my_hiden_config.json'
```

Important keys currently consumed by code:

- `beamline_name`
- `massoft.host`
- `massoft.port`
- `massoft.experiment_directory`
- `massoft.retry_s`
- `massoft.command_timeout_s`
- `massoft.link_chunk_timeout_s`
- `massoft.link_burst_gap_s`
- `massoft.enable_keepalive`
- `ioc.default_experiment`
- `ioc.default_view`
- `ioc.update_period_s`
- `ioc.default_go_od`
- `ioc.default_go_ot`
- `ioc.default_go_filename`
- `ioc.start_links_on_open_exp` (used by fixed IOC only)
- `ioc.default_data_cycles` (extended IOC)
- `ioc.default_data_time_fmt` (extended IOC)
- `ioc.default_data_ms_fmt` (extended IOC)

Other keys in the file (for example `archiver.*`, `epics.*`) are stored for operator tuning/reference and can be consumed by external tools/scripts.

## 1) Start the Extended IOC

```powershell
cd C:\repo\Hiden
python hiden\cap2_extended.py --list-pvs -v
```

The IOC intentionally keeps running in this terminal.

## 2) Configure EPICS CA networking

### RHEL 8 IOC server (recommended by controls support)

```bash
export EPICS_CA_AUTO_ADDR_LIST=no
export EPICS_CAS_AUTO_BEACON_ADDR_LIST=no
export EPICS_CAS_BEACON_ADDR_LIST=10.66.59.255
export EPICS_CA_ADDR_LIST=10.66.59.255
```

### Windows test terminal (local development)

```powershell
$env:EPICS_CA_ADDR_LIST="127.0.0.1 10.66.56.225"
$env:EPICS_CA_AUTO_ADDR_LIST="NO"
$env:EPICS_CA_SERVER_PORT="5064"
$env:EPICS_CA_REPEATER_PORT="5065"
```

## 3) Core workflow (tested)

### Open experiment and links

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:ExpName' 'file56.exp'
caproto-put 'XF:08IDB-SE{RGA:1}:View' 1
caproto-put 'XF:08IDB-SE{RGA:1}:OpenExp' 1
caproto-get 'XF:08IDB-SE{RGA:1}:Connected'
caproto-get -S 'XF:08IDB-SE{RGA:1}:Status' 'XF:08IDB-SE{RGA:1}:LastError'
```

### Start scan and publishing

```powershell
caproto-put 'XF:08IDB-SE{RGA:1}:Go' 1
caproto-put 'XF:08IDB-SE{RGA:1}:Acquire' 1
```

### Monitor live values

```powershell
caproto-monitor 'XF:08IDB-SE{RGA:1}:Status' 'XF:08IDB-SE{RGA:1}:DataAge' 'XF:08IDB-SE{RGA:1}P:MID1-I' 'XF:08IDB-SE{RGA:1}:DataRawLine' 'XF:08IDB-SE{RGA:1}:LastError'
```

Expected:

- `Status` transitions to `StartingActive` then `ScanningActive`.
- `MID1-I` updates continuously.
- `DataAge` remains low (not `-1`).
- `LastError` remains empty.

## 4) Extended generic controls

### Raw command path

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:RawCmd' '-xFilename'
caproto-put 'XF:08IDB-SE{RGA:1}:RawSend' 1
caproto-get -S 'XF:08IDB-SE{RGA:1}:RawResp' 'XF:08IDB-SE{RGA:1}:ActiveFile'
```

### Generic `-x*` path

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:XName' 'Status'
caproto-put 'XF:08IDB-SE{RGA:1}:XSend' 1
caproto-get -S 'XF:08IDB-SE{RGA:1}:XResp'
```

### Generic one-shot `-l*` path

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:LItem' 'Data'
caproto-put 'XF:08IDB-SE{RGA:1}:LView' 1
caproto-put 'XF:08IDB-SE{RGA:1}:LFetch' 1
caproto-get -S 'XF:08IDB-SE{RGA:1}:LResp'
```

### Restart hot-links

```powershell
caproto-put 'XF:08IDB-SE{RGA:1}:RestartLinks' 1
```

## 5) Shutdown

```powershell
caproto-put 'XF:08IDB-SE{RGA:1}:Abort' 1
caproto-put 'XF:08IDB-SE{RGA:1}:Close' 1
caproto-get 'XF:08IDB-SE{RGA:1}:Connected'
caproto-get -S 'XF:08IDB-SE{RGA:1}:Status'
```

## Notes and common pitfalls

- PV names in terminal commands use **single braces**: `XF:08IDB-SE{RGA:1}:...`
- `OpenExp`, `Go`, `Abort`, `Close`, `RestartLinks` are momentary command PVs and typically show `0 -> 0`.
- `caproto-get -S` is for string decoding. For integer PVs (like `Connected`), use plain `caproto-get`.
- If data/monitor commands include values beginning with `-` (for example `-Odt`, `-c1 -t0 -m0`), pass `--` before the value:

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:XArgs' -- '-Odt'
caproto-put -S 'XF:08IDB-SE{RGA:1}:LOpts' -- '-c1 -t0 -m0'
```
