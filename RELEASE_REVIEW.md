# Release Review: 1.0.0-rc.3

Consolidation review date: 2026-10-01. Scope: the unified IOC/client, launch
configuration, retained PV/API contracts, cleanup and deployment documentation.
The initial 2026-09-28 safety review checked the supplied Hiden MASsoft Sockets
manual HA-085-109, especially sections 1.2, 2.1.1, 2.1.3, and 3.2. This refactor
does not introduce new MASsoft commands. Tests issued no real RGA commands.

Guarded-acquisition review date: 2026-10-01. The source-time guard below replaces
receipt-time publication. Earlier commissioning evidence in this document is
historical and is not evidence that this new mode works on real hardware.

## Production Blockers

**Not approved for unattended production.** Consolidation and successful
Archiver connectivity are not evidence of measurement freshness.

1. A newly opened MASsoft data link can replay an experiment from the beginning.
   The IOC now withholds old rows and requires an operator-verified run origin,
   but the meaning/precision of that origin and elapsed counter must be confirmed
   against the selected run's CSV and real-time table. A plausible wrong origin
   can defeat freshness checks. No documented seek/latest operation is implemented.
   Long histories may not catch up; use an isolated TEST-prefixed trial first.
2. A bounded FIFO now replaces latest-only publication, with visible overflow
   failure and expired-row accounting. This is not end-to-end lossless archiving:
   CA buffering, Archiver sampling, throughput, and recipe timing need validation.
   Guarded live mode intentionally discards historical and paused intervals.
3. The new mode still needs deployment testing and a representative
   hardware/Archiver soak on IOC2. Local Windows tests cannot establish Linux
   broadcast behavior, hardware response timing, or long-term archival continuity.

The VM NTP correction was reported successful on 2026-10-01 (about 3.4-3.5 ms
offset in the supplied samples). It does not fix replay or rewrite existing
archive timestamps. Do not hide this limitation by filtering negative values,
guessing a constant clock offset, or marking a connected stream as source-fresh.

## Guarded Acquisition

- Added `massoft_timing.py`: strict explicit-zone origin parsing, offline CSV
  header helper, timestamp/age checks, bounded FIFO and fail-latched clock/counter
  checks. No clock copying, NTP changes, automatic hardware actions or new socket
  commands were added. The offline helper was checked on the supplied export.
- `SourceStartUTC` must be entered after OpenExp. It is bound to the selected
  file/view and cleared on OpenExp, changed View, Go, Close and faults. Starting a
  new run manually requires a new reference; the IOC cannot independently prove
  a user-supplied CSV belongs to the active run.
- `DataMsFmt=1` is required/default. Each eligible row receives run-origin plus
  elapsed-ms timestamps, not arrival timestamps. Source-age and publication-boundary
  gates withhold replay; no values are scaled, clipped or filtered by magnitude.
- Nine additional timing/quality PVs bring the total to 87. The 40 existing
  Archiver channel names are unchanged. `archiver-quality-pvs.txt` is an additional
  operator-managed list, not automatic Archiver configuration.
- Repeated initialization exposed numeric verification overriding explicit INVALID
  alarms. Server-owned measurement writes now bypass that redundant numeric limit
  check, preserving explicit quality alarms; source cells are already strictly
  parsed. Measurement PVs remain externally read-only.
- Alarm-only MID updates preserve the last source timestamp, rather than advance
  past the next delayed sample. Archive DataState for alarm/quality transitions
  because duplicate-timestamp MID alarm events may be ignored. Synthetic reset
  zeros are INVALID, not valid measurements.
- Simulator coverage includes history-to-live transition, FIFO ordering, source
  timestamps over actual local CA, reconnect replay, pause/resume, overflow,
  missing/reset/duplicate counters, future data, stale queues and IOC clock steps.
  The [commissioning runbook](docs/GUARDED_ACQUISITION.md) defines remaining tests.

## Consolidation

- `hiden/cap3.py` is the only IOC implementation for direct Python and Pixi.
- `hiden/massoft_client.py` includes the former extended client API; transport
  and parsing remain in `massoft_protocol.py`.
- `cap2.py`, `cap2_aj2.py`, and `massoft_client_aj2.py` are deleted as requested,
  not maintained as wrappers. Custom launchers/imports must be updated.
- All 78 extended IOC PVs and the 54 public client attributes were captured from
  pre-refactor commit `915284b` in `tests/fixtures/legacy_contract.json`.
  Regression tests enforce names, types, lengths, precision and read-only settings.
  The sole changed original default is DataMsFmt=1 for guarded publication; all
  40 Archiver PV names remain unchanged.
- Old-source duplicate parametrizations were replaced with unified tests.
  Added tests check launch configuration, CLI help, combined hot-link cleanup,
  data-option changes and raw/value timestamp consistency.
- Measurement exports and local analysis are retained in place and ignored,
  rather than deleted or included in the runtime release. The guarded NTP
  helper and its mocked tests are included; they do not change machine clocks
  during testing.

## Findings Resolved

| Severity | Finding | Resolution |
| --- | --- | --- |
| High | Late responses after timeouts could be consumed by later commands. | Requests serialize, use bounded reads, consume the greeting, and discard a timed-out stream; no replay. |
| High | Polling fallback sent repeated commands on a data hot-link. | Removed fallback; sockets in link mode reject subsequent commands until reconnected. |
| High | Abort/close trusted cached stopped status or ignored abort failure. | Fresh command status required; unknown/failed abort never proceeds to close. |
| High | Switching experiment files reused existing associations. | OpenExp uses fresh sockets and clears cached measurements without closing/aborting the old experiment. |
| High | Numeric/time heuristics and skipped invalid cells could shift mass assignments. | Exact legend count, strict cells, explicit time parsing, and rejection of ambiguous rows. |
| High | Background reader/publisher failure could leave Acquire stuck on. | Reader health checks and fault state reset; operator recovery through OpenExp, no IOC restart needed. |
| High | Concurrent PV puts/cancelled workers could close a socket mid-command. | Serialized lifecycle and cancellation-safe worker completion before teardown. |
| Medium | Old masses/values remained after shorter recipes or pauses. | Clear all 20 slots on open/view changes, alarm invalid/stale data, retain reception ages. |
| Medium | Independent row/raw/timestamp reads could disagree. | Locked snapshots and immutable queued source rows. Raw diagnostics remain latest-received, not an atomic companion to MID events. |
| Medium | Measurement PVs were writable; generic commands bypassed safety. | Read-only readbacks, separate alarms, commissioning disabled by default and raw/execute queries restricted. |
| Medium | Manifest/lock disagreed, startup could silently use stale dependencies. | Pin Python 3.13/caproto 1.3.0, regenerate lock, require locked Pixi startup. |
| Low | Tracked caches/logs/IDE state, obsolete source copies, missing CI/tests. | Ignore/untrack generated artifacts, remove old snapshots, add tests/CI and LF rules. |

## Compatibility

Use `cap3.py`/`massoft_client.py`; old entry points and the `_aj2` import no longer
exist. All existing 20-channel mass/intensity and control PV names remain.
Direct Python now exposes the diagnostics previously available only in the
extended IOC. Generic commissioning operations are still disabled by default.

Intentional changes: source-time reference required, DataMsFmt=1 required,
Go requires Acquire=0 and clears the reference; readbacks reject external writes; faulted Acquire returns to
zero; invalid/stale measurements carry alarms; generic RawSend/XSend no longer
allow hardware-changing commands; missing config fails startup; only asyncio is
supported. Custom legends and ambiguous layouts fail instead of guessing.
No scaling, unit conversion, clipping, smoothing, or removal of negative values
has been added. MID values remain as reported by MASsoft.

## Validation And Approval

Local rc.3 result: **139 passed, 15 skipped** with Python 3.13.2/caproto 1.3.0
on Windows. Lint, format, Bash syntax and Pixi lock checks passed. Tests used
loopback only; no instrument, VM clock or Archiver changes were made. In a
separate read-only check, all ten values and elapsed counters were retained for
157 rows of `16H48M57.csv` and 54,661 rows of the supplied long-run CSV copy.
That confirms parsing, not the physical correctness of the run-origin assumption.

Automated tests cover the unified implementation using a loopback simulator, real local CA
reads/write rejection, timeouts, framing, parser edge cases, 20-channel mapping,
file changes, stale data, abort refusal, cancellation, and recovery. Static lint,
format checks, shell syntax and Pixi lock consistency are also release gates.
Prior rc.2 consolidation result: **95 tests passed, 15 skipped** with
Python 3.13.2/caproto 1.3.0 on Windows. The skips are Windows PowerShell 5.1
runtime cases blocked by its Restricted execution policy; both available
PowerShell parsers were checked and PowerShell 7 mock cases ran. Tests do not
bypass script policy. Lint, format, Bash syntax, and Pixi lock checks passed.
Removing duplicate old-variant parametrizations reduces the test count without
removing the underlying scenarios. Linux execution and the
locked Linux Python patch release remain deployment checks. The new CI workflow is
provided, but its remote run cannot be claimed before it executes.

**Approved for controlled commissioning, not yet for unattended production.**
Windows loopback tests do not establish Linux network behavior, hardware timing,
detector units, or long-term Archiver reliability for this revised code.

### Commissioning Greeting Correction

IOC2 reported `MASsoftCommand: invalid greeting '3968'`. The initial release
candidate restricted greetings to the two/three digits described by HA-085-109;
the deployed MASsoft sends a longer numeric greeting. This prevented `OpenExp`
from associating any file, so subsequent `Acquire` requests correctly stayed off.

The corrected handshake accepts a nonempty ASCII decimal greeting without a
fixed digit count. It still consumes exactly one CRLF record before commands,
enforces the existing deadline/line-size limit, and closes malformed connections.
Tests reproduce the reported failure in both IOC variants before the fix and
verify successful OpenExp/Acquire afterward using a loopback simulator. Tests
also cover two-, three-, four-, and five-digit greetings and malformed greetings.
The operator subsequently reported successful OpenExp/Acquire on the real
MASsoft host. Repeat that check for this consolidated release.

### Commissioning Legend Correction

The next IOC2 log confirms the greeting was accepted and all three core sockets
connected. OpenExp then failed on `Unsupported MID legend 'Elapsed time'`.
Retained historical MASsoft output establishes the complete variant:
`Elapsed time`, `Time (ms)`, then `Scan 1 : mass 18.00`, and subsequent species.
The initial strict parser supported only bare `mass <number>` legends.

The parser now accepts that recorded format as well as bare mass labels. It
excludes only the recognized leading time headers, keeps species in response
order, and still rejects unknown, empty, duplicated/misplaced time columns and
more than 20 masses. One-shot data reads also count mass channels rather than
all legend cells. Regression tests cover both clients and IOCs, the recorded
format, the four-digit greeting, and MID mapping (including zero/negative values
and the seventh-channel signal). No magnitude-based filtering or data-cell
skipping was introduced. Confirm the corrected mapping on the real instrument.

Before production sign-off, the beamline owner must complete:

1. Stop the prior Hiden IOC only; verify no duplicate PV server. Back up any
   site-modified configuration and record the deployed Git commit.
2. Install the lock on IOC2, run the tests there, then start one instance with
   `bash st.cmd`. Check EPICS broadcast discovery from a workstation and services.
3. Follow the rc.3 isolated test procedure with a fresh post-NTP run, entering a
   verified SourceStartUTC after association without Go. Compare legends and values
   against MASsoft for every active channel, including channels 11..20. Test a
   shorter recipe and verify unused slots reset to zero. Identify the source
   row/time, not merely matching IOC/Archiver timestamps; confirm withheld replay,
   source timestamp accuracy, CatchingUp-to-Live transition and zero dropped rows.
4. Exercise Acquire pause/resume and file reassociation. In a maintenance window
   approved by the experiment owner, test Abort/Close and interrupted connectivity.
   Verify fresh OpenExp plus a reverified origin recovers and that no unintended
   scan starts/stops occur.
5. Verify stale/INVALID behavior with a threshold appropriate to the longest
   cycle. Confirm recipe labels satisfy the documented MID format.
6. Verify Archiver Connected/Monitored and retrieve recent JSON samples matching
   live values/timestamps; a broken Quick Chart alone is not an archival failure.
7. Perform a representative 24-hour soak with stable socket/thread/memory counts,
   acceptable SourceAge, no unexplained DroppedRows, and continuous archival.
   Monitor DataState as well as DataAge. Review logs and error recovery.
8. Confirm managed service PATH/PIXI_BIN, shutdown grace, network restrictions,
   and restart policy with controls staff. Shell exports do not configure services.

Recovery/rollback: record the deployed commit and back up site configuration
before updating. If needed, stop this IOC (not the running MASsoft scan), restore
the previously site-validated revision in a separate checkout, install its locked
environment, and start exactly one IOC. Reassociate the file without automatic
Go. The pre-consolidation checkpoint is `915284b`, not a claim of production
approval or a replay fix. Preserve archived entries and all local configuration
and measurement files when rolling back.
