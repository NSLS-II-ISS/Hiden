# Hiden RGA IOC

One IOC, two ways to launch it: **`hiden/cap3.py`** uses
**`hiden/massoft_client.py`**, either through Pixi on IOC2 or directly with Python.
Direct-Python setup and implementation notes are in `hiden/README.md`.

Release candidate: **1.0.0-rc.2**. This consolidates the implementations; it is
**not yet approved for unattended production**. A newly opened MASsoft data link
can replay old measurements which the IOC timestamps at receipt. A small
`DataAge` therefore does not prove current measurement data. Resolve and validate
this before production sign-off; clock synchronization alone does not fix it.
See `RELEASE_REVIEW.md` for the outstanding gates.

## Project Layout And Migration

- `hiden/cap3.py`: all 78 PVs, 20 MID/mass pairs, serialized lifecycle and publication.
- `hiden/massoft_client.py`: unified command, status, data and diagnostic client API.
- `hiden/massoft_protocol.py`: shared configuration, CRLF transport and strict parsing.
- `hiden/hiden_config.json`: MASsoft target and IOC defaults.
- `st.cmd`, `config`, `pixi.toml`, `pixi.lock`: IOC2 launch and managed deployment inputs.
- `archiver-pvs.txt`: the unchanged 40 mass/intensity PV names.
- `scripts/configure-massoft-time.ps1`: guarded VM NTP configuration/verification.
- `tests/`: loopback, CA, pre-consolidation contract and mocked time-helper tests.

The obsolete `cap2.py`, `cap2_aj2.py`, and `massoft_client_aj2.py` files are removed,
not kept as parallel implementations or wrappers. Update custom launch commands
to `cap3.py` and imports to `massoft_client`. Pixi and `st.cmd` use the new entry
point automatically. All former extended PVs are available in direct Python too;
commissioning commands remain disabled by default. Existing PV names, defaults,
types and access permissions are unchanged from the extended IOC.

Deploy the complete `hiden` directory. Stop the previous Hiden IOC before starting
the new one; do not run duplicate PV servers. Historical code remains in Git.
Local `analysis/` reports and `rga-comparison-*.json` exports are preserved and
ignored, not release inputs. Do not use `git clean -fdx`: it would delete these
diagnostics as well as local environments and logs.

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
python cap3.py
```

with working directory:

```bash
hiden
```

Start the IOC on `xf08idb-ioc2` (run only one Hiden IOC, including any legacy managed instance):

```bash
cd /nsls2/auto-storage/iss/shared/config/repos/Hiden
pixi install --locked
bash st.cmd
```

Leave the terminal open for this manual run. `pixi run --locked ioc` uses the same
interface setting. The production entry point requires Pixi on PATH, or an
absolute executable path in `PIXI_BIN`. It fails instead of silently launching a
stale environment when Pixi is unavailable. Do not run `pixi update` on a deployed IOC.

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
pixi run --locked python hiden/cap3.py --list-pvs
```

Override the MASsoft target if needed:

```bash
pixi run --locked python hiden/cap3.py --mas-host 10.66.58.227 --mas-port 5026
```

## Pixi Environment

The runtime is locked to the Python 3.13 series and caproto 1.3.0. `pixi.lock`
is tracked; `st.cmd` uses `--locked` so a manifest/lock mismatch fails explicitly.
The lockfile targets Linux; Windows development tests use `requirements-dev.txt`.

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

## MASsoft VM Clock

The MASsoft VM `XF08ID-WS7` is a workgroup Windows machine. Configure Windows
Time against the approved beamline NTP servers, not an SSH copy of IOC2's clock:
`time1.nsls2.bnl.gov`, `time2.nsls2.bnl.gov`, and `time3.nsls2.bnl.gov`.
Use all three explicit peers in Windows; the support note's `pool` directive is
for chrony, not PowerShell. Do not use accelerator or SDCC fallback servers here.

Open **PowerShell as Administrator on the MASsoft VM**, and run the helper from
this repository's root. It requires Windows PowerShell 5.1 or PowerShell 7 and
does not require Pixi. The default operation only reports configuration/status
and takes five NTP offset samples; it does not change the clock or service:

```powershell
.\scripts\configure-massoft-time.ps1
```

For initial setup or a deliberate repair, first stop the MASsoft experiment
and set the IOC's `Acquire=0`. `Acquire=0` alone does not stop the instrument.
Preview the changes, then apply them only when ready for a possible clock jump:

```powershell
.\scripts\configure-massoft-time.ps1 -Apply -ExperimentStopped -WhatIf
.\scripts\configure-massoft-time.ps1 -Apply -ExperimentStopped
```

`-ExperimentStopped` is the operator's acknowledgement, not an automatic check
of the RGA. The helper refuses another hostname, a non-elevated session, and
application on a domain-joined machine. It prints the previous configuration,
sets W32Time startup to Automatic, installs the three NTP peers in client mode
(`0x8`), restarts W32Time, and requests resynchronization. Service/native-command
failures stop the operation; changes already applied are not automatically
rolled back. It does not change the time zone, VMware providers, firewall,
IOC, or Archiver settings, and does not start/stop MASsoft or issue EPICS writes.
Follow site policy if PowerShell script execution is restricted.

Review the output rather than treating a successful command as proof of accuracy:

- Source must be an approved beamline server, not `Free-running System Clock`.
- Leap indicator must no longer be `3 (not synchronized)`, and the last
  successful synchronization time must be recent.
- Aim for an absolute offset below one second before the next data comparison.
  Recheck after 10-15 minutes and after the next planned reboot.
- If a resync fails, preserve its error output and have support check DNS and
  UDP 123 connectivity. Stripchart and the Windows Time service use different
  source-port behavior, so stripchart alone does not prove service connectivity.
- If drift returns, ask the VM administrators to check VMware host/guest time
  synchronization as well; do not blindly disable time providers.

See [Microsoft's Windows Time tools reference](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings).

On 2026-10-01 the operator applied this NTP configuration on `XF08ID-WS7` and
reported `time1.nsls2.bnl.gov,0x8`, leap indicator 0, stratum 2, a successful
sync at 12:56:13 EDT, and five offset samples of +3.3940 to +3.5440 milliseconds.
This verifies that short interval, not long-term stability. The output also
included root dispersion 7.7627936 seconds and `PeerPoll Interval: 17 (out of
valid range)`; retain those diagnostics for follow-up if they persist. Do not
treat root dispersion as the measured clock offset or tune polling solely from
that peer diagnostic.

Time synchronization does not repair previously archived timestamps or the
separate startup/reconnection history replay. The IOC still timestamps rows
when received, and `DataAge` measures receipt freshness, not measurement age.
Neither this helper nor a clock correction makes the IOC a lossless archive of
every historical MASsoft cycle. Keep the original experiment exports for comparison.

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
caput -c -w 120 'XF:08IDB-SE{RGA:1}:View' 1
caput -c -w 120 'XF:08IDB-SE{RGA:1}:OpenExp' 1
caget XF:08IDB-SE{RGA:1}:Connected
caget -S XF:08IDB-SE{RGA:1}:Status
```

If the experiment is stopped and you intend to start a scan:

```bash
caput -c -w 120 'XF:08IDB-SE{RGA:1}:Go' 1
```

If MASsoft is already running the selected experiment, skip `Go`. Enable IOC data publishing with:

```bash
caput -c -w 120 'XF:08IDB-SE{RGA:1}:Acquire' 1
```

After restarting the IOC, repeat `ExpName`, `View`, `OpenExp`, and `Acquire`; these settings are not restored automatically. `Acquire=0` stops IOC publishing without requesting a MASsoft abort.

For an already-running file with a full Windows path, use doubled backslashes
with EPICS `caput -S` from Bash. Set `View` **before** `OpenExp`:

```bash
caput -c -w 120 'XF:08IDB-SE{RGA:1}:Acquire' 0
caput -S 'XF:08IDB-SE{RGA:1}:ExpName' 'C:\\Users\\xf08id1\\Documents\\Hiden Analytical\\MASsoft\\11\\2026-3-alba-rubio1.exp'
caput -c -w 120 'XF:08IDB-SE{RGA:1}:View' 1
caput -c -w 120 'XF:08IDB-SE{RGA:1}:OpenExp' 1
caget 'XF:08IDB-SE{RGA:1}:Connected'
caget -S 'XF:08IDB-SE{RGA:1}:LastError'
```

Only after `Connected=1` and no error, enable publishing:

```bash
caput -c -w 120 'XF:08IDB-SE{RGA:1}:Acquire' 1
```

`-c` waits for put completion; a momentary PV returning zero is not proof of
hardware success. Inspect `LastError` after each operation. Do not issue `Go`
for an already-running recipe.

Abort or close safely:

```bash
caput -c -w 120 'XF:08IDB-SE{RGA:1}:Abort' 1
caput -c -w 120 'XF:08IDB-SE{RGA:1}:Close' 1
```

The unified IOC also keeps backward-compatible PV controls:

```bash
caput -c -w 120 'XF:08IDB-SE{RGA:1}:RunExp' 1
caput -c -w 120 'XF:08IDB-SE{RGA:1}:AbortExp' 1
caput -c -w 120 'XF:08IDB-SE{RGA:1}:CloseExp' 1
```

Monitor useful readbacks:

```bash
camonitor -S XF:08IDB-SE{RGA:1}:Status
camonitor XF:08IDB-SE{RGA:1}:DataAge
camonitor XF:08IDB-SE{RGA:1}P:MID1-I
camonitor -S XF:08IDB-SE{RGA:1}:DataRawLine
camonitor -S XF:08IDB-SE{RGA:1}:LastError
```

## Commissioning Controls

Commissioning PVs are retained but disabled by default. Temporarily set
`ioc.enable_generic_commands` to JSON `true` and restart only if needed. Stop
`Acquire` first. `RawSend` and `XSend` now allow **Status/Filename queries only**;
use dedicated `Go`, `Abort`, and `Close` controls for hardware operations.
`LFetch` uses a temporary dedicated socket. Reopen the experiment before returning
to acquisition after commissioning. These controls are not an authentication mechanism.

Raw command:

```bash
caput -S XF:08IDB-SE{RGA:1}:RawCmd -- "-xFilename"
caput -c -w 120 'XF:08IDB-SE{RGA:1}:RawSend' 1
caget -S XF:08IDB-SE{RGA:1}:RawResp
```

Generic `-x*` command:

```bash
caput -S XF:08IDB-SE{RGA:1}:XName "Status"
caput -c -w 120 'XF:08IDB-SE{RGA:1}:XSend' 1
caget -S XF:08IDB-SE{RGA:1}:XResp
```

Generic one-shot `-l*` command:

```bash
caput -S XF:08IDB-SE{RGA:1}:LItem "Data"
caput XF:08IDB-SE{RGA:1}:LView 1
caput -c -w 120 'XF:08IDB-SE{RGA:1}:LFetch' 1
caget -S XF:08IDB-SE{RGA:1}:LResp
```

Restart status/data hot-links:

```bash
caput -c -w 120 'XF:08IDB-SE{RGA:1}:RestartLinks' 1
```

## Implementation Notes

`cap3.py` contains the single `RGAIOC` class: lifecycle, all PV definitions,
compatibility controls and commissioning diagnostics. Pixi and direct Python
execute exactly the same implementation; neither depends on an old entry point.

The pixi IOC publishes MID intensity and mass readbacks from `MID1` through `MID20`. Recipes with fewer MID channels leave the unused PVs at `0`.

`massoft_client.py` owns the MASsoft client lifecycle and diagnostic API. It uses one command socket plus dedicated status and data hot-link sockets. A socket that has entered hot-link mode is not reused for normal commands; the client reconnects link sockets before restarting links. This avoids leaving MASsoft in a mixed command/listen state after aborts, restarts, or file changes.

The intended lifecycle is:

```text
OpenExp -> Go or RunExp -> Acquire=1 -> Abort/AbortExp if needed -> Close/CloseExp
```

`OpenExp` prepares the experiment and metadata. `Acquire=1` starts data publishing from hot-links. `Close` and `CloseExp` use safe abort/close sequencing and then disconnect local sockets because MASsoft drops file-associated sockets after `-xClose`.

## Failure Recovery And Data Quality

- **Unresolved history replay:** a new data link can start at the beginning of
  an existing experiment. Received historical rows get current receipt timestamps
  and can enter Archiver as apparently live samples. Starting/restarting links,
  reopening a file, or restarting the IOC must be followed by source-row validation.
  `DataAge`/`DataRawAge` measure receipt age only, not source age. This release does
  not add an undocumented seek option or claim to solve replay.
- A transport/parser/reader failure sets `Acquire=0`, `Connected=0`, `Status=Error`
  and `LastError`, and invalidates the intensity PVs. Set the correct file/view,
  run `OpenExp`, then `Acquire=1` to recover without restarting the IOC.
- A command timeout has an unknown hardware outcome. Check MASsoft before
  repeating a hardware command. The IOC does not replay commands or restart scans.
- Paused or stale intensity readbacks carry an INVALID alarm. `DataAge` tracks
  reception even while publishing is paused. The default stale threshold is
  `ioc.stale_after_s=60`; set it above the longest expected MID cycle plus margin.
  A silent stream produces a stale-data warning, not an automatic abort/reconnect.
- MID views must contain 1..20 species legends, either `mass <number>` or
  `Scan <index> : mass <number>`. Optional leading `Elapsed time` and `Time (ms)`
  headers describe time metadata, not extra MID channels. Masses retain their
  response-column order, regardless of the scan indices. Missing, malformed,
  nonfinite, or ambiguous cells fail visibly rather than shifting mass assignments.
  Negative and zero readings are retained. Unsupported/custom legend formats
  require an explicit parser extension and tests.
- A snapshot gives all intensities in a row the same IOC receive timestamp.
  `update_period_s` controls latest-row publication, not lossless delivery of every
  instrument cycle. A recipe faster than this period can be downsampled.
- Changing the selected view/data options requires `Acquire=0`. Opening a file
  resets old channel values; unused channels are zero, not retained from the prior recipe.
- Shutdown disconnects sockets without stopping the MASsoft scan. Only explicit
  `Abort`/`Close` commands stop hardware. Allow in-flight commands to finish during
  shutdown; do not use `kill -9` during a MASsoft command.

CA controls have no authentication in this repository. Wildcard binding is needed
for the verified Linux broadcast discovery, but also exposes control PVs on INST.
Use beamline firewall/network access policy and a single managed instance. Do not
expose this IOC to untrusted networks.

`archiver-pvs.txt` lists all 40 mass/intensity PVs. Archive entries and the JSON
`archiver` section do not automatically configure the Archiver service or conditional
sampling. Alarm-aware consumers should also monitor `Acquire`, `Connected`, and
`DataAge`. The earlier archive verification above predates this release candidate;
repeat retrieval during commissioning.

## Development Checks

Use Python 3.13. From the repository root, create a virtual environment and install
`requirements-dev.txt`, then run:

```bash
python -m pytest -q
python -m ruff check hiden tests
python -m ruff format --check hiden tests
bash -n st.cmd
pixi lock --check
```

The tests use a loopback MASsoft simulator and isolated CA ports/prefixes; they do
not contact the real RGA. CI runs the tests on Windows and Linux. Logs, caches,
IDE state, and local `hiden/scratch.py` notes are ignored. Historic source snapshots
remain available in Git history, not as alternative executable files.

Windows tests also syntax-check the time helper and mock its service/NTP calls;
they never change the host clock or contact time servers. Runtime cases are
skipped for shells whose execution policy disallows the unsigned script; tests
do not weaken that policy. PowerShell 5.1 and 7 are checked when available.
