"""
Caproto IOC for Hiden RGA via MASsoft sockets (rewrite).

Design goals:
- Keep MASsoft sockets in a protocol-compliant state:
  * one command socket, dedicated hot-link sockets for status and data 
- Avoid thread leaks / stuck executor threads when scans are aborted.
- Provide explicit PVs for Go/Abort/Close in addition to the data readbacks.

This file is the "stable full rewrite" intended to replace your current cap2.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from caproto.server import PVGroup, pvproperty, ioc_arg_parser, run

from massoft_client_rewrite2_fixed import (
    MASsoftClient,
    MASsoftConfig,
    MASsoftError,
    MASsoftTimeout,
    load_runtime_config,
)

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
# Avoid duplicated caproto log lines (caproto already installs its own handlers).
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


class RGAIOC(PVGroup):
    # ---------------------------------------------------------------------
    # Control / configuration
    # ---------------------------------------------------------------------

    open_exp = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:OpenExp",
        value=0,
        dtype=int,
        doc="Momentary: connect + open/associate the experiment + start status/data hot-links",
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
        doc="Momentary: -xAbort and wait for Stopped* status (uses status link / poll fallback).",
    )

    close = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Close",
        value=0,
        dtype=int,
        doc="Momentary: safe close (abort→wait for Stopped*→-xClose). MASsoft terminates all file-associated sockets.",
    )

    acquire = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Acquire",
        value=0,
        dtype=int,
        doc="Start/stop PV updates from latest hot-link values (hot-links keep draining in background).",
    )

    connected = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:Connected",
        value=0,
        dtype=int,
        doc="1 if the client has connected sockets and associated a file; 0 otherwise.",
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
        doc="Seconds since the last *new* data row was received from the MASsoft -lData hot-link. -1 means unknown.",
    )

    status_age = pvproperty(
        name="XF:08IDB-SE{{RGA:1}}:StatusAge",
        value=-1.0,
        dtype=float,
        doc="Seconds since the last status update was received from the MASsoft -lStatus hot-link. -1 means unknown.",
    )


    # ---------------------------------------------------------------------
    # MID-I readbacks (1–10)
    # ---------------------------------------------------------------------

    for idx in range(1, 11):
        locals()[f"mid{idx}"] = pvproperty(
            name=f"XF:08IDB-SE{{{{RGA:1}}}}P:MID{idx}-I",
            value=0.0,
            dtype=float,
            doc=f"RGA MID{idx} intensity",
        )
    del idx

    # ---------------------------------------------------------------------
    # Mass readbacks (1–10)
    # ---------------------------------------------------------------------

    for idx in range(1, 11):
        locals()[f"mass{idx}"] = pvproperty(
            name=f"XF:08IDB-VA{{{{RGA:1}}}}Mass:MID{idx}",
            value=0.0,
            dtype=float,
            doc=f"MID{idx} mass value",
        )
    del idx

    # ---------------------------------------------------------------------
    # IOC internals
    # ---------------------------------------------------------------------

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        cfg = MASsoftConfig.from_runtime_config()
        self.client = MASsoftClient(cfg)

        self._update_task: Optional[asyncio.Task] = None
        self._updating = False
        self._mass_vals: list[float] = []
        self._links_started = False
        self._last_pub_row_ts = 0.0
        self._last_pub_status_ts = 0.0
        self._update_period_s = max(0.05, float(_ioc_default("update_period_s", 1.0)))
        self._default_view = max(1, int(_ioc_default("default_view", 1)))
        self._default_experiment = str(_ioc_default("default_experiment", "file56.exp"))

    # ---------------------------------------------------------------------
    # PV put handlers
    # ---------------------------------------------------------------------

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

            # Fetch legends once to populate mass PVs (non-listening temp socket).
            legends = await asyncio.to_thread(self.client.fetch_legends, view=view)
            masses: list[float] = []
            for item in legends:
                if "mass" in item.lower():
                    try:
                        masses.append(float(item.split()[-1]))
                    except Exception:
                        continue
            self._mass_vals = masses[:10]

            for idx, m in enumerate(self._mass_vals, start=1):
                await getattr(self, f"mass{idx}").write(m)

            # Start hot-links once; keep them draining even if Acquire=0.
            if not self._links_started:
                await asyncio.to_thread(self.client.start_status_link, view=view)
                await asyncio.to_thread(self.client.start_data_link, view=view, mid_cycles=1, include_time=False, include_ms=False)
                self._links_started = True

            await self.connected.write(1)
            await self.status.write(self.client.get_latest_status() or "Connected")

        except Exception as exc:
            LOG.exception("OpenExp failed")
            await self.connected.write(0)
            await self.status.write("Error")
            await self.last_error.write(repr(exc))

        # Momentary action: reset PV to 0
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
            # Stop PV updates first to avoid races on stale data.
            await self._stop_update_task()

            # Safe sequencing: if the MSIU is running, abort and wait for Stopped* before closing.
            # After -xClose, MASsoft terminates all file-associated sockets (manual behavior).
            await asyncio.to_thread(self.client.safe_abort_and_close, abort_timeout_s=30.0, reconnect=False)

            self._links_started = False
            await self.connected.write(0)
            await self.status.write("Disconnected")

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

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------

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
        """
        Update PVs at 1 Hz from the latest hot-link values.

        This loop does not perform socket I/O. Sockets are drained by background hot-link threads.
        """
        try:
            while self._updating:
                now = time.monotonic()

                st = self.client.get_latest_status()
                st_ts = self.client.get_latest_status_timestamp()
                if st and st_ts > self._last_pub_status_ts:
                    await self.status.write(st)
                    self._last_pub_status_ts = st_ts
                if st_ts > 0:
                    await self.status_age.write(max(0.0, now - st_ts))
                else:
                    await self.status_age.write(-1.0)

                row_ts = self.client.get_latest_row_timestamp()
                row = self.client.get_latest_row()
                if row and row_ts > self._last_pub_row_ts:
                    for idx, val in enumerate(row[:10], start=1):
                        await getattr(self, f"mid{idx}").write(val)
                    self._last_pub_row_ts = row_ts

                if row_ts > 0:
                    await self.data_age.write(max(0.0, now - row_ts))
                else:
                    await self.data_age.write(-1.0)

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
    ioc_opts, run_opts = ioc_arg_parser(default_prefix="", desc="Hiden RGA MASsoft IOC (rewrite)")
    ioc = RGAIOC(**ioc_opts)
    run(ioc.pvdb, **run_opts)
