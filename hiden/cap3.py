"""Unified asyncio Hiden RGA IOC for direct Python and Pixi deployment."""

from __future__ import annotations

import asyncio
import logging
import math
import shlex
import time
from functools import wraps

from caproto import AlarmSeverity, AlarmStatus
from caproto.server import PVGroup, pvproperty, run, template_arg_parser
from massoft_client import MASsoftClient, load_runtime_config
from massoft_protocol import extract_masses
from massoft_timing import SourceGuard, parse_start_utc

LOG = logging.getLogger(__name__)
_IOC_CFG = load_runtime_config().get("ioc", {})
MAX_MIDS = 20


def _ioc_default(name, default):
    return _IOC_CFG.get(name, default)


async def worker(function, *args, **kwargs):
    # Cancelling to_thread does not stop its thread. Finish an outstanding
    # command before shutdown/reassociation is allowed to close its socket.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            LOG.exception("Command failed while cancellation was pending")
        raise


def serialized(function):
    @wraps(function)
    async def put(self, instance, value):
        async with self._operation_lock:
            try:
                return await function(self, instance, value)
            except ValueError as exc:
                await self._error_text(exc)
                return instance.value
            except Exception as exc:
                LOG.exception("%s failed", function.__name__)
                await self._fail(exc)
                return 0

    return put


class RGAIOC(PVGroup):
    # ---------------------------------------------------------------------
    # Control / configuration
    # ---------------------------------------------------------------------

    open_exp = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:OpenExp",
        value=0,
        dtype=int,
        doc="Momentary: associate experiment and fetch MID metadata; does not start a scan.",
    )

    experiment = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:ExpName",
        value=str(_ioc_default("default_experiment", "file56.exp")),
        dtype=str,
        max_length=256,
        doc="Experiment file name (relative to MASsoft experiment directory) or full path",
    )

    view = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:View",
        value=max(1, int(_ioc_default("default_view", 1))),
        dtype=int,
        doc="MASsoft view number (used for -lStatus/-lData/-lLegends)",
    )

    go = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Go",
        value=0,
        dtype=int,
        doc="Momentary: -xGo (run experiment). Uses GoOD/GoOT and GoFilename.",
    )

    go_od = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:GoOD",
        value=1 if int(_ioc_default("default_go_od", 1)) else 0,
        dtype=int,
        doc="When 1, include 'd' in -O flags (create date directory).",
    )

    go_ot = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:GoOT",
        value=1 if int(_ioc_default("default_go_ot", 1)) else 0,
        dtype=int,
        doc="When 1, include 't' in -O flags (time-based filename).",
    )

    go_filename = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:GoFilename",
        value=str(_ioc_default("default_go_filename", "")),
        dtype=str,
        max_length=256,
        doc="Optional filename argument to -xGo. If blank, MASsoft uses its defaults.",
    )

    abort = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Abort",
        value=0,
        dtype=int,
        doc="Momentary: -xAbort and wait for a fresh Stopped* command response.",
    )

    close = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Close",
        value=0,
        dtype=int,
        doc="Momentary: abort, wait for Stopped*, then close and disconnect.",
    )

    acquire = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Acquire",
        value=0,
        dtype=int,
        doc="Enable guarded live publication; requires SourceStartUTC. Paused links keep draining.",
    )

    connected = pvproperty(
        read_only=True,
        alarm_group="connected",
        name="XF:08IDB-SE{{RGA:1}}:Connected",
        value=0,
        dtype=int,
        doc="1 if the client has connected sockets and associated a file; 0 otherwise.",
    )

    status = pvproperty(
        read_only=True,
        alarm_group="status",
        name="XF:08IDB-SE{{RGA:1}}:Status",
        value="Disconnected",
        dtype=str,
        max_length=32,
        doc="Last MASsoft status received from the status hot-link.",
    )

    last_error = pvproperty(
        read_only=True,
        alarm_group="last_error",
        name="XF:08IDB-SE{{RGA:1}}:LastError",
        value="",
        dtype=str,
        max_length=256,
        doc="Last client-side socket/protocol error for diagnostics.",
    )

    data_age = pvproperty(
        read_only=True,
        alarm_group="data_age",
        name="XF:08IDB-SE{{RGA:1}}:DataAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since the latest row was received, not source measurement age. -1 means unknown.",
    )

    status_age = pvproperty(
        read_only=True,
        alarm_group="status_age",
        name="XF:08IDB-SE{{RGA:1}}:StatusAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since the last status update was received from the MASsoft -lStatus hot-link. -1 means unknown.",
    )

    # ---------------------------------------------------------------------
    # MID-I readbacks (1-MAX_MIDS)
    # ---------------------------------------------------------------------

    for idx in range(1, MAX_MIDS + 1):
        locals()[f"mid{idx}"] = pvproperty(
            read_only=True,
            alarm_group=f"mid{idx}",
            name=f"XF:08IDB-SE{{{{RGA:1}}}}P:MID{idx}-I",
            value=0.0,
            dtype=float,
            doc=f"RGA MID{idx} intensity",
            precision=15,
        )
    del idx

    # ---------------------------------------------------------------------
    # Mass readbacks (1-MAX_MIDS)
    # ---------------------------------------------------------------------

    for idx in range(1, MAX_MIDS + 1):
        locals()[f"mass{idx}"] = pvproperty(
            read_only=True,
            alarm_group=f"mass{idx}",
            name=f"XF:08IDB-VA{{{{RGA:1}}}}Mass:MID{idx}",
            value=0.0,
            dtype=float,
            doc=f"MID{idx} mass value",
            precision=3,
        )
    del idx

    # Diagnostic and compatibility PVs retained from the extended IOC.
    run_exp = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:RunExp",
        value=0,
        dtype=int,
        doc="Write 1 to start the experiment",
    )

    abort_exp = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:AbortExp",
        value=0,
        dtype=int,
        doc="Write 1 to abort the running experiment",
    )

    close_exp = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:CloseExp",
        value=0,
        dtype=int,
        doc="Write 1 to close the experiment file",
    )

    active_file = pvproperty(
        read_only=True,
        alarm_group="active_file",
        name="XF:08IDB-SE{{RGA:1}}:ActiveFile",
        value="",
        dtype=str,
        max_length=320,
        doc="Current filename reported by -xFilename.",
    )

    refresh_file = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:RefreshFile",
        value=0,
        dtype=int,
        doc="Momentary: query -xFilename and update ActiveFile.",
    )

    data_cycles = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataCycles",
        value=max(1, int(_ioc_default("default_data_cycles", 1))),
        dtype=int,
        doc="Data link -c batch count; not a latest-row selector.",
    )

    data_time_fmt = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataTimeFmt",
        value=1 if int(_ioc_default("default_data_time_fmt", 0)) else 0,
        dtype=int,
        doc="Data link -t option.",
    )

    data_ms_fmt = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataMsFmt",
        value=1 if int(_ioc_default("default_data_ms_fmt", 1)) else 0,
        dtype=int,
        doc="Data link -m option.",
    )

    restart_links = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:RestartLinks",
        value=0,
        dtype=int,
        doc="Momentary: restart core status/data links with current options.",
    )

    data_raw_line = pvproperty(
        read_only=True,
        alarm_group="data_raw_line",
        name="XF:08IDB-SE{{RGA:1}}:DataRawLine",
        value="",
        dtype=str,
        max_length=4096,
        doc="Latest raw -lData line.",
    )

    data_raw_age = pvproperty(
        read_only=True,
        alarm_group="data_raw_age",
        name="XF:08IDB-SE{{RGA:1}}:DataRawAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since receipt of the latest raw line, not source measurement age.",
    )

    source_start = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:SourceStartUTC",
        value="",
        dtype=str,
        max_length=64,
        doc="Verified run origin with explicit Z/offset. Set AFTER OpenExp; never use current time.",
    )
    source_max_age = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:SourceMaxAge",
        value=float(_ioc_default("max_source_age_s", 10.0)),
        dtype=float,
        doc="Maximum source measurement age eligible for live publication, in seconds.",
    )
    data_state = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataState",
        value="Disconnected",
        dtype=str,
        max_length=32,
        read_only=True,
        alarm_group="data_state",
        doc="Publication quality, independent of MASsoft Status: AwaitingTime/CatchingUp/Live/etc.",
    )
    source_time = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:SourceTime",
        value=0.0,
        dtype=float,
        precision=3,
        read_only=True,
        alarm_group="source_time",
        doc="Latest validated received row's source Unix UTC seconds; 0 means unknown.",
    )
    source_age = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:SourceAge",
        value=-1.0,
        dtype=float,
        read_only=True,
        alarm_group="source_age",
        doc="Now minus SourceTime, including history/paused rows; -1 when unknown.",
    )
    history_rows = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:HistoryRows",
        value=0,
        dtype=int,
        read_only=True,
        alarm_group="history_rows",
        doc="Rows withheld as too old or before the publication boundary, since link start.",
    )
    queue_depth = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:QueueDepth",
        value=0,
        dtype=int,
        read_only=True,
        alarm_group="queue_depth",
        doc="Eligible live rows awaiting publication.",
    )
    dropped_rows = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DroppedRows",
        value=0,
        dtype=int,
        read_only=True,
        alarm_group="dropped_rows",
        doc="Live queue rows expired/discarded on overflow, since link start; excludes history/pause.",
    )
    published_rows = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:PublishedRows",
        value=0,
        dtype=int,
        read_only=True,
        alarm_group="published_rows",
        doc="Complete live rows published since OpenExp.",
    )

    raw_cmd = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:RawCmd",
        value="",
        dtype=str,
        max_length=320,
        doc="Raw command text for command socket.",
    )

    raw_send = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:RawSend",
        value=0,
        dtype=int,
        doc="Momentary: send RawCmd on command socket.",
    )

    raw_resp = pvproperty(
        read_only=True,
        alarm_group="raw_resp",
        name="XF:08IDB-SE{{RGA:1}}:RawResp",
        value="",
        dtype=str,
        max_length=512,
        doc="Response of latest RawSend.",
    )

    x_name = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:XName",
        value="Status",
        dtype=str,
        max_length=64,
        doc="Diagnostic x-call name: Status or Filename. Use dedicated scan controls for writes.",
    )

    x_args = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:XArgs",
        value="",
        dtype=str,
        max_length=320,
        doc="Arguments for generic x-call, shell-style quoted.",
    )

    x_send = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:XSend",
        value=0,
        dtype=int,
        doc="Momentary: run generic x-call (XName + XArgs).",
    )

    x_resp = pvproperty(
        read_only=True,
        alarm_group="x_resp",
        name="XF:08IDB-SE{{RGA:1}}:XResp",
        value="",
        dtype=str,
        max_length=512,
        doc="Response from latest generic x-call.",
    )

    l_item = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:LItem",
        value="Legends",
        dtype=str,
        max_length=64,
        doc="Generic one-shot link item name (Legends, Data, Status, ...).",
    )

    l_view = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:LView",
        value=1,
        dtype=int,
        doc="View for generic one-shot l-call.",
    )

    l_opts = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:LOpts",
        value="",
        dtype=str,
        max_length=320,
        doc="Extra options for generic one-shot l-call (shell-style quoted).",
    )

    l_fetch = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:LFetch",
        value=0,
        dtype=int,
        doc="Momentary: run generic one-shot l-call.",
    )

    l_resp = pvproperty(
        read_only=True,
        alarm_group="l_resp",
        name="XF:08IDB-SE{{RGA:1}}:LResp",
        value="",
        dtype=str,
        max_length=1024,
        doc="Response from latest generic one-shot l-call.",
    )

    # ---------------------------------------------------------------------
    # IOC internals
    # ---------------------------------------------------------------------
    client_class = MASsoftClient

    def __init__(self, *args, mas_host=None, mas_port=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = self.client_class(host=mas_host, port=mas_port)
        self._operation_lock = asyncio.Lock()
        self._publishing = False
        self._links_started = False
        self._mass_vals = []
        self._last_pub_status_ts = 0.0
        self._data_valid = False
        self._stream_started_at = 0.0
        self._source_epoch = None
        self._source_binding = None
        self._source_floor = max(getattr(self, f"mid{i}").timestamp for i in range(1, MAX_MIDS + 1))
        self._last_source_published = None
        self._published_count = 0
        self._last_raw_ts = 0.0
        self._queue_capacity = _ioc_default("data_queue_size", 256)
        self._update_period_s = float(_ioc_default("update_period_s", 1.0))
        self._stale_after_s = float(_ioc_default("stale_after_s", 60.0))
        if not math.isfinite(self._update_period_s) or self._update_period_s < 0.05:
            raise ValueError("update_period_s must be finite and >= 0.05")
        if not math.isfinite(self._stale_after_s) or self._stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive and finite")
        if not math.isfinite(self.source_max_age.value) or self.source_max_age.value <= 0:
            raise ValueError("max_source_age_s must be positive and finite")
        if type(self._queue_capacity) is not int or not 1 <= self._queue_capacity <= 100000:
            raise ValueError("data_queue_size must be an integer in 1..100000")
        if int(_ioc_default("start_links_on_open_exp", 0)):
            raise ValueError("Guarded mode requires start_links_on_open_exp=0")

    async def _error_text(self, exc):
        await self.last_error.write(str(exc)[:255])

    async def _invalidate(self):
        self._data_valid = False
        for i in range(1, MAX_MIDS + 1):
            pv = getattr(self, f"mid{i}")
            # Alarm changes must not put receipt time ahead of the next source timestamp.
            await pv.write(
                pv.value,
                verify_value=False,
                timestamp=pv.timestamp,
                status=AlarmStatus.COMM,
                severity=AlarmSeverity.INVALID_ALARM,
            )

    def _binding(self):
        return (str(self.client.current_file).casefold(), int(self.view.value))

    async def _clear_source_reference(self, state="AwaitingTime"):
        self._source_epoch = self._source_binding = None
        await self.source_start.write("", verify_value=False)
        await self.data_state.write(state)

    def _require_source_reference(self):
        if self._source_epoch is None or self._source_binding != self._binding():
            raise ValueError("Set verified SourceStartUTC after OpenExp/View selection")
        if not int(self.data_ms_fmt.value):
            raise ValueError("Guarded publication requires DataMsFmt=1")

    async def _fail(self, exc):
        self._publishing = False
        self._links_started = False
        await self.acquire.write(0, verify_value=False)
        await self.connected.write(0)
        await self.status.write("Error")
        await self.active_file.write("")
        await self._error_text(exc)
        await self._invalidate()
        await self._clear_source_reference("Error")
        # A fresh OpenExp is the only reconnection path; never replay Go/Abort.
        await worker(self.client.disconnect)

    async def _metadata(self):
        legends = await worker(self.client.fetch_legends, view=int(self.view.value))
        self._mass_vals = extract_masses(legends)
        for i in range(1, MAX_MIDS + 1):
            value = self._mass_vals[i - 1] if i <= len(self._mass_vals) else 0.0
            await getattr(self, f"mass{i}").write(value)

    async def _reset_readbacks(self, *, clear_masses=True):
        self._last_pub_status_ts = 0.0
        reset_time = max(time.time(), self._source_floor)
        for i in range(1, MAX_MIDS + 1):
            if clear_masses:
                await getattr(self, f"mass{i}").write(0.0)
            await getattr(self, f"mid{i}").write(
                0.0,
                verify_value=False,
                timestamp=reset_time,
                status=AlarmStatus.UDF,
                severity=AlarmSeverity.INVALID_ALARM,
            )
        self._source_floor = reset_time
        self._last_source_published = None
        self._data_valid = False
        await self.data_age.write(-1.0)
        await self.status_age.write(-1.0)
        await self.data_raw_line.write("")
        await self.data_raw_age.write(-1.0)
        self._last_raw_ts = 0.0
        await self.source_time.write(0.0)
        await self.source_age.write(-1.0)
        await self.history_rows.write(0)
        await self.queue_depth.write(0)
        await self.dropped_rows.write(0)

    @open_exp.putter
    @serialized
    async def open_exp(self, instance, value):
        if not int(value):
            return 0
        filename = str(self.experiment.value).strip()
        self.client._resolve_path(filename)  # Validate before disturbing the current association.
        self._publishing = False
        self._links_started = False
        await self.acquire.write(0, verify_value=False)
        await self.connected.write(0)
        await self.status.write("Opening")
        await self._clear_source_reference()
        self._published_count = 0
        await self.published_rows.write(0)
        await self._reset_readbacks()
        await worker(self.client.open_experiment, filename)
        await self._metadata()
        await self.active_file.write(self.client.current_file)
        await self.connected.write(1)
        await self.status.write("Connected")
        await self.last_error.write("")
        return 0

    async def _start_links(self):
        if not self.client.current_file:
            raise RuntimeError("Set ExpName/View and OpenExp before Acquire")
        self._require_source_reference()
        if self._links_started:
            self.client.check_links()
            return
        # Refetch metadata on each new stream, including after View changes.
        await self._metadata()
        guard = SourceGuard(
            self._source_epoch,
            max_age=float(self.source_max_age.value),
            capacity=self._queue_capacity,
            not_before=self._source_floor,
            wall=time.time(),
            monotonic=time.monotonic(),
        )
        guard.set_enabled(self._publishing)
        self.client.configure_timing(guard)
        await worker(self.client.start_status_link, view=int(self.view.value))
        options = dict(
            mid_cycles=int(self.data_cycles.value),
            include_time=bool(self.data_time_fmt.value),
            include_ms=bool(self.data_ms_fmt.value),
        )
        await worker(self.client.start_data_link, view=int(self.view.value), **options)
        self._links_started = True
        self._stream_started_at = time.monotonic()
        await self.data_state.write("CatchingUp" if self._publishing else "Paused")

    @acquire.putter
    @serialized
    async def acquire(self, instance, value):
        if int(value) not in (0, 1):
            raise ValueError("Acquire must be 0 or 1")
        if int(value):
            was_publishing = self._publishing
            self._publishing = True
            try:
                if self._links_started and not was_publishing:
                    self._require_source_reference()
                    self.client.enable_publication(
                        True, not_before=max(time.time(), self._source_floor)
                    )
                await self._start_links()
            except Exception:
                self._publishing = was_publishing
                raise
            await self.last_error.write("")
        else:
            self._publishing = False
            self.client.enable_publication(False)
            await self._invalidate()
            if self.connected.value:
                await self.data_state.write(
                    "Paused" if self._source_epoch is not None else "AwaitingTime"
                )
            elif self.data_state.value != "Error":
                await self.data_state.write("Disconnected")
        return int(value)

    async def _go(self):
        if self._publishing:
            raise ValueError("Set Acquire=0 before Go; the new run needs a new SourceStartUTC")
        await worker(self.client.stop_links)
        self._links_started = False
        self.client._reset_cache()
        await self._clear_source_reference()
        await self._reset_readbacks()
        await worker(
            self.client.x_go,
            filename=str(self.go_filename.value).strip() or None,
            od=bool(self.go_od.value),
            ot=bool(self.go_ot.value),
        )
        await self.active_file.write(self.client.current_file)

    @go.putter
    @serialized
    async def go(self, instance, value):
        if int(value):
            await self._go()
        return 0

    @abort.putter
    @serialized
    async def abort(self, instance, value):
        if int(value):
            await self.status.write(await worker(self.client.safe_abort_and_wait))
        return 0

    async def _close(self):
        self._publishing = False
        await self.acquire.write(0, verify_value=False)
        await self._invalidate()
        await worker(self.client.safe_abort_and_close)
        self._links_started = False
        await self.connected.write(0)
        await self.status.write("Disconnected")
        await self.active_file.write("")
        await self._clear_source_reference("Disconnected")

    @close.putter
    @serialized
    async def close(self, instance, value):
        if int(value):
            await self._close()
        return 0

    @view.putter
    async def view(self, instance, value):
        async with self._operation_lock:
            if int(value) < 1:
                raise ValueError("View must be positive")
            if int(value) != int(instance.value):
                if self._publishing:
                    raise ValueError("Set Acquire=0 before changing View")
                await worker(self.client.stop_links)
                self._links_started = False
                self.client._reset_cache()
                await self._clear_source_reference()
                await self._reset_readbacks()
            return int(value)

    async def _publish_once(self):
        sample = self.client.snapshot()
        now = time.monotonic()
        age = now - sample["row_ts"] if sample["row_ts"] else -1.0
        await self.data_age.write(age)
        await self.status_age.write(now - sample["status_ts"] if sample["status_ts"] else -1.0)
        if sample["status_ts"] > self._last_pub_status_ts:
            await self.status.write(sample["status"][:31])
            self._last_pub_status_ts = sample["status_ts"]
        await self.data_raw_age.write(age)
        if sample["raw_ts"] > self._last_raw_ts:
            # This diagnostic remains receipt-timed, including rejected history.
            await self.data_raw_line.write(sample["raw"][:4095], timestamp=sample["row_wall_ts"])
            self._last_raw_ts = sample["raw_ts"]
        timing = sample["timing"]
        if timing is not None:
            await self.history_rows.write(timing["history_rows"])
            await self.queue_depth.write(timing["queue_depth"])
            await self.dropped_rows.write(timing["dropped_rows"])
            if timing["latest"] is not None:
                source = timing["latest"].source_time
                await self.source_time.write(source)
                await self.source_age.write(time.time() - source)
        self.client.check_source_clock()
        if self._links_started:
            self.client.check_links()
        if not self._publishing:
            return
        self._require_source_reference()
        if age > self._stale_after_s or (
            age < 0 and now - self._stream_started_at > self._stale_after_s
        ):
            if self._data_valid:
                await self._invalidate()
            await self.data_state.write("Stale")
            await self.last_error.write("Data stale; check MASsoft scan and DataAge")
            return
        for row in self.client.drain_live_rows():
            self.client.check_source_clock()
            if (
                row.source_time <= self._source_floor
                or time.time() - row.source_time > self.source_max_age.value
            ):
                self.client.note_dropped_row()
                continue
            if len(row.values) != len(self._mass_vals):
                raise RuntimeError("Data/legend channel count changed; OpenExp again")
            for i in range(1, MAX_MIDS + 1):
                value = row.values[i - 1] if i <= len(row.values) else 0.0
                await getattr(self, f"mid{i}").write(
                    value,
                    # Parsed finite values; do not let numeric limit verification
                    # override the explicitly managed data-quality alarms.
                    verify_value=False,
                    timestamp=row.source_time,
                    status=AlarmStatus.NO_ALARM,
                    severity=AlarmSeverity.NO_ALARM,
                )
            self._source_floor = row.source_time
            self._last_source_published = row.source_time
            self._published_count += 1
            self._data_valid = True
        await self.published_rows.write(self._published_count)
        timing = self.client.snapshot()["timing"]
        await self.queue_depth.write(timing["queue_depth"])
        await self.dropped_rows.write(timing["dropped_rows"])
        if (
            self._data_valid
            and self._last_source_published is not None
            and time.time() - self._last_source_published <= self.source_max_age.value
        ):
            await self.data_state.write("Live")
            await self.last_error.write("")
        else:
            if self._data_valid:
                await self._invalidate()
            if self._last_source_published is None:
                await self.data_state.write("CatchingUp" if sample["row_ts"] else "Waiting")
            else:
                await self.data_state.write("Stale")
                await self.last_error.write("Source data stale; check SourceAge and SourceMaxAge")

    @source_start.putter
    async def source_start(self, instance, value):
        async with self._operation_lock:
            try:
                if self._publishing:
                    raise ValueError("Set Acquire=0 before changing SourceStartUTC")
                if not self.connected.value or not self.client.current_file:
                    raise ValueError("OpenExp before setting SourceStartUTC")
                normalized, epoch = parse_start_utc(str(value))
                if epoch > time.time() + 1.0:
                    raise ValueError(
                        "SourceStartUTC is in the future; verify CSV header/UTC offset"
                    )
            except ValueError as exc:
                await self._error_text(exc)
                return instance.value
            try:
                await worker(self.client.stop_links)
                self._links_started = False
                self.client._reset_cache()
                self._source_epoch, self._source_binding = epoch, self._binding()
                await self._reset_readbacks(clear_masses=False)
                await self.data_state.write("Paused")
                await self.last_error.write("")
                return normalized
            except Exception as exc:
                await self._fail(exc)
                return ""

    @source_max_age.putter
    async def source_max_age(self, instance, value):
        async with self._operation_lock:
            value = float(value)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("SourceMaxAge must be positive and finite")
            if self._publishing:
                raise ValueError("Set Acquire=0 before changing SourceMaxAge")
            await worker(self.client.stop_links)
            self._links_started = False
            self.client._reset_cache()
            await self._reset_readbacks(clear_masses=False)
            return value

    @status.startup
    async def status(self, instance, async_lib):
        async with self._operation_lock:
            await self._reset_readbacks()
        while True:
            async with self._operation_lock:
                try:
                    await self._publish_once()
                except Exception as exc:
                    LOG.exception("Publisher failed")
                    await self._fail(exc)
            await asyncio.sleep(self._update_period_s)

    @connected.shutdown
    async def connected(self, instance, async_lib):
        async with self._operation_lock:
            self._publishing = False
            await worker(self.client.disconnect)

    @run_exp.putter
    @serialized
    async def run_exp(self, instance, value):
        if int(value):
            await self._go()
        return 0

    @abort_exp.putter
    @serialized
    async def abort_exp(self, instance, value):
        if int(value):
            await self.status.write(await worker(self.client.safe_abort_and_wait))
        return 0

    @close_exp.putter
    @serialized
    async def close_exp(self, instance, value):
        if int(value):
            await self._close()
        return 0

    @refresh_file.putter
    @serialized
    async def refresh_file(self, instance, value):
        if int(value):
            await self.active_file.write(await worker(self.client.query_filename))
            if self._source_binding is not None and self._source_binding != self._binding():
                raise RuntimeError("Active file changed; OpenExp and verify SourceStartUTC")
        return 0

    @restart_links.putter
    @serialized
    async def restart_links(self, instance, value):
        if int(value):
            self._require_source_reference()
            await worker(self.client.stop_links)
            self._links_started = False
            self.client._reset_cache()
            await self._reset_readbacks()
            await self._start_links()
        return 0

    def _require_commissioning(self):
        if _ioc_default("enable_generic_commands", False) is not True:
            raise ValueError("Generic commands disabled; use dedicated controls")
        if self._publishing:
            raise ValueError("Stop Acquire before commissioning commands; OpenExp again afterwards")

    @raw_send.putter
    @serialized
    async def raw_send(self, instance, value):
        if int(value):
            self._require_commissioning()
            await self.raw_resp.write(
                (await worker(self.client.request_raw, str(self.raw_cmd.value)))[:511]
            )
        return 0

    @x_send.putter
    @serialized
    async def x_send(self, instance, value):
        if int(value):
            self._require_commissioning()
            args = shlex.split(str(self.x_args.value))
            await self.x_resp.write(
                (await worker(self.client.x_call, str(self.x_name.value), *args))[:511]
            )
        return 0

    @l_fetch.putter
    @serialized
    async def l_fetch(self, instance, value):
        if int(value):
            self._require_commissioning()
            opts = shlex.split(str(self.l_opts.value))
            await self.l_resp.write(
                (
                    await worker(
                        self.client.l_call_once,
                        str(self.l_item.value),
                        view=int(self.l_view.value),
                        options=opts,
                    )
                )[:1023]
            )
        return 0

    async def _set_data_option(self, instance, value, *, boolean=False):
        async with self._operation_lock:
            value = int(value)
            if (boolean and value not in (0, 1)) or (not boolean and not 1 <= value <= 100):
                raise ValueError("Invalid data format/cycle option")
            if instance is self.data_ms_fmt and value != 1:
                raise ValueError("Guarded publication requires DataMsFmt=1")
            if self._publishing:
                raise ValueError("Set Acquire=0 before changing data options")
            await worker(self.client.stop_links)
            self._links_started = False
            self.client._reset_cache()
            await self._reset_readbacks()
            return value

    @data_cycles.putter
    async def data_cycles(self, instance, value):
        return await self._set_data_option(instance, value)

    @data_time_fmt.putter
    async def data_time_fmt(self, instance, value):
        return await self._set_data_option(instance, value, boolean=True)

    @data_ms_fmt.putter
    async def data_ms_fmt(self, instance, value):
        return await self._set_data_option(instance, value, boolean=True)


def main(ioc_class=RGAIOC):
    parser, split = template_arg_parser(
        desc="Hiden RGA MASsoft IOC", default_prefix="", supported_async_libs=["asyncio"]
    )
    parser.add_argument("--mas-host", help="Override MASsoft INST host")
    parser.add_argument("--mas-port", type=int, help="Override MASsoft port (normally 5026)")
    args = parser.parse_args()
    ioc_options, run_options = split(args)
    logging.basicConfig(level=logging.INFO)
    ioc = ioc_class(**ioc_options, mas_host=args.mas_host, mas_port=args.mas_port)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
