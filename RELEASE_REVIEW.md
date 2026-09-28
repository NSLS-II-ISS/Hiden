# Release Review: 1.0.0-rc.1

Review date: 2026-09-28. Scope: both IOC entry points, both client APIs, runtime
configuration, startup, dependency lock, repository artifacts, and deployment docs.
Protocol checked against the supplied Hiden MASsoft Sockets manual HA-085-109,
especially sections 1.2, 2.1.1, 2.1.3, and 3.2. No real RGA commands were issued.

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
| Medium | Independent row/raw/timestamp reads could disagree. | One locked snapshot; receive timestamps shared across a row. |
| Medium | Measurement PVs were writable; generic commands bypassed safety. | Read-only readbacks, separate alarms, commissioning disabled by default and raw/execute queries restricted. |
| Medium | Manifest/lock disagreed, startup could silently use stale dependencies. | Pin Python 3.13/caproto 1.3.0, regenerate lock, require locked Pixi startup. |
| Low | Tracked caches/logs/IDE state, obsolete source copies, missing CI/tests. | Ignore/untrack generated artifacts, remove old snapshots, add tests/CI and LF rules. |

## Compatibility

Both `cap2.py`/`massoft_client.py` and `cap2_aj2.py`/`massoft_client_aj2.py` remain
runnable. All existing 20-channel mass/intensity and control PV names remain.
Common framing/configuration lives in `massoft_protocol.py`; the extended option
inherits the core client/IOC rather than duplicating safety logic.

Intentional changes: readbacks reject external writes; faulted Acquire returns to
zero; invalid/stale measurements carry alarms; generic RawSend/XSend no longer
allow hardware-changing commands; missing config fails startup; only asyncio is
supported. Custom legends and ambiguous layouts fail instead of guessing.
No scaling, unit conversion, clipping, smoothing, or removal of negative values
has been added. MID values remain as reported by MASsoft.

## Validation And Approval

Automated tests cover both variants using a loopback simulator, real local CA
reads/write rejection, timeouts, framing, parser edge cases, 20-channel mapping,
file changes, stale data, abort refusal, cancellation, and recovery. Static lint,
format checks, shell syntax and Pixi lock consistency are also release gates.
Local result after the greeting correction below: **79 tests passed** with
Python 3.13.2/caproto 1.3.0 on Windows;
lint, format, Bash syntax, and Pixi lock checks passed. Linux execution and the
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
The corrected handshake still needs confirmation on the real MASsoft host.

Before production sign-off, the beamline owner must complete:

1. Stop the prior Hiden IOC only; verify no duplicate PV server. Back up any
   site-modified configuration and record the deployed Git commit.
2. Install the lock on IOC2, run the tests there, then start one instance with
   `bash st.cmd`. Check EPICS broadcast discovery from a workstation and services.
3. Associate an already-running recipe without Go. Compare legends and values
   against MASsoft for every active channel, including channels 11..20. Test a
   shorter recipe and verify unused slots reset to zero.
4. Exercise Acquire pause/resume and file reassociation. In a maintenance window
   approved by the experiment owner, test Abort/Close and interrupted connectivity.
   Verify fresh OpenExp recovers and that no unintended scan starts/stops occur.
5. Verify stale/INVALID behavior with a threshold appropriate to the longest
   cycle. Confirm recipe labels satisfy the documented MID format.
6. Verify Archiver Connected/Monitored and retrieve recent JSON samples matching
   live values/timestamps; a broken Quick Chart alone is not an archival failure.
7. Perform a representative 24-hour soak with stable socket/thread/memory counts,
   current DataAge, and continuous archival. Review logs and error recovery.
8. Confirm managed service PATH/PIXI_BIN, shutdown grace, network restrictions,
   and restart policy with controls staff. Shell exports do not configure services.

Recovery/rollback: stop this IOC (not the running MASsoft scan), deploy the previous
known-good commit `c71971d` in a separate checkout, install its environment, and
start exactly one IOC. Reassociate the correct file and Acquire; do not restore
Go automatically. The earlier commit predates the safety fixes above. Preserve
archived entries and all local configuration/measurement files when rolling back.
