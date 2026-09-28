"""Pixi/extended IOC: core lifecycle plus compatibility and commissioning PVs."""

from __future__ import annotations

import shlex

from cap2 import RGAIOC as CoreIOC
from cap2 import _ioc_default, main, serialized, worker
from caproto.server import pvproperty
from massoft_client_aj2 import MASsoftClient


class RGAIOC(CoreIOC):
    client_class = MASsoftClient

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
        return 0

    @restart_links.putter
    @serialized
    async def restart_links(self, instance, value):
        if int(value):
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


if __name__ == "__main__":
    main(RGAIOC)
