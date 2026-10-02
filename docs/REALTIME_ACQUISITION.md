# MASsoft Tabular Real-Time Acquisition (rc.3 Extension)

This is an opt-in commissioning mode, not unattended-production approval. It
removes the per-run CSV/start-time entry **when the selected view supplies a full
acquisition date/time in every TCP data row**. Default behavior remains manual
origin mode (`SourceMode=0`, View 1), so updating code does not silently change an
existing installation's timing source or view.

## Evidence And Precision

On 2026-10-01, View 2 of the running `2026-3-jorge1.exp` returned:

```text
10/1/2026 3:51:41 PM\t43\t4.70849e-09\t0\t8.6e-12\t6.38227e-11\t0\t1.12905e-08\t-2.6875e-12\t1.2255e-11\t-2.68749e-09\t0
```

The request was received around 17:54, confirming that this was a historical row,
not a latest-sample query. The date/time is now inside the MASsoft response. The
IOC can convert it directly to UTC; it does not use receipt time or infer run
start by subtracting the ms field. File association succeeded with the documented
busy retry enabled. No undocumented seek, timestamp or acquisition commands are used.

- Date order is explicitly configured as `mdy` or `dmy`, never inferred.
- The configured MASsoft VM timezone defaults to `America/New_York`. The IOC
  host's timezone and current UTC offset are not used. See Python's
  [zoneinfo documentation](https://docs.python.org/3/library/zoneinfo.html).
- The example time has one-second resolution. It is published exactly at the
  displayed second, **not** at an invented fractional second based on `43`.
- Fractional seconds are used only when present in the date/time text itself.
  `SourceResolution` reports that text resolution, not synchronization accuracy.
- The ms field is preserved unchanged. Its meaning in real-time display mode
  still needs consecutive-row verification; real-time mode does not depend on
  it being an experiment-elapsed counter or fraction of a second.
- Distinct rows with the same absolute timestamp stop publication with an error,
  rather than silently overwrite/merge measurements or manufacture timestamps.
  Validate this before using recipes faster than the table's time resolution.
- Invalid dates, backwards/future timestamps, DST-ambiguous or nonexistent local
  times, or a change back to elapsed-time text fail closed. There is no automatic
  fallback to receipt time or an old manual origin.

History is still discarded while the socket catches up. This change does not
speed replay, backfill missing history, or repair previously archived timestamps.

## Fresh Post-NTP Test

Obtain experiment-owner approval before stopping a run. First set the running
IOC's `Acquire=0`, then stop the actual experiment in MASsoft and confirm it has
stopped. `Acquire=0` alone does not stop the instrument. Restarting MASsoft is
optional; it clears application state but does not fix replay by itself.

Check the VM's `w32tm.exe /query /status` and IOC2's `chronyc tracking` before the
new run. Do not step a clock during acquisition. Start a **fresh, uniquely named
experiment in MASsoft**, with a tabular view (View 2 in this setup) displaying
**Real Time**. Verify that view's masses, order and units. The IOC does not create
the view or change MASsoft's table display setting.

After this revision is deployed on IOC2, stop the old Hiden IOC process (without
sending Go/Abort/Close to the instrument) and launch the isolated trial:

```bash
cd /nsls2/auto-storage/iss/shared/config/repos/Hiden
pixi install --locked
pixi run --locked python hiden/cap3.py --interfaces 0.0.0.0 --prefix TEST:
```

`TEST:` separates PV names, not instrument access. Do not run duplicate Hiden
instances or use Go/Abort/Close during the read-only comparison.

From a second IOC2 terminal or a beamline workstation:

```bash
export EPICS_CA_ADDR_LIST=10.66.59.255
export EPICS_CA_AUTO_ADDR_LIST=NO
export EPICS_CA_SERVER_PORT=5064
export EPICS_CA_REPEATER_PORT=5065

p='TEST:XF:08IDB-SE{RGA:1}:'
caput -c -w 120 "${p}Acquire" 0
read -r -p 'New experiment filename relative to the MASsoft directory: ' experiment
caput -c -w 120 -S "${p}ExpName" "$experiment"
caput -c -w 120 "${p}View" 2
caput -c -w 120 "${p}SourceMode" 1
caput -c -w 120 "${p}DataCycles" 1
caget -S "${p}SourceTimezone" "${p}SourceDateOrder"
caput -c -w 120 "${p}OpenExp" 1
caget "${p}Connected"
caget -S "${p}ActiveFile" "${p}LastError"
```

Retain any other beamline discovery addresses required at your site. Use the full
Windows path with doubled backslashes in `caput -S` if the file is not in the
configured directory. Verify **Connected=1**, the correct ActiveFile/view, the VM
timezone/date order, and an empty LastError before proceeding:

```bash
caput -c -w 120 "${p}Acquire" 1
caget "${p}Acquire" "${p}SourceAge" "${p}HistoryRows" "${p}PublishedRows"
caget -S "${p}DataState" "${p}LastError" "${p}SourceStartUTC"
caget -a -S "${p}DataRawLine"
caget -a -S "${p}SourceSample"
camonitor -S "${p}DataState" "${p}MASsoftTimeRaw" "${p}MASsoftMilliseconds" "${p}LastError"
```

No CSV or `SourceStartUTC` entry is required in this mode. SourceStartUTC remains
blank, by design. Selecting SourceMode=1 enables DataTimeFmt=1 and DataMsFmt=1;
both must remain enabled. An unavailable view, unsupported legend or non-real-time
row raises an error rather than guessing a mapping. Do not raise SourceMaxAge to
make old data appear Live. For long existing runs, catch-up can still be lengthy.

## Acceptance And Archiving

Compare at least 10 consecutive `SourceSample` records with the corresponding
MASsoft table rows, using `mas_time`, `mas_ms`, and the entire intensity vector,
not independent caget values taken at different times. The record includes the
filename, view, ordered masses, original time/ms, source Unix UTC seconds, text
resolution and IOC receipt time. Negative values and zeros are unchanged.

`MASsoftTimeRaw`, `MASsoftMilliseconds`, `SourceResolution`, and `SourceSample`
describe the **last published measurement**, with the same source timestamp as
the intensity PVs. During CatchingUp they remain empty/-1 and INVALID. While
paused/stale they retain the last measurement and become INVALID. In contrast,
DataRawLine and SourceTime/SourceAge describe the latest received row, including
withheld history. SourceSample is one coherent payload, not a guarantee of atomic
delivery across independent CA PVs or lossless Archiver sampling.

After the TEST trial, stop that IOC and launch the normal names on IOC2 with
`bash st.cmd`. Repeat association with `p='XF:08IDB-SE{RGA:1}:'`, View 2,
SourceMode=1, OpenExp, and Acquire. No automatic resumption or scan start occurs.
Enroll the relevant PVs from `archiver-quality-pvs.txt` alongside `archiver-pvs.txt`;
these lists do not configure the appliance themselves. Verify archived source
timestamps and JSON values against MASsoft using the retrieval API, not only the
web interface's connection status. Test pause/resume and reconnect after the first
comparison, then perform a representative hardware/Archiver soak.

For persistent opt-in after acceptance, set `ioc.default_source_mode=1`,
`ioc.default_view=2`, and `ioc.default_data_time_fmt=1` in the deployment config.
The VM locale/timezone (`source_date_order`, `source_timezone`) is a one-time site
configuration, not a per-experiment timestamp. Pixi's existing Linux lock includes
the IANA timezone database; direct Python installations need the updated
`requirements.txt` (including tzdata for Windows).

To return to manual origin mode, pause, set SourceMode=0 and the appropriate
view, then OpenExp and supply a verified SourceStartUTC as described in
[Guarded Live Acquisition](GUARDED_ACQUISITION.md). Changing modes clears the old
reference and readbacks. Never reuse a prior experiment's origin for a new run.
