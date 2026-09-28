"""Direct asyncio IOC. Shared lifecycle for both supported launch options."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from functools import wraps

from caproto import AlarmSeverity, AlarmStatus
from caproto.server import PVGroup, pvproperty, run, template_arg_parser
from massoft_client import MASsoftClient, load_runtime_config
from massoft_protocol import extract_masses

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
        doc="Start/stop PV updates from latest hot-link values (hot-links keep draining in background).",
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
        doc="Seconds since the last *new* data row was received from the MASsoft -lData hot-link. -1 means unknown.",
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
        self._last_pub_row_ts = self._last_pub_status_ts = 0.0
        self._data_valid = False
        self._stream_started_at = 0.0
        self._update_period_s = float(_ioc_default("update_period_s", 1.0))
        self._stale_after_s = float(_ioc_default("stale_after_s", 60.0))
        if not math.isfinite(self._update_period_s) or self._update_period_s < 0.05:
            raise ValueError("update_period_s must be finite and >= 0.05")
        if not math.isfinite(self._stale_after_s) or self._stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive and finite")

    async def _error_text(self, exc):
        await self.last_error.write(str(exc)[:255])

    async def _invalidate(self):
        self._data_valid = False
        for i in range(1, MAX_MIDS + 1):
            pv = getattr(self, f"mid{i}")
            await pv.write(pv.value, status=AlarmStatus.COMM, severity=AlarmSeverity.INVALID_ALARM)

    async def _fail(self, exc):
        self._publishing = False
        self._links_started = False
        await self.acquire.write(0, verify_value=False)
        await self.connected.write(0)
        await self.status.write("Error")
        if hasattr(self, "active_file"):
            await self.active_file.write("")
        await self._error_text(exc)
        await self._invalidate()
        # A fresh OpenExp is the only reconnection path; never replay Go/Abort.
        await worker(self.client.disconnect)

    async def _metadata(self):
        legends = await worker(self.client.fetch_legends, view=int(self.view.value))
        self._mass_vals = extract_masses(legends)
        for i in range(1, MAX_MIDS + 1):
            value = self._mass_vals[i - 1] if i <= len(self._mass_vals) else 0.0
            await getattr(self, f"mass{i}").write(value)

    async def _reset_readbacks(self):
        self._last_pub_row_ts = self._last_pub_status_ts = 0.0
        for i in range(1, MAX_MIDS + 1):
            await getattr(self, f"mass{i}").write(0.0)
            await getattr(self, f"mid{i}").write(
                0.0, status=AlarmStatus.UDF, severity=AlarmSeverity.INVALID_ALARM
            )
        self._data_valid = False
        await self.data_age.write(-1.0)
        await self.status_age.write(-1.0)
        if hasattr(self, "data_raw_line"):
            await self.data_raw_line.write("")
            await self.data_raw_age.write(-1.0)

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
        await self._reset_readbacks()
        await worker(self.client.open_experiment, filename)
        await self._metadata()
        if hasattr(self, "active_file"):
            await self.active_file.write(self.client.current_file)
        await self.connected.write(1)
        await self.status.write("Connected")
        await self.last_error.write("")
        if int(_ioc_default("start_links_on_open_exp", 0)):
            await self._start_links()
        return 0

    async def _start_links(self):
        if not self.client.current_file:
            raise RuntimeError("Set ExpName/View and OpenExp before Acquire")
        if self._links_started:
            self.client.check_links()
            return
        # Refetch metadata on each new stream, including after View changes.
        await self._metadata()
        await worker(self.client.start_status_link, view=int(self.view.value))
        options = {}
        if hasattr(self, "data_cycles"):
            options = dict(
                mid_cycles=int(self.data_cycles.value),
                include_time=bool(self.data_time_fmt.value),
                include_ms=bool(self.data_ms_fmt.value),
            )
        await worker(self.client.start_data_link, view=int(self.view.value), **options)
        self._links_started = True
        self._stream_started_at = time.monotonic()

    @acquire.putter
    @serialized
    async def acquire(self, instance, value):
        if int(value) not in (0, 1):
            raise ValueError("Acquire must be 0 or 1")
        if int(value):
            await self._start_links()
            self._publishing = True
            await self.last_error.write("")
        else:
            self._publishing = False
            await self._invalidate()
        return int(value)

    async def _go(self):
        await worker(
            self.client.x_go,
            filename=str(self.go_filename.value).strip() or None,
            od=bool(self.go_od.value),
            ot=bool(self.go_ot.value),
        )
        if hasattr(self, "active_file"):
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
        if hasattr(self, "active_file"):
            await self.active_file.write("")

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
                await self._reset_readbacks()
            return int(value)

    async def _publish_once(self):
        if self._links_started:
            self.client.check_links()
        sample = self.client.snapshot()
        now = time.monotonic()
        age = now - sample["row_ts"] if sample["row_ts"] else -1.0
        await self.data_age.write(age)
        await self.status_age.write(now - sample["status_ts"] if sample["status_ts"] else -1.0)
        if sample["status_ts"] > self._last_pub_status_ts:
            await self.status.write(sample["status"][:31])
            self._last_pub_status_ts = sample["status_ts"]
        if hasattr(self, "data_raw_age"):
            await self.data_raw_age.write(age)
        if not self._publishing:
            return
        if age > self._stale_after_s or (
            age < 0 and now - self._stream_started_at > self._stale_after_s
        ):
            if self._data_valid:
                await self._invalidate()
            await self.last_error.write("Data stale; check MASsoft scan and DataAge")
            return
        if sample["row_ts"] > self._last_pub_row_ts:
            row = sample["row"]
            if len(row) != len(self._mass_vals):
                raise RuntimeError("Data/legend channel count changed; OpenExp again")
            for i in range(1, MAX_MIDS + 1):
                value = row[i - 1] if i <= len(row) else 0.0
                await getattr(self, f"mid{i}").write(
                    value,
                    timestamp=sample["row_wall_ts"],
                    status=AlarmStatus.NO_ALARM,
                    severity=AlarmSeverity.NO_ALARM,
                )
            if hasattr(self, "data_raw_line"):
                await self.data_raw_line.write(
                    sample["raw"][:4095], timestamp=sample["row_wall_ts"]
                )
            self._last_pub_row_ts = sample["row_ts"]
            self._data_valid = True
            await self.last_error.write("")

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
