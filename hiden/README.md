# Unified IOC: Direct Python

`cap3.py` is the single IOC implementation, with `massoft_client.py` and
`massoft_protocol.py` and `massoft_timing.py`. Direct Python and Pixi expose the same
87 PVs (78 original plus 9 source-quality PVs), including
20 mass/intensity pairs and the previously extended diagnostics. The old IOC
entry points and `_aj2` client module have been removed. Use `massoft_client.py`
(with an underscore, not a space) for Python imports.

For IOC2's Pixi/managed launcher, workstation access, operating commands and
Archiver retrieval, see the [root README](../README.md). Deploy this whole
`hiden` directory, not an isolated script. Run exactly one Hiden IOC.

**rc.3: commissioning candidate, not unattended-production approval.** New links
can replay historical rows. The IOC now requires a verified `SourceStartUTC`,
withholds old rows and uses source-derived MID timestamps. Follow the
[guarded-acquisition trial](../docs/GUARDED_ACQUISITION.md) and
[release review](../RELEASE_REVIEW.md); DataAge alone does not prove freshness.

## Direct Python Setup

From the repository root, using Python 3.13:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set the IOC environment on **IOC2**, then launch:

```bash
export EPICS_CA_SERVER_PORT=5064
export EPICS_CA_REPEATER_PORT=5065
export EPICS_CA_ADDR_LIST=10.66.59.255
export EPICS_CA_AUTO_ADDR_LIST=NO
export EPICS_CAS_AUTO_BEACON_ADDR_LIST=NO
export EPICS_CAS_BEACON_ADDR_LIST=10.66.59.255
export EPICS_CAS_INTF_ADDR_LIST=0.0.0.0
python hiden/cap3.py
```

MASsoft remains at `10.66.58.227:5026` on INST. CA searches use UDP 5064;
beacons go to `10.66.59.255`. Do not change the MASsoft port to 5064.
Binding to all interfaces preserves the broadcast discovery verified on IOC2,
but exposes controls on INST as well. Follow beamline network access policy.
Workstation clients need only CA search settings, not the IOC's `EPICS_CAS_*`.

Useful launch options (from the repository root):

```bash
python hiden/cap3.py --help
python hiden/cap3.py --list-pvs
python hiden/cap3.py --mas-host 10.66.58.227 --mas-port 5026
```

`--list-pvs` also starts the server; it is not a dry-run option. Do not run it
alongside another instance. Pixi is an alternative environment for the very same
code: `pixi run --locked python hiden/cap3.py`. The recommended IOC2 launcher is
`bash st.cmd`, which supplies the EPICS defaults automatically.

For Windows development, create the environment with `py -3.13 -m venv .venv`
and use `.\.venv\Scripts\python.exe` directly. Set any required variables with
PowerShell's `$env:NAME = 'value'`. Do not launch a second beamline PV server
from the Windows VM or workstation while the IOC2 instance is running.

## Configuration

Defaults are read from `hiden_config.json` beside the modules, independent of the
current directory. To select a different file before launch:

```bash
export HIDEN_CONFIG=/path/to/hiden_config.json
```

```powershell
$env:HIDEN_CONFIG = 'C:\path\to\hiden_config.json'
```

Important keys:

- `massoft.host`, `massoft.port`, `massoft.experiment_directory`: MASsoft endpoint/path.
- `massoft.retry_s`, `massoft.command_timeout_s`: server wait option and bounded client timeout.
- `massoft.link_chunk_timeout_s`, `massoft.link_burst_gap_s`: stream read timing.
- `ioc.default_experiment`, `ioc.default_view`: initial file/view selection, not automatic connection.
- `ioc.update_period_s`: live FIFO drain interval (up to 64 rows/pass), not guaranteed archival delivery.
- `ioc.stale_after_s`: receipt silence threshold; set above the longest normal cycle.
- `ioc.start_links_on_open_exp`: must be zero; enter the time reference after OpenExp.
- `ioc.default_data_cycles`, `ioc.default_data_time_fmt`, `ioc.default_data_ms_fmt`: link options.
- `ioc.max_source_age_s`: default SourceMaxAge=10 seconds; commission for the recipe.
- `ioc.data_queue_size`: eligible live FIFO capacity, default 256; overflow faults acquisition.
- `ioc.enable_generic_commands`: defaults to false; leave off during normal operation.

The JSON `epics`/`archiver` sections do not configure OS environment or the
Archiver service. See [MASsoft VM clock setup](../README.md#massoft-vm-clock)
for the guarded NTP helper and the verified NSLS-II sources. Clock changes do
not repair existing archived timestamps or the data-link replay issue.

Keep DataCycles=1 for commissioning; DataMsFmt must be 1. Increasing batch size
does not select the newest row and can delay delivery of live measurements.

## Implementation

`cap3.RGAIOC` owns PV definitions, readback publication and one asynchronous
operation lock. All potentially blocking MASsoft operations run in a worker;
shutdown waits for an in-flight command rather than closing its socket midway.

`massoft_client.MASsoftClient` owns one command socket and separate status/data
hot-links, plus temporary diagnostic sockets. `massoft_protocol` supplies CRLF
framing, bounded reads, numeric greetings and strict legend/row parsing. A
hot-link socket cannot subsequently receive commands without reconnection.
Commands with uncertain outcomes are never replayed automatically.

The client retains the prior public diagnostic/compatibility methods, but not a
second implementation. Python clients must serialize lifecycle calls themselves;
the IOC's operation lock provides that serialization for PV operations.

`massoft_timing.SourceGuard` is attached explicitly by the IOC after the operator
verifies the run origin. Under the client's sample lock it validates elapsed
counters, clock continuity and age, discards history and queues eligible rows.
The publisher drains the FIFO with shared source timestamps for each vector.
Standalone snapshot API users do not automatically receive this freshness guarantee.

## Controls And Data

All control PVs use `XF:08IDB-SE{RGA:1}:` followed by their suffix:

- `ExpName`, `View`, `OpenExp`: associate a file and load ordered mass legends.
- `Go` / `RunExp`: explicitly start a scan; skip for an already-running recipe.
- `Acquire`: enable/disable publication, not the MASsoft experiment.
- `Abort` / `AbortExp`: abort and require a fresh stopped response.
- `Close` / `CloseExp`: stop if needed, close the file and disconnect.
- `Connected`, `Status`, `LastError`, `ActiveFile`: state/error diagnostics.
- `DataAge`, `StatusAge`, `DataRawLine`, `DataRawAge`: receipt/stream diagnostics.
- `DataCycles`, `DataTimeFmt`, `DataMsFmt`, `RestartLinks`: data-link configuration.
- `SourceStartUTC`, `SourceMaxAge`: required run origin and source-age eligibility.
- `DataState`, `SourceTime`, `SourceAge`: publication quality/source-time diagnostics.
- `HistoryRows`, `QueueDepth`, `DroppedRows`, `PublishedRows`: replay/queue accounting.

Intensity names are `XF:08IDB-SE{RGA:1}P:MID1-I` through `P:MID20-I`;
mass labels are `XF:08IDB-VA{RGA:1}Mass:MID1` through `Mass:MID20`.
The [Archiver list](../archiver-pvs.txt) contains all 40 names. No renaming or
re-creation of existing archive entries is needed for this consolidation.

Readbacks are read-only. Unused channels are zero. Paused/stale intensities are
INVALID. A transport/parser failure clears Acquire and requires explicit
OpenExp/SourceStartUTC/Acquire recovery. After an IOC restart, set ExpName and View again;
there is no automatic resumption or scan start. Use `caput -c -w 120` for
lifecycle puts, and inspect LastError: put completion is not hardware success.

Changing View/data options requires Acquire=0 and can open a new replaying
stream. Acquire=0 by itself leaves healthy links draining and does not abort
the experiment. See the root README for complete copy-and-paste sequences.

MID alarm-only changes retain the last measurement timestamp to avoid time
reversal on subsequent delayed source rows. Archive DataState to preserve the
quality-transition timeline; duplicate-timestamp MID alarms may not be stored.
Quality PV names are listed in `../archiver-quality-pvs.txt`.
