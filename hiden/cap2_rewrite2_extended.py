"""
Extended Caproto IOC for Hiden RGA via MASsoft sockets.

This keeps the working rewrite behavior and adds generic/manual controls so
future MASsoft commands can be used without further IOC code changes.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from typing import Optional

from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run

from massoft_client_rewrite2_extended import MASsoftClientExtended, MASsoftConfig
from massoft_client_rewrite2_fixed import MASsoftTimeout, load_runtime_config


if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
logging.getLogger("caproto").propagate = False
logging.getLogger("caproto.ctx").propagate = False
LOG = logging.getLogger(__name__)

_RUNTIME_CFG = load_runtime_config()
_IOC_CFG = _RUNTIME_CFG.get("ioc", {}) if isinstance(_RUNTIME_CFG, dict) else {}
if not isinstance(_IOC_CFG, dict):
    _IOC_CFG = {}


def _ioc_default(name: str, default):
    val = _IOC_CFG.get(name, default)
    return default if val is None else val


class RGAIOCExtended(PVGroup):
    # ------------------------------------------------------------------
    # Core workflow PVs (compatible with rewrite2_fixed IOC)
    # ------------------------------------------------------------------

    open_exp = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:OpenExp",
        value=0,
        dtype=int,
        doc="Momentary: connect + open/associate experiment + start status/data links.",
    )

    experiment = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:ExpName",
        value=str(_ioc_default("default_experiment", "file56.exp")),
        dtype=str,
        max_length=256,
        doc="Experiment file name (relative to configured directory) or full path.",
    )

    view = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:View",
        value=max(1, int(_ioc_default("default_view", 1))),
        dtype=int,
        doc="MASsoft view number used for status/data/legends.",
    )

    go = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Go",
        value=0,
        dtype=int,
        doc="Momentary: execute -xGo with GoOD/GoOT/GoFilename options.",
    )

    go_od = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:GoOD",
        value=1 if int(_ioc_default("default_go_od", 1)) else 0,
        dtype=int,
        doc="Include 'd' in -O flags.",
    )

    go_ot = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:GoOT",
        value=1 if int(_ioc_default("default_go_ot", 1)) else 0,
        dtype=int,
        doc="Include 't' in -O flags.",
    )

    go_filename = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:GoFilename",
        value=str(_ioc_default("default_go_filename", "")),
        dtype=str,
        max_length=256,
        doc="Optional -xGo filename argument.",
    )

    abort = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Abort",
        value=0,
        dtype=int,
        doc="Momentary: -xAbort and wait for Stopped*.",
    )

    close = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Close",
        value=0,
        dtype=int,
        doc="Momentary: safe abort-and-close.",
    )

    acquire = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Acquire",
        value=0,
        dtype=int,
        doc="Enable IOC PV publishing from latest link values.",
    )

    connected = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Connected",
        value=0,
        dtype=int,
        doc="1 when sockets are connected and experiment opened.",
    )

    status = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Status",
        value="Disconnected",
        dtype=str,
        max_length=64,
        doc="Last status from hot-link.",
    )

    last_error = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:LastError",
        value="",
        dtype=str,
        max_length=512,
        doc="Last client-side error text.",
    )

    data_age = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since last parsed data row.",
    )

    status_age = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:StatusAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since last status update.",
    )

    # ------------------------------------------------------------------
    # Extended controls / diagnostics
    # ------------------------------------------------------------------

    active_file = pvproperty(
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
        doc="Data link -c option (core data link).",
    )

    data_time_fmt = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataTimeFmt",
        value=1 if int(_ioc_default("default_data_time_fmt", 0)) else 0,
        dtype=int,
        doc="Data link -t option.",
    )

    data_ms_fmt = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataMsFmt",
        value=1 if int(_ioc_default("default_data_ms_fmt", 0)) else 0,
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
        name="XF:08IDB-SE{{RGA:1}}:DataRawLine",
        value="",
        dtype=str,
        max_length=512,
        doc="Latest raw -lData line.",
    )

    data_raw_age = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataRawAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since latest raw -lData line.",
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
        doc="Name part for generic x-call (e.g. Status, Filename, Go).",
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
        name="XF:08IDB-SE{{RGA:1}}:LResp",
        value="",
        dtype=str,
        max_length=1024,
        doc="Response from latest generic one-shot l-call.",
    )

    # ------------------------------------------------------------------
    # MID-I readbacks (1-10)
    # ------------------------------------------------------------------

    for idx in range(1, 11):
        locals()[f"mid{idx}"] = pvproperty(
            name=f"XF:08IDB-SE{{{{RGA:1}}}}P:MID{idx}-I",
            value=0.0,
            dtype=float,
            doc=f"RGA MID{idx} intensity",
        )
    del idx

    # ------------------------------------------------------------------
    # Mass readbacks (1-10)
    # ------------------------------------------------------------------

    for idx in range(1, 11):
        locals()[f"mass{idx}"] = pvproperty(
            name=f"XF:08IDB-VA{{{{RGA:1}}}}Mass:MID{idx}",
            value=0.0,
            dtype=float,
            doc=f"MID{idx} mass value",
        )
    del idx

    # ------------------------------------------------------------------
    # IOC internals
    # ------------------------------------------------------------------

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        cfg = MASsoftConfig.from_runtime_config()
        self.client = MASsoftClientExtended(cfg)

        self._update_task: Optional[asyncio.Task] = None
        self._updating = False
        self._links_started = False
        self._last_pub_row_ts = 0.0
        self._last_pub_raw_ts = 0.0
        self._last_pub_status_ts = 0.0
        self._update_period_s = max(0.05, float(_ioc_default("update_period_s", 1.0)))
        self._default_view = max(1, int(_ioc_default("default_view", 1)))
        self._default_experiment = str(_ioc_default("default_experiment", "file56.exp"))

    # ------------------------------------------------------------------
    # Core workflow putters
    # ------------------------------------------------------------------

    @open_exp.putter
    async def open_exp(self, instance, value):
        if not int(value):
            return value

        fname = (self.experiment.value or "").strip() or self._default_experiment
        view = max(1, int(self.view.value or self._default_view))
        LOG.info("Open/associate experiment %s (view=%d)", fname, view)

        try:
            await asyncio.to_thread(self.client.connect)
            await asyncio.to_thread(self.client.open_experiment, fname)
            await self._refresh_active_file()

            legends = await asyncio.to_thread(self.client.fetch_legends_once, view=view)
            masses = self._extract_masses(legends)[:10]
            for idx, m in enumerate(masses, start=1):
                await getattr(self, f"mass{idx}").write(m)

            if not self._links_started:
                await self._start_core_links(view=view)

            await self.connected.write(1)
            await self.status.write(self.client.get_latest_status() or "Connected")
            await self.last_error.write("")

        except Exception as exc:
            LOG.exception("OpenExp failed")
            await self.connected.write(0)
            await self.status.write("Error")
            await self.last_error.write(repr(exc))

        return 0

    @go.putter
    async def go(self, instance, value):
        if not int(value):
            return value

        try:
            od = bool(int(self.go_od.value))
            ot = bool(int(self.go_ot.value))
            fn = (self.go_filename.value or "").strip() or None
            await asyncio.to_thread(self.client.x_go, filename=fn, od=od, ot=ot)
            await self._refresh_active_file()
        except Exception as exc:
            LOG.exception("Go failed")
            await self.last_error.write(repr(exc))
        return 0

    @abort.putter
    async def abort(self, instance, value):
        if not int(value):
            return value
        try:
            final = await asyncio.to_thread(self.client.safe_abort_and_wait, timeout_s=30.0)
            await self.status.write(final)
        except MASsoftTimeout as exc:
            await self.last_error.write(repr(exc))
        except Exception as exc:
            LOG.exception("Abort failed")
            await self.last_error.write(repr(exc))
        return 0

    @close.putter
    async def close(self, instance, value):
        if not int(value):
            return value
        try:
            await self._stop_update_task()
            await asyncio.to_thread(self.client.safe_abort_and_close, abort_timeout_s=30.0, reconnect=False)
            self._links_started = False
            await self.connected.write(0)
            await self.status.write("Disconnected")
            await self.active_file.write("")
        except Exception as exc:
            LOG.exception("Close failed")
            await self.last_error.write(repr(exc))
        return 0

    @acquire.putter
    async def acquire(self, instance, value):
        want = bool(int(value))
        if want and not self._updating:
            self._updating = True
            self._update_task = asyncio.create_task(self._pv_update_loop())
        elif not want and self._updating:
            self._updating = False
            await self._stop_update_task()
        return value

    @restart_links.putter
    async def restart_links(self, instance, value):
        if not int(value):
            return value
        try:
            await asyncio.to_thread(self.client.stop_links)
            self._links_started = False
            await self._start_core_links(view=int(self.view.value))
        except Exception as exc:
            LOG.exception("RestartLinks failed")
            await self.last_error.write(repr(exc))
        return 0

    @refresh_file.putter
    async def refresh_file(self, instance, value):
        if not int(value):
            return value
        try:
            await self._refresh_active_file()
        except Exception as exc:
            await self.last_error.write(repr(exc))
        return 0

    # ------------------------------------------------------------------
    # Extended/generic putters
    # ------------------------------------------------------------------

    @raw_send.putter
    async def raw_send(self, instance, value):
        if not int(value):
            return value
        cmd = (self.raw_cmd.value or "").strip()
        if not cmd:
            await self.raw_resp.write("")
            return 0
        try:
            resp = await asyncio.to_thread(self.client.request_raw, cmd)
            await self.raw_resp.write(resp)
            if cmd.lower().startswith("-xfilename"):
                await self.active_file.write(resp.strip().strip('"'))
        except Exception as exc:
            await self.last_error.write(repr(exc))
        return 0

    @x_send.putter
    async def x_send(self, instance, value):
        if not int(value):
            return value
        name = (self.x_name.value or "").strip()
        arg_text = (self.x_args.value or "").strip()
        args = shlex.split(arg_text) if arg_text else []
        try:
            resp = await asyncio.to_thread(self.client.x_call, name, *args)
            await self.x_resp.write(resp)
            lname = name.lower().lstrip("-")
            if lname.startswith("x"):
                lname = lname[1:]
            if lname == "status":
                await self.status.write(resp)
            if lname == "filename":
                await self.active_file.write(resp.strip().strip('"'))
        except Exception as exc:
            await self.last_error.write(repr(exc))
        return 0

    @l_fetch.putter
    async def l_fetch(self, instance, value):
        if not int(value):
            return value
        item = (self.l_item.value or "").strip()
        view = int(self.l_view.value)
        opt_text = (self.l_opts.value or "").strip()
        opts = shlex.split(opt_text) if opt_text else []
        try:
            resp = await asyncio.to_thread(self.client.l_call_once, item, view=view, options=opts)
            await self.l_resp.write(resp)
        except Exception as exc:
            await self.last_error.write(repr(exc))
        return 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _start_core_links(self, *, view: int) -> None:
        c = int(self.data_cycles.value)
        t = bool(int(self.data_time_fmt.value))
        m = bool(int(self.data_ms_fmt.value))
        await asyncio.to_thread(self.client.start_status_link, view=view)
        await asyncio.to_thread(
            self.client.start_data_link,
            view=view,
            mid_cycles=max(1, c),
            include_time=t,
            include_ms=m,
        )
        self._links_started = True

    async def _refresh_active_file(self) -> None:
        path = await asyncio.to_thread(
            self.client.query_filename,
            retry_s=self.client.cfg.retry_s,
            update_current=True,
        )
        await self.active_file.write(path)

    @staticmethod
    def _extract_masses(legends: list[str]) -> list[float]:
        out: list[float] = []
        for item in legends:
            if "mass" not in item.lower():
                continue
            try:
                out.append(float(item.split()[-1]))
            except Exception:
                continue
        return out

    async def _stop_update_task(self) -> None:
        t = self._update_task
        self._update_task = None
        if t is None:
            return
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    async def _pv_update_loop(self) -> None:
        try:
            while self._updating:
                now = time.monotonic()

                st = self.client.get_latest_status()
                st_ts = self.client.get_latest_status_timestamp()
                if st and st_ts > self._last_pub_status_ts:
                    await self.status.write(st)
                    self._last_pub_status_ts = st_ts
                await self.status_age.write(max(0.0, now - st_ts) if st_ts > 0 else -1.0)

                row = self.client.get_latest_row()
                row_ts = self.client.get_latest_row_timestamp()
                if row and row_ts > self._last_pub_row_ts:
                    for idx, val in enumerate(row[:10], start=1):
                        await getattr(self, f"mid{idx}").write(val)
                    self._last_pub_row_ts = row_ts
                await self.data_age.write(max(0.0, now - row_ts) if row_ts > 0 else -1.0)

                raw = self.client.get_latest_raw_line()
                raw_ts = self.client.get_latest_raw_line_timestamp()
                if raw and raw_ts > self._last_pub_raw_ts:
                    await self.data_raw_line.write(raw)
                    self._last_pub_raw_ts = raw_ts
                await self.data_raw_age.write(max(0.0, now - raw_ts) if raw_ts > 0 else -1.0)

                err = self.client.get_last_error()
                if err:
                    await self.last_error.write(err)

                await asyncio.sleep(self._update_period_s)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception("PV update loop error")
            await self.last_error.write(repr(exc))
            await self.status.write("Error")


if __name__ == "__main__":
    ioc_opts, run_opts = ioc_arg_parser(default_prefix="", desc="Hiden RGA MASsoft IOC (extended)")
    ioc = RGAIOCExtended(**ioc_opts)
    run(ioc.pvdb, **run_opts)
