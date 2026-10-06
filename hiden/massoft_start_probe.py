"""Opt-in, bounded start-timing experiment. Never supplies production timestamps.

Only association, filename, status and data requests are sent. The operator starts
the run in MASsoft; receiving ScanningActive on a newly opened link is NOT a start.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from massoft_protocol import (
    MASsoftHotlink,
    MASsoftProtocolError,
    MASsoftTimeout,
    _CRLFSocket,
    parse_realtime_row,
    quote_path,
)


class StartObservation:
    """Thread-safe evidence and conditional bounds, not an accuracy certificate."""

    def __init__(self, *, file, view, masses, clock, correction_s, rows, wall, monotonic):
        if not math.isfinite(correction_s) or not 0 <= correction_s <= 60:
            raise ValueError("TimingCorrection must be finite in 0..60 seconds")
        if type(rows) is not int or not 1 <= rows <= 1000:
            raise ValueError("TimingRows must be an integer in 1..1000")
        self.lock = threading.RLock()
        self.clock = clock
        self.state, self.error = "Connecting", ""
        self.file, self.view, self.masses = file, view, list(masses)
        self.correction_s, self.target_rows = correction_s, rows
        self.arm_wall, self.arm_monotonic = wall, monotonic
        self.statuses, self.rows = [], []
        self.transition = None
        self.origin_low = self.origin_high = None
        self.session = uuid.uuid4().hex
        self.output_file, self.saved = "", False

    def fail(self, message):
        with self.lock:
            if self.state not in ("Error", "Cancelled"):
                self.state, self.error = "Error", str(message)

    def _check_clock(self, wall, monotonic):
        if (
            not all(math.isfinite(x) for x in (wall, monotonic))
            or abs((wall - self.arm_wall) - (monotonic - self.arm_monotonic)) > 0.1
        ):
            raise MASsoftProtocolError("IOC clock stepped by >100 ms; timing trial invalid")

    def status(self, raw, *, wall, monotonic):
        with self.lock:
            if self.state in ("Complete", "Error", "Cancelled"):
                return
            self._check_clock(wall, monotonic)
            if len(raw) > 128:
                raise MASsoftProtocolError("Oversized status event")
            status = raw.strip().strip('"')
            if len(self.statuses) >= 128:
                raise MASsoftProtocolError("Too many status events; rearm a new trial")
            self.statuses.append(
                dict(raw=raw, status=status, receipt_unix=wall, monotonic=monotonic)
            )
            stopped = status in ("StoppedActive", "StoppedShutdown")
            if len(self.statuses) == 1:
                if not stopped:
                    raise MASsoftProtocolError(
                        "Initial status is not StoppedActive/StoppedShutdown; start was not observed"
                    )
                self.state = "Armed"
            elif self.state == "Armed" and stopped:
                pass
            elif self.state == "Armed" and status == "StartingActive":
                self.state = "Starting"
            elif self.state in ("Armed", "Starting") and status == "ScanningActive":
                self.transition = self.statuses[-1].copy()
                self.state = "Capturing"
            elif self.state in ("Starting", "Capturing") and status == self.statuses[-2]["status"]:
                pass
            else:
                raise MASsoftProtocolError(
                    f"Unexpected status {status!r}; no resume/reconnect may create a new origin"
                )

    def data(self, raw, *, wall, monotonic):
        with self.lock:
            if self.state != "Capturing":
                raise MASsoftProtocolError("No observed new-run transition for this data")
            self._check_clock(wall, monotonic)
            if len(raw) > 2048:
                raise MASsoftProtocolError("Oversized diagnostic row")
            values, elapsed, text = parse_realtime_row(raw, expected_count=len(self.masses))
            calendar, resolution = self.clock.parse(text)
            # Even a replayed row must belong to this trial, not a previous run
            # saved under the same filename. Allow only calendar quantization/skew.
            if calendar + resolution < self.arm_wall - 0.1:
                raise MASsoftProtocolError("Historical row predates arming; use a fresh run/file")
            if calendar > wall + 0.1:
                raise MASsoftProtocolError("Future MASsoft date; verify VM clock")
            if self.rows and elapsed <= self.rows[-1]["elapsed_ms"]:
                raise MASsoftProtocolError("Elapsed counter repeated/reset; trial invalid")
            low = calendar - elapsed / 1000.0
            high = low + resolution
            self.origin_low = low if self.origin_low is None else max(self.origin_low, low)
            self.origin_high = high if self.origin_high is None else min(self.origin_high, high)
            first_elapsed = self.rows[0]["elapsed_ms"] if self.rows else elapsed
            self.rows.append(
                dict(
                    raw=raw,
                    elapsed_ms=elapsed,
                    calendar_unix=calendar,
                    resolution_s=resolution,
                    values=values,
                    receipt_unix=wall,
                    monotonic=monotonic,
                    status_estimated_row_start_unix=(
                        self.transition["receipt_unix"]
                        - self.correction_s
                        + (elapsed - first_elapsed) / 1000.0
                    ),
                )
            )
            if self.origin_low >= self.origin_high:
                raise MASsoftProtocolError(
                    "Calendar/counter origin intervals disagree; model invalid"
                )
            if len(self.rows) >= self.target_rows:
                self.state = "Complete"

    def snapshot(self, *, full=False):
        with self.lock:
            report = dict(
                version=1,
                session=self.session,
                state=self.state,
                error=self.error,
                file=self.file,
                view=self.view,
                status_view=1,
                masses=self.masses,
                armed_unix=self.arm_wall,
                armed_monotonic=self.arm_monotonic,
                rows_requested=self.target_rows,
                rows_received=len(self.rows),
                correction_s=self.correction_s,
                statuses=list(self.statuses),
                transition=self.transition,
                output_file=self.output_file,
                saved=self.saved,
                accuracy_certified=False,
                production_timestamps_changed=False,
                model="calendar = truncate(origin + elapsed_ms/1000); constant origin",
                hypothesis="selected view includes the experiment's first measured mass",
            )
            if self.rows and self.transition:
                first = self.rows[0]
                raw_origin = self.transition["receipt_unix"] - first["elapsed_ms"] / 1000.0
                estimate = raw_origin - self.correction_s
                low, high = self.origin_low, self.origin_high
                report.update(
                    first_row=first,
                    last_row=self.rows[-1],
                    status_origin_uncorrected_unix=raw_origin,
                    status_origin_estimate_unix=estimate,
                    calendar_origin_interval_unix=[low, high],
                    calendar_origin_width_s=high - low,
                    status_origin_error_interval_s=[estimate - high, estimate - low],
                    combined_acquisition_notification_delay_interval_s=[
                        raw_origin - high,
                        raw_origin - low,
                    ],
                    # This is only model consistency, not hardware timing validation.
                    status_estimate_within_calendar_interval=low <= estimate < high,
                )
            if full:
                report["rows"] = list(self.rows)
            else:
                report["statuses"] = report["statuses"][-8:]
            return report


class StatusTimingProbe:
    """Independent, finite observer with one status reader and one data reader.

    Status is subscribed before the run. The data link is requested only AFTER
    the transition; its first row is expected to replay the start of the file.
    Data receipt times are therefore explicitly not a latency measurement.
    """

    def __init__(
        self, cfg, *, file, view, masses, clock, correction_s=0, rows=100, output_dir, timeout_s=600
    ):
        quote_path(file)
        if view < 1 or not 1 <= len(masses) <= 20:
            raise ValueError("A selected MID view with 1..20 masses is required")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Probe timeout must be positive")
        self.cfg, self.timeout_s = cfg, timeout_s
        self.model = StartObservation(
            file=file,
            view=view,
            masses=masses,
            clock=clock,
            correction_s=correction_s,
            rows=rows,
            wall=time.time(),
            monotonic=time.monotonic(),
        )
        self.output_dir = Path(output_dir)
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Use a new probe for each trial")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.output_dir / f"status-timing-{stamp}-{self.model.session}.json"
        # Test output permissions before opening instrument sockets; never overwrite evidence.
        with path.open("x", encoding="utf-8") as stream:
            stream.write("{}\n")
        self.model.output_file = str(path.resolve())
        self._thread = threading.Thread(target=self._run, name="MASsoftStartProbe", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(self.cfg.command_timeout_s + 3)
            if self.running:
                raise RuntimeError("Timing observer did not stop; do not rearm")

    def snapshot(self):
        return self.model.snapshot()

    def _new_socket(self, name):
        return _CRLFSocket(
            self.cfg.host, self.cfg.port, name=name, timeout_s=self.cfg.command_timeout_s
        )

    def _run(self):
        status, data = self._new_socket("TimingStatus"), self._new_socket("TimingData")
        link = None
        deadline = time.monotonic() + self.timeout_s

        def receive(lines):
            for raw in lines:
                # Capture immediately in the reader, before locks, parsing or disk/PV writes.
                wall, mono = time.time_ns() / 1e9, time.monotonic_ns() / 1e9
                try:
                    self.model.status(raw, wall=wall, monotonic=mono)
                except Exception as exc:
                    self.model.fail(exc)

        def check():
            if self._stop.is_set():
                raise InterruptedError("Operator cancelled timing trial")
            if time.monotonic() >= deadline:
                raise MASsoftTimeout(f"Timing trial exceeded its {self.timeout_s:g}-second budget")
            if self.model.snapshot()["state"] == "Error":
                raise MASsoftProtocolError(self.model.error)
            if link and link.error:
                raise MASsoftProtocolError(link.error)

        try:
            for sock in (status, data):
                check()
                sock.connect(enable_keepalive=self.cfg.enable_keepalive)
                check()
                if sock.request(f"-f{quote_path(self.model.file)}", retry_s=self.cfg.retry_s) in (
                    "",
                    "0",
                ):
                    raise MASsoftProtocolError("Timing socket association refused")
            check()
            status.send("-lStatus -v1", retry_s=self.cfg.retry_s)
            link = MASsoftHotlink(
                status,
                chunk_timeout_s=0.25,
                burst_gap_s=0,
                on_burst=receive,
                name="MASsoftTimingStatus",
            )
            link.start()
            while self.model.snapshot()["state"] != "Capturing":
                check()
                self._stop.wait(0.01)
            check()
            actual = data.request("-xFilename", retry_s=self.cfg.retry_s).strip().strip('"')
            if actual.casefold() != self.model.file.casefold():
                raise MASsoftProtocolError(
                    f"Filename changed at start ({actual!r}); use the same fresh named file"
                )
            check()
            data.send(f"-lData -v{self.model.view} -c1 -t1 -m1", retry_s=self.cfg.retry_s)
            while self.model.snapshot()["state"] != "Complete":
                check()
                try:
                    raw = data.read_line(timeout_s=0.25)
                except MASsoftTimeout:
                    continue
                wall, mono = time.time_ns() / 1e9, time.monotonic_ns() / 1e9
                self.model.data(raw, wall=wall, monotonic=mono)
        except InterruptedError as exc:
            with self.model.lock:
                if self.model.state not in ("Complete", "Error"):
                    self.model.state, self.model.error = "Cancelled", str(exc)
        except Exception as exc:
            self.model.fail(exc)
        finally:
            try:
                if link:
                    link.stop()
            except Exception as exc:
                self.model.fail(exc)
            finally:
                status.close()
                data.close()
            try:
                with self.model.lock:
                    self.model.saved = True
                    with Path(self.model.output_file).open("w", encoding="utf-8") as stream:
                        json.dump(self.model.snapshot(full=True), stream, indent=2, allow_nan=False)
                        stream.write("\n")
            except Exception as exc:
                with self.model.lock:
                    self.model.saved = False
                    self.model.fail(f"Could not save timing evidence: {exc}")
