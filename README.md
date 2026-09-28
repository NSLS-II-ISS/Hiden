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

The IOC talks to MASsoft on the INST subnet at `10.66.58.227:5026`. Caproto listens on all IOC2 interfaces (`0.0.0.0`) so it can receive broadcast searches, and sends beacons to `10.66.59.255`. The wildcard binding also makes the CA server reachable on the INST interface.

The pixi task in `pixi.toml` runs:

```bash
python cap2_aj2.py
```

with working directory:

```bash
hiden
```

Start the IOC on `xf08idb-ioc2` (run only one Hiden IOC, including any legacy managed instance):

```bash
cd /nsls2/auto-storage/iss/shared/config/repos/Hiden
bash st.cmd
```

Leave the terminal open for this manual run. `pixi run ioc` is also supported and uses the same interface setting from `pixi.toml`.

## Managed IOC Deployment

The legacy Hiden IOC was integrated into the beamline IOC infrastructure with a root `st.cmd` plus a `config` file:

```text
NAME=hiden
USER=softioc-iss
PORT=5110
HOST=xf08idb-ioc2
```

This repository now includes the same deployment shape. For Ansible/procServ, use the repository root `st.cmd` as the IOC startup script. It sets the EPICS Channel Access environment and then runs the pixi IOC.

The important IOC-side setting is:

```bash
EPICS_CAS_INTF_ADDR_LIST=0.0.0.0
```

This allows broadcast discovery on Linux. Binding only to `10.66.59.30` prevented broadcast searches from reaching this IOC in the IOC2 test. Beacons still go to the EPICS subnet. See [caproto's multiple-server documentation](https://caproto.github.io/caproto/master/servers.html#running-multiple-servers-on-one-host).

`PORT=5110` is the managed console port, not the Channel Access or MASsoft port. Adding these files does not itself register or start a managed service; controls staff must configure that deployment.

Log PV names while starting the server:

```bash
pixi run python hiden/cap2_aj2.py --list-pvs
```

Override the MASsoft target if needed:

```bash
pixi run python hiden/cap2_aj2.py --mas-host 10.66.58.227 --mas-port 5026
```

## Pixi Environment

The EPICS Channel Access variables are set in `pixi.toml` under `[activation.env]`:

```toml
EPICS_CAS_AUTO_BEACON_ADDR_LIST = "NO"
EPICS_CAS_BEACON_ADDR_LIST = "10.66.59.255"
EPICS_CAS_INTF_ADDR_LIST = "0.0.0.0"
```

`st.cmd` additionally supplies default CA search/repeater ports (`5064`/`5065`) and a client broadcast address. The JSON `epics` section is not automatically exported into the process environment. Command-line `--interfaces` takes precedence over the interface environment setting.

Runtime MASsoft and IOC defaults come from:

```bash
hiden/hiden_config.json
```

To use a different config:

```bash
export HIDEN_CONFIG=/path/to/hiden_config.json
pixi run ioc
```

## Workstation Access

On a workstation or `epics-services-iss` connected to the EPICS subnet:

```bash
export EPICS_CA_ADDR_LIST="10.66.59.255"
export EPICS_CA_AUTO_ADDR_LIST=NO
export EPICS_CA_SERVER_PORT=5064
export EPICS_CA_REPEATER_PORT=5065

cainfo 'XF:08IDB-VA{RGA:1}Mass:MID1'
caget 'XF:08IDB-VA{RGA:1}Mass:MID1' 'XF:08IDB-SE{RGA:1}P:MID1-I'
camonitor 'XF:08IDB-SE{RGA:1}P:MID1-I' 'XF:08IDB-SE{RGA:1}:DataAge'
```

This searches all IOCs on the EPICS subnet, including other beamline equipment. Preserve any additional broadcast or gateway addresses your site needs in `EPICS_CA_ADDR_LIST`. Avoid using only `10.66.59.30`: with multiple IOCs sharing UDP port 5064, a unicast search may reach a different IOC. Client terminals do not need `EPICS_CAS_*` variables, and shell exports do not configure the running Archiver service.

## Archiver Discovery

The 2026-09-25 test from `epics-services-iss` found that broadcast-only discovery failed with the `10.66.59.30` binding and succeeded after switching to `0.0.0.0`. Archiver then reported `Connected=true` and `Monitored=true` for `Mass:MID1` through `Mass:MID10`. On 2026-09-28, HTTP retrieval of `P:MID1-I` returned archived samples matching all eight values and timestamps in the supplied `camonitor` interval (10:39:36 through 10:39:49 EDT). This verified IOC publishing, Archiver reception, and historical retrieval without an Archiver service change.

Repeat this test without depending on the shell's exported address list:

```bash
env EPICS_CA_ADDR_LIST=10.66.59.255 EPICS_CA_AUTO_ADDR_LIST=NO EPICS_CA_SERVER_PORT=5064 cainfo -w 5 'XF:08IDB-VA{RGA:1}Mass:MID1'
```

The reported host/port should match the current IOC. An automatically selected TCP port is normal when another IOC already uses TCP 5064; searches still use UDP 5064.

After opening the experiment and enabling `Acquire`, check `Connected=true` and retrieve recent samples or a recent chart for an intensity PV in Archiver. A constant mass PV may not produce frequent value-change events. If discovery succeeds but archiving remains disconnected, inspect the engine's effective search configuration and logs. Do not delete existing archive entries to troubleshoot discovery.

## Archived Data Retrieval

Quick charts were unavailable during verification, but the HTTP data API worked. The appliance's `getApplianceInfo` response supplied this `dataRetrievalURL`:

```text
http://epics-services-iss.nsls2.bnl.local:19268/retrieval
```

From a machine on the beamline network, retrieve the verified example interval (2026-09-28 10:39:30-10:40:00 EDT, or 14:39:30-14:40:00 UTC):

```bash
curl -fsS --max-time 30 --get 'http://epics-services-iss.nsls2.bnl.local:19268/retrieval/data/getData.json' --data-urlencode 'pv=XF:08IDB-SE{RGA:1}P:MID1-I' --data-urlencode 'from=2026-09-28T14:39:30.000Z' --data-urlencode 'to=2026-09-28T14:40:00.000Z'
```

In PowerShell, use the same command with `curl.exe` instead of `curl`. Adjust `pv`, `from`, and `to` for subsequent tests. Times ending in `Z` are UTC. The JSON contains a `data` array with `secs`, `nanos`, and `val` for each sample. For example, the archived value `6.66498e-10` at 10:39:36.120040 EDT matched the live monitor exactly.

Use `dataRetrievalURL` for samples, not the separate `retrievalURL` ending in `/bpl` (which advertised `localhost` in this deployment). If rediscovering the endpoint through the HTTPS management site, open `getApplianceInfo` in an authenticated browser; terminal `curl` does not inherit that browser's BNL login session. See the [Archiver retrieval API guide](https://epicsarchiver.readthedocs.io/en/latest/reader/guides/fetch-data.html).

## Operating Sequence

Open the experiment template:

```bash
caput -S XF:08IDB-SE{RGA:1}:ExpName "file56.exp"
caput XF:08IDB-SE{RGA:1}:View 1
caput XF:08IDB-SE{RGA:1}:OpenExp 1
caget XF:08IDB-SE{RGA:1}:Connected
caget -S XF:08IDB-SE{RGA:1}:Status
```

If the experiment is stopped and you intend to start a scan:

```bash
caput XF:08IDB-SE{RGA:1}:Go 1
```

If MASsoft is already running the selected experiment, skip `Go`. Enable IOC data publishing with:

```bash
caput XF:08IDB-SE{RGA:1}:Acquire 1
```

After restarting the IOC, repeat `ExpName`, `View`, `OpenExp`, and `Acquire`; these settings are not restored automatically. `Acquire=0` stops IOC publishing without requesting a MASsoft abort.

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

`cap2_aj2.py` is the pixi production IOC wrapper. It exposes the PVs, keeps `OpenExp`, `Go`, `Abort`, and `Close` as momentary controls, uses `Acquire` as a persistent 0/1 publishing control, and adds diagnostic/generic command PVs for commissioning.

The pixi IOC publishes MID intensity and mass readbacks from `MID1` through `MID20`. Recipes with fewer MID channels leave the unused PVs at `0`.

`massoft_client_aj2.py` owns the MASsoft protocol work. It uses one command socket plus dedicated status and data hot-link sockets. A socket that has entered hot-link mode is not reused for normal commands; the client reconnects link sockets before restarting links. This avoids leaving MASsoft in a mixed command/listen state after aborts, restarts, or file changes.

The intended lifecycle is:

```text
OpenExp -> Go or RunExp -> Acquire=1 -> Abort/AbortExp if needed -> Close/CloseExp
```

`OpenExp` prepares the experiment and metadata. `Acquire=1` starts data publishing from hot-links. `Close` and `CloseExp` use safe abort/close sequencing and then disconnect local sockets because MASsoft drops file-associated sockets after `-xClose`.
