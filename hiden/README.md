# Hiden MASsoft IOC: Operator Guide

This guide documents the production pair:

- `massoft_client_aj2.py`
- `cap2_aj2.py`

## 0) Runtime JSON config

The client/IOC read runtime settings from:

- `hiden/hiden_config.json`

Override config path with:

```bash
export HIDEN_CONFIG=/path/to/my_hiden_config.json
```

or on Windows:

```powershell
$env:HIDEN_CONFIG='C:\path\to\my_hiden_config.json'
```

Keys used by code:

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
- `ioc.default_go_od`
- `ioc.default_go_ot`
- `ioc.default_go_filename`
- `ioc.update_period_s`
- `ioc.default_data_cycles`
- `ioc.default_data_time_fmt`
- `ioc.default_data_ms_fmt`

Other keys in the file (for example `archiver.*`, `epics.*`) are stored for operator tuning/reference and can be consumed by external tools/scripts.

## 1) Start the IOC

```bash
cd /path/to/repo/Hiden
python hiden/cap2_aj2.py --list-pvs -v
```

The IOC should keep running in this terminal.

## 2) Configure EPICS CA networking

### RHEL 8 IOC server (controls support settings)

```bash
export EPICS_CA_AUTO_ADDR_LIST=no
export EPICS_CAS_AUTO_BEACON_ADDR_LIST=no
export EPICS_CAS_BEACON_ADDR_LIST=10.66.59.255
export EPICS_CA_ADDR_LIST=10.66.59.255
```

### Windows client/testing shell (example)

```powershell
$env:EPICS_CA_ADDR_LIST="127.0.0.1 10.66.56.225"
$env:EPICS_CA_AUTO_ADDR_LIST="NO"
$env:EPICS_CA_SERVER_PORT="5064"
$env:EPICS_CA_REPEATER_PORT="5065"
```

## 3) Core operating sequence

### Open experiment

```bash
caput -S XF:08IDB-SE{RGA:1}:ExpName "file56.exp" # Stages the file to be open. This is just an example. Any file name can be called as soon as exists in folder
caput XF:08IDB-SE{RGA:1}:View 1 # Window view number in MASsoft of data to be streamed
caput XF:08IDB-SE{RGA:1}:OpenExp 1 # Opens the staged file
caget XF:08IDB-SE{RGA:1}:Connected # Should return [1] for connected state and [0] for disconnected state
caget -S XF:08IDB-SE{RGA:1}:Status # Returns present status of the unit
caget -S XF:08IDB-SE{RGA:1}:LastError # Return last error registered. Return [] if no error since last session
```

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:ExpName' 'file56.exp' # Stages the file to be open. This is just an example. Any file name can be called as soon as exists in folder
caproto-put 'XF:08IDB-SE{RGA:1}:View' 1 # Window view number in MASsoft of data to be streamed
caproto-put 'XF:08IDB-SE{RGA:1}:OpenExp' 1 # Opens the staged file
caproto-get 'XF:08IDB-SE{RGA:1}:Connected' # Should return [1] for connected state and [0] for disconnected state
caproto-get -S 'XF:08IDB-SE{RGA:1}:Status' 'XF:08IDB-SE{RGA:1}:LastError' # Return last error registered. Return [] if no error since last session
```

### Start scan and publish PV updates (starts links lazily)

```bash
caput XF:08IDB-SE{RGA:1}:Go 1 # Default open mode. It creates a new file based on the initial template with a date-time subfolder/file naming
# caput XF:08IDB-SE{RGA:1}:GoOD 1 # Alternative modes of opening files.
# caput XF:08IDB-SE{RGA:1}:GoOT 1 # Alternative modes of opening files.
caput XF:08IDB-SE{RGA:1}:Acquire 1 # Triggers the data parsing and PVs creation
```

```powershell
caproto-put 'XF:08IDB-SE{RGA:1}:Go' 1 # Default open mode. It creates a new file based on the initial template with a date-time subfolder/file naming
# caproto-put 'XF:08IDB-SE{RGA:1}:GoOD' 1 # Alternative modes of opening files.
# caproto-put 'XF:08IDB-SE{RGA:1}:GoOT' 1 # Alternative modes of opening files.
caproto-put 'XF:08IDB-SE{RGA:1}:Acquire' 1 # Triggers the data parsing and PVs creation
```

### Monitor live data

```bash
camonitor -S XF:08IDB-SE{RGA:1}:Status # Reports the status of the unit
camonitor XF:08IDB-SE{RGA:1}:DataAge # Time stamp of the last data cycle
camonitor XF:08IDB-SE{RGA:1}P:MID1-I # Example of monitored PV
camonitor -S XF:08IDB-SE{RGA:1}:DataRawLine # Latest raw data line
camonitor -S XF:08IDB-SE{RGA:1}:LastError # Checking for errors
```

```powershell
caproto-monitor 'XF:08IDB-SE{RGA:1}:Status' 'XF:08IDB-SE{RGA:1}:DataAge' 'XF:08IDB-SE{RGA:1}P:MID1-I' 'XF:08IDB-SE{RGA:1}:DataRawLine' 'XF:08IDB-SE{RGA:1}:LastError'
```

Expected:

- `Status` transitions to `StartingActive` then `ScanningActive`.
- `MID1-I` and other MID channels update continuously.
- `DataAge` is low and changing (not `-1` once data is flowing).
- `LastError` remains empty.

## 4) Extended generic controls

### Raw command path

```bash
caput -S XF:08IDB-SE{RGA:1}:RawCmd "-xFilename"
caput XF:08IDB-SE{RGA:1}:RawSend 1
caget -S XF:08IDB-SE{RGA:1}:RawResp
caget -S XF:08IDB-SE{RGA:1}:ActiveFile
```

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:RawCmd' '-xFilename'
caproto-put 'XF:08IDB-SE{RGA:1}:RawSend' 1
caproto-get -S 'XF:08IDB-SE{RGA:1}:RawResp' 'XF:08IDB-SE{RGA:1}:ActiveFile'
```

### Generic `-x*` path

```bash
caput -S XF:08IDB-SE{RGA:1}:XName "Status"
caput XF:08IDB-SE{RGA:1}:XSend 1
caget -S XF:08IDB-SE{RGA:1}:XResp
```

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:XName' 'Status'
caproto-put 'XF:08IDB-SE{RGA:1}:XSend' 1
caproto-get -S 'XF:08IDB-SE{RGA:1}:XResp'
```

### Generic one-shot `-l*` path

```bash
caput -S XF:08IDB-SE{RGA:1}:LItem "Data"
caput XF:08IDB-SE{RGA:1}:LView 1
caput XF:08IDB-SE{RGA:1}:LFetch 1
caget -S XF:08IDB-SE{RGA:1}:LResp
```

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:LItem' 'Data'
caproto-put 'XF:08IDB-SE{RGA:1}:LView' 1
caproto-put 'XF:08IDB-SE{RGA:1}:LFetch' 1
caproto-get -S 'XF:08IDB-SE{RGA:1}:LResp'
```

### Restart hot-links

```bash
caput XF:08IDB-SE{RGA:1}:RestartLinks 1
```

```powershell
caproto-put 'XF:08IDB-SE{RGA:1}:RestartLinks' 1
```

## 5) Shutdown sequence

```bash
caput XF:08IDB-SE{RGA:1}:Abort 1
caput XF:08IDB-SE{RGA:1}:Close 1
caget XF:08IDB-SE{RGA:1}:Connected
caget -S XF:08IDB-SE{RGA:1}:Status
```

```powershell
caproto-put 'XF:08IDB-SE{RGA:1}:Abort' 1
caproto-put 'XF:08IDB-SE{RGA:1}:Close' 1
caproto-get 'XF:08IDB-SE{RGA:1}:Connected'
caproto-get -S 'XF:08IDB-SE{RGA:1}:Status'
```

Expected:

- `Connected` becomes `0`
- `Status` becomes `Disconnected`

## 6) Notes and common pitfalls

- PV names in terminal commands use **single braces**: `XF:08IDB-SE{RGA:1}:...`
- `OpenExp`, `Go`, `Abort`, `Close`, `RestartLinks`, `RawSend`, `XSend`, `LFetch`, `RefreshFile` are momentary command PVs and typically show `0 -> 0`.
- `caproto-get -S` is for string decoding. For integer PVs (like `Connected`), use plain `caproto-get`.
- If a `caproto-put` data value starts with `-`, add `--` before data:

```powershell
caproto-put -S 'XF:08IDB-SE{RGA:1}:XArgs' -- '-Odt'
caproto-put -S 'XF:08IDB-SE{RGA:1}:LOpts' -- '-c1 -t0 -m0'
```

```bash
caput -S XF:08IDB-SE{RGA:1}:RawCmd -- "-xFilename"
```
