# Status-Observed Start Timing Trial

2026-10-06, rc.3 commissioning extension. **Not a new production time source.**
The IOC opens a dedicated `-lStatus -v1` socket before a manually started run.
It timestamps complete status records in the reader thread, not in the 1-second
PV update loop. No busy spin, remote trigger, Go, Abort, Close or recipe changes
are performed by this observer. Starting the IOC alone does not enable it.

The existing 40 mass/intensity PV names, SourceMode behavior and publication
timestamps are unchanged. Diagnostic results never populate SourceStartUTC.
No per-run CSV is required for this test.

## What The Test Measures

Hiden's email supplied by the operator on 2026-10-06 states:

- On this HAL9, a row's ms counter precedes the first measurement in that row.
- The Real Time calendar text is truncated to seconds. The separate ms counter
  is not the fractional part of that calendar second.
- ScanningActive follows receipt of the first data point by MASsoft. Settle,
  dwell, other applicable acquisition overhead, MASsoft scheduling, transport,
  and receiver scheduling separate measurement start from our observation.
- A view per mass provides per-mass timestamps. A multi-mass view does not.
- A hardware-triggered action sequence is the stronger start reference, but is
  outside the intended beamline workflow. Socket notification timing is not
  guaranteed. The direct MSIU driver is not a drop-in MASsoft replacement.

The supplied HA-085-109 manual, sections 2.1.3.1 and 4, also documents
StartingActive before ScanningActive in newer MASsoft versions. Both receipts
are recorded, but neither is claimed to be the precise hardware start instant.

For this experiment, use a tabular Real Time view containing the **first mass
actually measured in the recipe**, preferably the existing all-mass View 2.
Using a view of a later mass would introduce its offset into the status estimate.
This test is not the future multi-view acquisition implementation.

The independent observer pre-associates status/data sockets to the selected file.
After an observed stopped-to-scanning transition, it requests that file's data
from the beginning. Data receipt times include association/request/replay delay;
they do NOT measure the original MASsoft-to-IOC delivery latency.

For the first row, with elapsed counter M in seconds:

```text
uncorrected origin = ScanningActive receipt UTC - M
estimated origin   = uncorrected origin - TimingCorrection
```

TimingCorrection is an explicitly assumed combined first-measurement and
notification delay. Default 0 intentionally leaves the estimate uncorrected.
Do not enter dwell percentages as seconds. No correction is guessed by the IOC.

Separately, each calendar/counter pair gives a conditional origin interval:

```text
calendar - elapsed <= origin < calendar + calendar_resolution - elapsed
```

The report intersects these intervals. This assumes the calendar is generated
by truncating a constant origin plus elapsed counter. The intervals exclude VM
clock error, device-clock drift and other model errors; they are NOT a certified
bound on alignment to XAS. An empty intersection invalidates the trial. A narrow
interval corroborates internal consistency, not absolute hardware accuracy.

## Update IOC2

Use a maintenance window approved by the experiment owner. Stop the prior Hiden
IOC with Ctrl-C in its server terminal. Stop the MASsoft experiment separately in
the GUI; `Acquire=0` and IOC shutdown do not stop it. Do not run duplicate IOCs.

On IOC2 (not a workstation):

```bash
cd /nsls2/auto-storage/iss/shared/config/repos/Hiden
git status --short
git pull --ff-only && pixi install --locked && git log -1 --oneline
```

If Git reports local changes, inspect/preserve them instead of forcing a pull.
Do not delete exports, analysis results, environments or site configuration.
Run the next command only after the update succeeds:

```bash
pixi run --locked python hiden/cap3.py --interfaces 0.0.0.0 --prefix TEST:
```

This is one isolated test IOC on IOC2. TEST PVs do not replace/enroll normal
Archiver channels. The tests temporarily interrupt normal RGA archival when
the normal IOC is stopped; coordinate that gap with the experiment owner.

## Prepare And Arm

In MASsoft, save a **fresh, uniquely named .exp file** with the desired recipe
and View 2 in **Real Time** tabular mode. Include the ms column and confirm
the view contains the first recipe mass. Leave the experiment STOPPED. Avoid
automatic date/time filename generation for this trial: the filename must stay
the same when you manually start. If MASsoft renames it, the observer fails
safely and the report explains why; it never silently follows another file.

Record clock health before the trials, without stepping either clock mid-run:
`chronyc tracking` on IOC2 and `w32tm.exe /query /status` on the VM. Save these
outputs with the reports. The previously observed NTP offset is not a current
clock-accuracy guarantee.

In a second **Bash** terminal on IOC2 or WS3:

```bash
export EPICS_CA_ADDR_LIST="10.66.59.30 10.66.59.255"
export EPICS_CA_AUTO_ADDR_LIST=NO
export EPICS_CA_SERVER_PORT=5064
export EPICS_CA_REPEATER_PORT=5065
export EPICS_CA_MAX_ARRAY_BYTES=65536

p='TEST:XF:08IDB-SE{RGA:1}:'
caput -c -w 120 "${p}Acquire" 0
read -r -p 'Fresh stopped experiment filename, e.g. timing-test-01.exp: ' experiment
caput -c -w 120 -S "${p}ExpName" "$experiment"
caput -c -w 120 "${p}View" 2
caput -c -w 120 "${p}OpenExp" 1
caget "${p}Connected"
caget -S "${p}ActiveFile" "${p}LastError"
```

These filenames are relative to the configured MASsoft directory, not IOC2's
filesystem. Verify Connected=1 and the correct ActiveFile before continuing.
Retain other search addresses needed by your beamline; the example covers the
stated EPICS subnet only.

```bash
caput -c -w 120 "${p}TimingRows" 100
caput -c -w 120 "${p}TimingCorrection" 0
caput -c -w 120 "${p}TimingArm" 1
camonitor -S "${p}TimingState"
```

Wait for **Armed**, then click Start manually in MASsoft. Do not use the IOC's
Go or Acquire controls. Keep the run going until the observer finishes.

Expected states: Connecting -> Armed -> Starting (if emitted) -> Capturing ->
Complete. Capturing begins when the status transition is received, not when the
first diagnostic row arrives. At a roughly 2-second cycle, 100 rows may take
about 3-4 minutes. The total observer budget is 600 seconds, including arming;
use fewer rows for a slower recipe. TimingRows accepts 1..1000.

`TimingArm` becomes 0 after the worker has closed its sockets and saved evidence.
Complete alone means the requested capture completed, not that timing passed.
`Acquire` stays 0; intensity PVs remain unpublished/INVALID during this test.

## Retrieve Results

After Complete, Ctrl-C stops camonitor only. Then:

```bash
caget "${p}TimingArm" "${p}Acquire" "${p}PublishedRows"
caget -S "${p}TimingState" "${p}TimingReport"
```

The JSON report identifies its `output_file` on IOC2. Full reports, including
all raw rows, are saved automatically under the repository's ignored directory:

```text
analysis/status-timing/status-timing-<UTC>-<unique-session>.json
```

Upload those JSON files rather than a screenshot. No CSV export or manual start
time is needed. The PV report contains a compact summary with the first/last row.
Files are also saved for cancellation/failure when possible; check `saved=true`.
No data file is deleted, overwritten by a later trial, or committed to Git.

Important fields:

| Field | Meaning |
| --- | --- |
| statuses | Status strings plus UTC and monotonic receipt times |
| transition | Observed ScanningActive notification, not hardware start |
| first_row / last_row | Raw line, calendar time, elapsed ms, values, receipt times and estimated row-start time |
| status_origin_uncorrected_unix | Notification time minus first elapsed counter |
| status_origin_estimate_unix | Same estimate after the configured correction |
| calendar_origin_interval_unix | Conditional lower-inclusive, upper-exclusive origin bounds |
| calendar_origin_width_s | Remaining ambiguity under the calendar/counter model |
| status_origin_error_interval_s | Estimated origin minus the possible calendar-derived origin |
| combined_acquisition_notification_delay_interval_s | Apparent combined delay, not network-only latency |
| accuracy_certified | Always false: no independent hardware reference was measured |

Every stored row includes `status_estimated_row_start_unix`, computed from the
observed transition, configured correction and that row's elapsed offset from
the first row. This is the trial's proposed timestamp, available for offline
comparison only; it is never assigned to an intensity PV or its Archiver event.

Repeat at least three times with fresh filenames and the **same recipe**, then
repeat with a representative different dwell/mass configuration. Save clock
health and recipe details (mass order, detector, dwell/settle) for comparison.
If practical, repeat once under ordinary beamline/XAS workload; do not introduce
artificial network or CPU stress on shared IOC servers.

## Failure And Cancellation

Set `TimingArm=0` to cancel only the observer; it does NOT stop MASsoft.
OpenExp or IOC shutdown also closes the observer and preserves available evidence.
An active trial blocks Acquire=1, Go and view/timing-setting changes. Trial
failure does not disconnect the main IOC's association or alter publication time.

- Initial ScanningActive/StartingActive: the start was already missed. Stop in
  MASsoft and use a new stopped experiment for the next trial.
- Disconnected/unknown status or interrupted socket: no synthetic start and no
  automatic reconnect. Start a fresh trial after fixing the connection.
- Historical row predates arming: replay from an old/overwritten file, or clocks
  disagree. Verify file, clocks and fresh data; do not accept that origin.
- Invalid date/time: use tabular **Real Time**, not the graphical elapsed view.
- Counter repeated/reset, inconsistent calendar intervals or IOC clock step:
  retain the report. No fallback to receiver time is made.
- Filename changed: configure the manual run to use the explicitly selected file.
- Timeout/no data/refusal: save the report and MASsoft configuration. Do not loop
  retries or reset the instrument from a diagnostic script.

## Recommended Production Direction

**No hardware results exist for this new trial yet.** Production decisions must
be conditional on the submitted reports and a representative IOC2/Archiver soak.

1. Retain MASsoft and healthy NTP on VM/IOC/XAS hosts. Do not replace it with the
   direct MSIU driver or add a hardware trigger against the intended workflow.
2. Prefer embedded MASsoft Real Time as the primary absolute reference when
   approximately one-second alignment is acceptable. It works for attaching to
   an already-running experiment, unlike a missed status edge. Current SourceMode=1
   already supports this, but still rejects distinct rows at the same second.
3. Whole-second truncation contributes up to almost one second of early bias;
   clock errors and per-mass offsets are additional. It does not establish a hard
   <=1 s end-to-end guarantee. For a strict bound, budget those terms explicitly
   and validate with an independent reference. Do not just append ms digits.
4. Use single-mass views when individual masses need accurate alignment. A shared
   row timestamps its FIRST measurement; a ~2 s cycle may violate a 1 s target
   for later masses. Multi-view acquisition is a separate implementation, with
   independent per-mass timestamps and bounded queues, not part of this trial.
5. If calendar/counter bounds converge consistently, a future opt-in calibrated
   mode could preserve millisecond spacing with a reported origin uncertainty.
   Do not adjust the origin mid-run after publication. Reused filenames, counter
   resets and clock discontinuities must invalidate/re-establish the run identity.
6. Use status observation as a cross-check by default. Only consider a status-based
   origin for witnessed starts if repeated tests show an acceptable delay/jitter
   budget across recipes. Do not reuse one recipe's fitted correction indiscriminately.
   A correction fitted and checked on the SAME trial is not independent validation.
7. For XAS metadata, record intensity, mass, source timestamp, age, alarm, file and
   timing method/quality. Reading a latest PV at scan start does not make it a
   fresh measurement. Separate scalar reads are not atomic; SourceSample bundles
   the current single-view frame. Future multi-view frames need per-mass times.
8. Keep replay suppression, bounded buffering and explicit stale/INVALID alarms.
   Neither this observer nor NTP skips history or repairs existing archive entries.
   Verify retrieved Archiver timestamps/values, not only Connected/Monitored flags.

To resume ordinary commissioning after the trial, stop the TEST IOC, launch
`bash st.cmd` on IOC2, and use the normal prefix with View 2, SourceMode=1,
OpenExp and Acquire as described in [Real-Time Acquisition](REALTIME_ACQUISITION.md).
Do not set SourceStartUTC from this diagnostic report or enroll TEST intensity
channels as production data. No automatic scan start or restart is performed.
