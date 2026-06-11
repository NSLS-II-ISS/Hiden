"""
Caproto IOC for Hiden RGA via MASsoft sockets (merged).

Combines the working version's reliability patterns (eager connect, polling
fallback with reconnect retry) with the rewrite's protocol-correct hot-link
threads and diagnostic PVs.

Provides both working-version PVs (RunExp, AbortExp, CloseExp) and rewrite
PVs (Go, Abort, Close) for backward compatibility.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import shlex
import sys
import time

from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run

from massoft_client_aj2 import MASsoftClient, MASsoftTimeout, load_runtime_config

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
logging.getLogger("caproto").propagate = False
logging.getLogger("caproto.ctx").propagate = False
LOG = logging.getLogger(__name__)

# Runtime config for IOC defaults
_RUNTIME_CFG = load_runtime_config()
_IOC_CFG = _RUNTIME_CFG.get("ioc", {}) if isinstance(_RUNTIME_CFG, dict) else {}
if not isinstance(_IOC_CFG, dict):
    _IOC_CFG = {}


def _ioc_default(name: str, default):
    val = _IOC_CFG.get(name, default)
    return default if val is None else val


class RGAIOC(PVGroup):
    # -----------------------------------------------------------------
    # Control PVs (working-version names)
    # -----------------------------------------------------------------

    open_exp = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:OpenExp",
        value=0,
        dtype=int,
        doc="Write 1 to connect + open/associate the experiment + fetch legends",
    )

    experiment = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:ExpName",
        value=str(_ioc_default("default_experiment", "file56.exp")),
        dtype=str,
        max_length=256,
        doc="Experiment file name (relative to MASsoft experiment directory) or full path",
    )

    acquire = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Acquire",
        value=0,
        dtype=int,
        doc="Start/stop the acquisition loop",
    )

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

    go = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Go",
        value=0,
        dtype=int,
        doc="Momentary: -xGo (run experiment). Uses GoOD/GoOT and GoFilename.",
    )

    abort = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Abort",
        value=0,
        dtype=int,
        doc="Momentary: -xAbort and wait for Stopped* status.",
    )

    close = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Close",
        value=0,
        dtype=int,
        doc="Momentary: safe close (abort->wait for Stopped*->-xClose).",
    )

    # -----------------------------------------------------------------
    # Configuration PVs (from rewrite)
    # -----------------------------------------------------------------

    view = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:View",
        value=max(1, int(_ioc_default("default_view", 1))),
        dtype=int,
        doc="MASsoft view number (used for -lStatus/-lData/-lLegends)",
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

    # -----------------------------------------------------------------
    # Diagnostic PVs (from rewrite)
    # -----------------------------------------------------------------

    connected = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Connected",
        value=0,
        dtype=int,
        doc="1 if client has connected sockets and associated a file; 0 otherwise.",
    )

    status = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Status",
        value="Disconnected",
        dtype=str,
        max_length=32,
        doc="Last MASsoft status received from the status hot-link.",
    )

    last_error = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:LastError",
        value="",
        dtype=str,
        max_length=256,
        doc="Last client-side socket/protocol error for diagnostics.",
    )

    data_age = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:DataAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since last data row received. -1 means unknown.",
    )

    status_age = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:StatusAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since last status update received. -1 means unknown.",
    )

    # -----------------------------------------------------------------
    # Extended controls / diagnostics
    # -----------------------------------------------------------------

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

    # -----------------------------------------------------------------
    # MID-I readbacks (1-10)
    # -----------------------------------------------------------------

    for idx in range(1, 11):
        locals()[f"mid{idx}"] = pvproperty(
            name=f"XF:08IDB-SE{{{{RGA:1}}}}P:MID{idx}-I",
            value=0.0,
            dtype=float,
            doc=f"RGA MID{idx} intensity",
        )
    del idx

    # -----------------------------------------------------------------
    # Mass readbacks (1-10)
    # -----------------------------------------------------------------

    for idx in range(1, 11):
        locals()[f"mass{idx}"] = pvproperty(
            name=f"XF:08IDB-VA{{{{RGA:1}}}}Mass:MID{idx}",
            value=0.0,
            dtype=float,
            doc=f"MID{idx} mass value",
        )
    del idx

    # -----------------------------------------------------------------
    # Constructor
    # -----------------------------------------------------------------

    def __init__(self, *args, mas_host=None, mas_port=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = MASsoftClient(host=mas_host, port=mas_port)
        self._running = False
        self._acq_task: asyncio.Task | None = None
        self._links_started = False
        self._mass_vals: list[float] = []
        self._last_pub_status_ts = 0.0
        self._last_pub_row_ts = 0.0
        self._last_pub_raw_ts = 0.0
        self._update_period_s = max(0.05, float(_ioc_default("update_period_s", 1.0)))

    # -----------------------------------------------------------------
    # PV put handlers
    # -----------------------------------------------------------------

    @open_exp.putter
    async def open_exp(self, instance, value):
        """Connect if needed, open/associate experiment, and fetch legends."""
        if not int(value):
            return value

        fname = self.experiment.value
        if isinstance(fname, (list, tuple)):
            fname = fname[0]
        fname = (fname or "").strip() or "file56.exp"
        view = max(1, int(self.view.value or 1))

        LOG.info("Opening experiment: %s (view=%d)", fname, view)

        try:
            await self._stop_update_task()
            self._links_started = False

            await asyncio.to_thread(self.client.open_experiment, fname)
            await self._refresh_active_file()

            legends = await asyncio.to_thread(
                self.client.fetch_legends_once, view=view
            )
            masses = self._extract_masses(legends)[:10]
            self._mass_vals = masses

            for i, m in enumerate(self._mass_vals, start=1):
                await getattr(self, f"mass{i}").write(m)

            await self.connected.write(1)
            await self.status.write(
                self.client.get_latest_status() or "Connected"
            )
            await self.last_error.write("")
        except Exception as exc:
            LOG.exception("OpenExp failed")
            await self.connected.write(0)
            await self.status.write("Error")
            await self.last_error.write(repr(exc))

        return 0

    @run_exp.putter
    async def run_exp(self, instance, value):
        """Write 1 to start the experiment."""
        if int(value):
            try:
                await asyncio.to_thread(self.client.run_experiment)
                await self._refresh_active_file()
                if self._links_started:
                    await self._restart_core_links(view=max(1, int(self.view.value or 1)))
            except Exception as exc:
                LOG.exception("RunExp failed")
                await self.last_error.write(repr(exc))
        return 0

    @abort_exp.putter
    async def abort_exp(self, instance, value):
        """Write 1 to abort the running experiment and wait until stopped."""
        if int(value):
            try:
                final = await asyncio.to_thread(
                    self.client.safe_abort_and_wait,
                    timeout_s=30.0,
                )
                await self.status.write(final)
            except MASsoftTimeout as exc:
                await self.last_error.write(repr(exc))
            except Exception as exc:
                LOG.exception("AbortExp failed")
                await self.last_error.write(repr(exc))
        return 0

    @close_exp.putter
    async def close_exp(self, instance, value):
        """Write 1 to close the experiment file."""
        if not int(value):
            return 0

        try:
            await self._stop_update_task()

            await asyncio.to_thread(
                self.client.safe_abort_and_close,
                abort_timeout_s=30.0,
                reconnect=False,
            )
            self._links_started = False
            await self.connected.write(0)
            await self.status.write("Disconnected")
            await self.active_file.write("")
        except Exception as exc:
            LOG.exception("CloseExp failed")
            await self.last_error.write(repr(exc))

        return 0

    @go.putter
    async def go(self, instance, value):
        """Momentary: start experiment using GoOD/GoOT/GoFilename config PVs."""
        if not int(value):
            return value
        try:
            od = bool(int(self.go_od.value))
            ot = bool(int(self.go_ot.value))
            fn = (self.go_filename.value or "").strip() or None
            await asyncio.to_thread(self.client.x_go, filename=fn, od=od, ot=ot)
            await self._refresh_active_file()
            if self._links_started:
                await self._restart_core_links(view=max(1, int(self.view.value or 1)))
        except Exception as exc:
            LOG.exception("Go failed")
            await self.last_error.write(repr(exc))
        return 0

    @abort.putter
    async def abort(self, instance, value):
        """Momentary: abort and wait for Stopped* status."""
        if not int(value):
            return value
        try:
            final = await asyncio.to_thread(
                self.client.safe_abort_and_wait,
                timeout_s=30.0,
            )
            await self.status.write(final)
        except MASsoftTimeout as exc:
            await self.last_error.write(repr(exc))
        except Exception as exc:
            LOG.exception("Abort failed")
            await self.last_error.write(repr(exc))
        return 0

    @close.putter
    async def close(self, instance, value):
        """Momentary: safe abort->wait->close. MASsoft drops all file sockets."""
        if not int(value):
            return value
        try:
            await self._stop_update_task()

            await asyncio.to_thread(
                self.client.safe_abort_and_close,
                abort_timeout_s=30.0,
                reconnect=False,
            )
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
        """Start/stop acquisition loop."""
        want = bool(int(value))

        if want and not self._running:
            LOG.info("Starting acquisition loop")
            self._running = True
            self._acq_task = asyncio.create_task(self._acquire_loop())
        elif not want and self._running:
            LOG.info("Stopping acquisition loop")
            self._running = False
            await self._stop_update_task()

        return value

    # -----------------------------------------------------------------
    # Extended putters
    # -----------------------------------------------------------------

    @restart_links.putter
    async def restart_links(self, instance, value):
        """Stop links, restart with current PV options."""
        if not int(value):
            return value
        try:
            await self._restart_core_links(view=max(1, int(self.view.value or 1)))
        except Exception as exc:
            LOG.exception("RestartLinks failed")
            await self.last_error.write(repr(exc))
        return 0

    @refresh_file.putter
    async def refresh_file(self, instance, value):
        """Query -xFilename, update ActiveFile PV."""
        if not int(value):
            return value
        try:
            await self._refresh_active_file()
        except Exception as exc:
            await self.last_error.write(repr(exc))
        return 0

    @raw_send.putter
    async def raw_send(self, instance, value):
        """Send RawCmd on command socket."""
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
        """Generic -x call using XName/XArgs PVs."""
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
        """Generic one-shot -l call using LItem/LView/LOpts PVs."""
        if not int(value):
            return value
        item = (self.l_item.value or "").strip()
        view = int(self.l_view.value)
        opt_text = (self.l_opts.value or "").strip()
        opts = shlex.split(opt_text) if opt_text else []
        try:
            resp = await asyncio.to_thread(
                self.client.l_call_once, item, view=view, options=opts
            )
            await self.l_resp.write(resp)
        except Exception as exc:
            await self.last_error.write(repr(exc))
        return 0

    # -----------------------------------------------------------------
    # Acquisition loops
    # -----------------------------------------------------------------

    async def _acquire_loop(self):
        """Hot-link with polling fallback."""
        try:
            view = max(1, int(self.view.value or 1))

            # Fetch legends and populate mass PVs
            legends, path = await asyncio.to_thread(self.client.get_legends, view)
            mass_vals = self._extract_masses(legends)[:10]
            self._mass_vals = mass_vals
            for i, m in enumerate(mass_vals, start=1):
                await getattr(self, f"mass{i}").write(m)

            # Try hot-link threads first (skip if already started via OpenExp)
            if not self._links_started:
                try:
                    await self._start_core_links(view=view)
                    LOG.info("Hot-links started, using hotlink update loop")
                except Exception as exc:
                    LOG.warning("Hot-links failed (%s), falling back to polling", exc)
                    self._links_started = False
                    await self._polling_acquire_loop(view, mass_vals, path)
                    return

            await self._hotlink_update_loop()

        except asyncio.CancelledError:
            LOG.info("Acquisition loop cancelled")
        except Exception as exc:
            LOG.error("Error in acquisition loop: %s", exc)
            with contextlib.suppress(Exception):
                await self.last_error.write(repr(exc))

    async def _hotlink_update_loop(self):
        """Read thread-safe accessors and update PVs."""
        while self._running:
            now = time.monotonic()

            # Status
            st = self.client.get_latest_status()
            st_ts = self.client.get_latest_status_timestamp()
            if st and st_ts > self._last_pub_status_ts:
                await self.status.write(st)
                self._last_pub_status_ts = st_ts
            if st_ts > 0:
                await self.status_age.write(max(0.0, now - st_ts))
            else:
                await self.status_age.write(-1.0)

            # Data
            row = self.client.get_latest_row()
            row_ts = self.client.get_latest_row_timestamp()
            if row and row_ts > self._last_pub_row_ts:
                for i, val in enumerate(row[:10], start=1):
                    await getattr(self, f"mid{i}").write(val)
                self._last_pub_row_ts = row_ts
            if row_ts > 0:
                await self.data_age.write(max(0.0, now - row_ts))
            else:
                await self.data_age.write(-1.0)

            # Raw data line
            raw = self.client.get_latest_raw_line()
            raw_ts = self.client.get_latest_raw_line_timestamp()
            if raw and raw_ts > self._last_pub_raw_ts:
                await self.data_raw_line.write(raw)
                self._last_pub_raw_ts = raw_ts
            if raw_ts > 0:
                await self.data_raw_age.write(max(0.0, now - raw_ts))
            else:
                await self.data_raw_age.write(-1.0)

            # Error
            err = self.client.get_last_error()
            if err:
                await self.last_error.write(err)

            await asyncio.sleep(self._update_period_s)

    async def _polling_acquire_loop(self, view, mass_vals, path):
        """Working-version direct -lData polling with 5-attempt reconnect retry."""
        # Associate data socket with the experiment
        try:
            await asyncio.to_thread(self.client.open_experiment_data, path)
        except Exception as exc:
            LOG.error("Failed to associate data socket: %s", exc)
            return

        while self._running:
            try:
                raw_data = await asyncio.to_thread(
                    self.client.data_socket.send_command,
                    f"-lData -v{view}",
                )
                if raw_data and raw_data != "0":
                    values = raw_data.split()[2:]  # Skip time columns
                    if len(values) >= len(mass_vals):
                        for i, val in enumerate(values[: len(mass_vals)], start=1):
                            await getattr(self, f"mid{i}").write(float(val))
            except Exception as exc:
                LOG.error("Socket error during polling: %s", exc)
                self.client.data_socket.close()
                for attempt in range(5):
                    try:
                        await asyncio.to_thread(self.client.data_socket.connect)
                        LOG.info("Reconnected data socket")
                        break
                    except Exception as reconnect_err:
                        LOG.error(
                            "Reconnect attempt %d failed: %s",
                            attempt + 1,
                            reconnect_err,
                        )
                        await asyncio.sleep(5)
            await asyncio.sleep(1.0)

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    async def _start_core_links(self, *, view: int) -> None:
        """Start status and data hot-links using current data PV options."""
        c = int(self.data_cycles.value)
        t = bool(int(self.data_time_fmt.value))
        m = bool(int(self.data_ms_fmt.value))
        try:
            await asyncio.to_thread(self.client.start_status_link, view=view)
            await asyncio.to_thread(
                self.client.start_data_link,
                view=view,
                mid_cycles=max(1, c),
                include_time=t,
                include_ms=m,
            )
            self._links_started = True
        except Exception:
            self._links_started = False
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.client.stop_links, close_core_sockets=True)
            raise

    async def _restart_core_links(self, *, view: int) -> None:
        """Restart core hot-links on fresh TCP sockets."""
        await asyncio.to_thread(self.client.stop_links, close_core_sockets=True)
        self._links_started = False
        await self._start_core_links(view=view)

    async def _refresh_active_file(self) -> None:
        """Query -xFilename and update the ActiveFile PV."""
        path = await asyncio.to_thread(
            self.client.query_filename,
            retry_s=self.client.cfg.retry_s,
            update_current=True,
        )
        await self.active_file.write(path)

    @staticmethod
    def _extract_masses(legends: list[str]) -> list[float]:
        """Extract mass floats from legend strings."""
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
        """Cancel the acquisition/update task."""
        self._running = False
        t = self._acq_task
        self._acq_task = None
        if t is None:
            return
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t


if __name__ == "__main__":
    # Parse MASsoft connection arguments
    parser = argparse.ArgumentParser(description="RGA MASsoft IOC")
    parser.add_argument(
        "--mas-host",
        default=None,
        help="MASsoft host address (overrides config file)",
    )
    parser.add_argument(
        "--mas-port",
        type=int,
        default=None,
        help="MASsoft port number (overrides config file)",
    )
    args, remaining = parser.parse_known_args(sys.argv[1:])

    # Let caproto parse its own arguments from remaining args
    sys.argv = [sys.argv[0], *remaining]
    ioc_opts, run_opts = ioc_arg_parser(
        default_prefix="",
        desc="RGA MASsoft IOC",
    )

    ioc = RGAIOC(mas_host=args.mas_host, mas_port=args.mas_port, **ioc_opts)
    run(ioc.pvdb, **run_opts)
