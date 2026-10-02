# Guarded Live Acquisition (rc.3)

This page describes **manual-origin mode (`SourceMode=0`)**, retained as the
default. For automatic timing from tabular Real Time rows, with no per-run CSV,
see [MASsoft Tabular Real-Time Acquisition](REALTIME_ACQUISITION.md).

This is a commissioning candidate, not unattended-production approval. It does
not seek to MASsoft's latest cycle or repair old archive entries. It stops old
rows being published as current measurements, **provided the operator supplies
the correct run origin** and the source elapsed counter has the verified meaning.

## What Changed

- `SourceStartUTC` is mandatory after `OpenExp`. Use the Date/Time header from
  an export of that exact run, with an explicit time zone. Never use the time
  you connected, the first received row, `Get-Date`, or `-lTimeDate` as run start.
- MID timestamps are `SourceStartUTC + elapsed_ms / 1000`, not arrival time.
  All channels from one row get the same timestamp; values, units, zeros and
  negative readings are unchanged. `DataMsFmt=1` is now required/default.
- Old rows are counted and discarded while the link drains. They never become
  valid measurement updates. Publication also excludes rows at/before the most
  recent readback reset or previously published timestamp. This is live-only,
  not a historical backfill mechanism.
- Eligible rows use a bounded FIFO (256 rows by default), rather than just the
  latest-value cache. At most 64 rows are drained per publication pass. Overflow
  stops acquisition visibly; queued rows that expire are counted and discarded.
  This is not a guarantee that every cycle reaches Archiver: CA/client sampling
  and subscription buffering must also be tested.
- Counter resets, conflicting duplicate counters, source times over one second
  in the future, and IOC clock steps over one second fail closed. Small future
  timestamps wait until their time rather than being published into the future.
- `Acquire=0` keeps healthy links draining without publication. Resume discards
  pre-resume rows; it does not re-publish the paused interval. No automatic Go,
  Abort, Close, clock adjustment, reconnect, or command replay is added.

No undocumented MASsoft flags are used. Keep `DataCycles=1` for this test.
Large batches previously improved historical draining but delayed updates near
the live end. Changing link options/reopening can start history again; do not
increase `SourceMaxAge` just to make old data appear Live.

## Source-Time Prerequisites

Use a **fresh experiment started after both clocks are synchronized**, with the
experiment owner's approval. Stop publication before changing experiments or
clocks. `Acquire=0` does not stop a MASsoft experiment. This software does not
verify NTP or the selected CSV's identity automatically.

Export that run's CSV from MASsoft. Confirm its Date/Time header describes the
run origin by comparing `header + row elapsed_ms` against its real-time table.
The supplied exports have second-resolution headers without time-zone metadata:
do not claim sub-second absolute accuracy from them. Use the UTC offset **at run
start** (New York EDT is `-04:00`, EST is `-05:00`), not a guessed present offset.

The formula must be revalidated for experiment pauses, counter resets, clock
changes or a different export format. A wrong but plausible origin can mislabel
data despite the age guard. Do not restart an experiment under the same reference.
`OpenExp`, a changed View, Go, Close, or a fault clears the reference. A new IOC
process starts without one. `RestartLinks` retains the reference only for the
same run/view, establishes a fresh publication boundary, and filters replay again.

The offline helper reads only the CSV header. On a Windows workstation with this
checkout and its Python environment, choose the fresh export when prompted:

```powershell
$csv = Read-Host 'Full path to the CSV exported from the selected fresh run'
$offset = Read-Host 'UTC offset at run start, for example -04:00'
.\.venv\Scripts\python.exe hiden/massoft_timing.py "$csv" --date-order mdy "--utc-offset=$offset"
```

Use `dmy` if the export's date order is day/month/year. Linux equivalent, when
the CSV is available on that machine:

```bash
read -r -p 'CSV path: ' csv
read -r -p 'UTC offset at run start: ' offset
pixi run --locked python hiden/massoft_timing.py "$csv" --date-order mdy "--utc-offset=$offset"
```

The result is a UTC ISO timestamp ending in `Z`. Review it; the helper neither
sets a PV nor establishes that the file belongs to the active experiment.

## IOC2 Trial

Do not run duplicate Hiden instances, including the managed instance. Stop the
old Hiden IOC, not the MASsoft experiment, before this trial. Once this revision
is deployed, from the repository root on IOC2:

```bash
cd /nsls2/auto-storage/iss/shared/config/repos/Hiden
pixi install --locked
pixi run --locked python hiden/cap3.py --interfaces 0.0.0.0 --prefix TEST:
```

`TEST:` isolates PV names from existing production archive entries. It is **not**
a hardware sandbox: Go/Abort/Close would still control the RGA. Do not use those
commands for this read-only acquisition comparison.

From WS3 (or a second IOC2 terminal), retain any additional beamline search
addresses your site needs. The commands below select an already-running run;
paste the verified UTC origin produced above at the prompt:

```bash
export EPICS_CA_ADDR_LIST=10.66.59.255
export EPICS_CA_AUTO_ADDR_LIST=NO
export EPICS_CA_SERVER_PORT=5064
export EPICS_CA_REPEATER_PORT=5065

p='TEST:XF:08IDB-SE{RGA:1}:'
caput -c -w 120 "${p}Acquire" 0
caput -c -w 120 "${p}SourceMode" 0
read -r -p 'MASsoft experiment filename (relative to configured experiment directory): ' experiment
caput -S "${p}ExpName" "$experiment"
caput -c -w 120 "${p}View" 1
caput -c -w 120 "${p}OpenExp" 1
caget "${p}Connected"
caget -S "${p}ActiveFile" "${p}LastError"
```

Proceed only if Connected=1, ActiveFile is correct and LastError is empty. For
absolute Windows paths, `caput -S` needs doubled backslashes as documented in the
root README. Set the following options **before** the time reference:

```bash
caput -c -w 120 "${p}DataCycles" 1
caput -c -w 120 "${p}DataTimeFmt" 0
caput -c -w 120 "${p}DataMsFmt" 1
caput -c -w 120 "${p}SourceMaxAge" 10
read -r -p 'Verified run-start UTC ISO timestamp: ' start_utc
caput -c -w 120 -S "${p}SourceStartUTC" "$start_utc"
caget -S "${p}SourceStartUTC" "${p}LastError"
```

Check that SourceStartUTC was accepted and LastError is empty, then:

```bash
caput -c -w 120 "${p}Acquire" 1
caget "${p}Acquire" "${p}Connected"
caget -S "${p}DataState" "${p}LastError"
camonitor -S "${p}DataState" "${p}SourceAge" "${p}HistoryRows" "${p}QueueDepth" "${p}DroppedRows" "${p}PublishedRows"
```

Use another client terminal for measurement/diagnostic capture:

```bash
camonitor -S 'TEST:XF:08IDB-SE{RGA:1}:DataRawLine' 'TEST:XF:08IDB-SE{RGA:1}P:MID1-I' 'TEST:XF:08IDB-SE{RGA:1}P:MID9-I'
```

`DataRawLine` remains a latest-row, **receipt-timed diagnostic**, including withheld
history; it is not the guaranteed same row as each MID event in a burst. Use a
MASsoft CSV and source timestamps to compare all ten/twenty channels.

## Acceptance Checks

1. Initially, old rows may increment HistoryRows while SourceAge is large and
   PublishedRows stays zero. MID alarms must be INVALID; no old row becomes valid.
2. At the live end, DataState becomes Live, SourceAge stays within SourceMaxAge,
   PublishedRows advances, QueueDepth stays bounded and DroppedRows stays zero.
   Status=ScanningActive and small DataAge alone are **not** success criteria.
3. Compare a short MASsoft CSV interval against all active MID values and CA
   source timestamps. Match vectors without magnitude filtering or a guessed
   timestamp shift. Allow the documented CSV-header/clock precision, not hours.
4. Pause/resume publication. Expect no replay of paused rows and no experiment
   stop. Under operator supervision, reopen the same run and re-enter its origin;
   replay must be withheld again. Do not disrupt hardware/network for a fault test
   without a maintenance window; those paths have local simulator coverage.
5. If CatchingUp persists, leave the guard intact. Record SourceAge over time,
   HistoryRows, raw rows, LastError and the start reference. A replay stream slower
   than the recipe may never catch up. Use a fresh run linked promptly or request
   a documented latest/seek operation from Hiden; do not fabricate live timestamps.
6. After a successful isolated test, stop it before launching production names
   with `bash st.cmd`. Reassociate and re-enter the origin. Verify actual Archiver
   retrieval at source times, not just Connected=true. TEST PVs are not in the
   existing Archiver list; temporary archiving requires an explicit operator action.

Do not change or delete past archived data as part of this trial.

## Quality PVs And Limits

All suffixes below use `XF:08IDB-SE{RGA:1}:` (or `TEST:` plus that prefix).

| PV | Meaning |
| --- | --- |
| SourceStartUTC | Operator-verified origin, blank when unverified; writable only while paused/connected. |
| SourceMaxAge | Maximum source age in seconds; default 10 for commissioning, not a universal recipe limit. |
| DataState | Disconnected, AwaitingTime, Paused, Waiting, CatchingUp, Live, Stale or Error. |
| SourceTime / SourceAge | Latest validated received source epoch/age, including withheld history and paused rows. SourceTime=0 means unknown. |
| HistoryRows | Too-old/pre-boundary rows withheld since link start; not an error by itself. |
| QueueDepth | Eligible rows waiting for publication. |
| DroppedRows | Live rows expired or discarded at overflow since link start; excludes intentional history/pause discard. |
| PublishedRows | Complete measurement vectors published since OpenExp, not a count confirmed by Archiver. |

Add appropriate quality PVs from `archiver-quality-pvs.txt` as well as the existing
40 channels when commissioning production names. These lists do not configure
Archiver automatically. Quality PVs use IOC update timestamps. MID alarm changes
preserve the last source timestamp to avoid advancing it past the next source row;
an Archiver may ignore duplicate-timestamp alarm updates. Archive **DataState** for
the quality-transition timeline; don't rely only on MID alarm events. Reset zeros
are explicitly INVALID initialization records, not physical zero measurements.

Default ten-second eligibility allows some delivery latency; it does not promise
ten-second absolute time accuracy or detection of every possible VM clock problem.
This release does not synchronize clocks, verify the run identity cryptographically,
support historical backfill, or establish per-cycle Archiver completeness. Standalone
`MASsoftClient` callers keep the legacy snapshot API; the IOC explicitly configures
the guard and consumes the queue. Such callers must not treat snapshots as proven live.
