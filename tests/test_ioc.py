import asyncio
import importlib
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from cap2 import worker
from caproto import AlarmSeverity
from massoft_protocol import MASsoftConfig


@pytest.fixture(params=["cap2", "cap2_aj2"])
def ioc(request, sim):
    cls = importlib.import_module(request.param).RGAIOC
    group = cls(prefix="", mas_host="127.0.0.1", mas_port=sim.server_address[1])
    group.client.cfg = MASsoftConfig(
        host="127.0.0.1",
        port=sim.server_address[1],
        retry_s=0,
        command_timeout_s=0.8,
        link_chunk_timeout_s=0.03,
    )
    yield group
    group.client.disconnect()


async def publish_when_ready(ioc):
    for _ in range(200):
        if ioc.client.get_latest_row() is not None:
            await ioc._publish_once()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("No data arrived")


def test_twenty_channels_schema_and_readonly(ioc):
    for i in range(1, 21):
        assert f"XF:08IDB-SE{{RGA:1}}P:MID{i}-I" in ioc.pvdb
        assert f"XF:08IDB-VA{{RGA:1}}Mass:MID{i}" in ioc.pvdb
        assert type(getattr(ioc, f"mid{i}")).__name__.endswith("RO")
        assert type(getattr(ioc, f"mass{i}")).__name__.endswith("RO")
    assert ioc.mid1.alarm is not ioc.mid2.alarm


def test_open_acquire_close_reopen_and_short_recipe(ioc, sim):
    async def scenario():
        await ioc.open_exp.write(1)
        assert ioc.connected.value == 1
        assert ioc.mass20.value == 20
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.mid20.value == pytest.approx(19e-10)
        assert ioc.mid1.alarm.severity == AlarmSeverity.NO_ALARM
        await ioc.close.write(1)
        assert ioc.acquire.value == 0
        assert ioc.connected.value == 0
        assert ioc.mid1.alarm.severity == AlarmSeverity.INVALID_ALARM
        sim.legends = ["mass 2", "mass 40"]
        sim.row = "00:00:00\t0\t0\t5.6e-9"
        await ioc.experiment.write("new.exp")
        await ioc.open_exp.write(1)
        assert ioc.connected.value == 1
        assert ioc.mass20.value == 0
        assert ioc.mid20.value == 0
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.mid2.value == 5.6e-9
        assert ioc.mid20.value == 0

    asyncio.run(scenario())


def test_acquire_after_reported_four_digit_greeting(ioc, sim):
    # Actual MASsoft greeting from the IOC2 commissioning log.
    sim.greeting = "3968"

    async def scenario():
        await ioc.experiment.write("2026-3-alba-rubio1.exp")
        await ioc.open_exp.write(1)
        assert ioc.connected.value == 1, ioc.last_error.value
        assert ioc.last_error.value == ""
        await ioc.acquire.write(1)
        assert ioc.acquire.value == 1, ioc.last_error.value
        await publish_when_ready(ioc)
        assert ioc.status.value == "ScanningActive"
        assert ioc.mid20.value == pytest.approx(19e-10)
        assert not any(cmd.startswith(("-xGo", "-xAbort", "-xClose")) for _, cmd in sim.commands)

    asyncio.run(scenario())


def test_acquire_with_time_headers_and_scan_mass_legends(ioc, sim):
    sim.greeting = "3968"
    masses = [2, 15, 18, 28, 31, 32, 40, 44]
    sim.legends = ["Elapsed time", "Time (ms)"] + [
        f"Scan {i} : mass {mass:.2f}" for i, mass in enumerate(masses, 1)
    ]
    values = [0, -1.075e-10, 0, 3.225e-10, 6.45e-15, 0, 5.4825e-9, 8.0625e-14]
    sim.row = "00:06:51\t411758\t" + "\t".join(map(str, values))

    async def scenario():
        await ioc.open_exp.write(1)
        assert ioc.connected.value == 1, ioc.last_error.value
        await ioc.acquire.write(1)
        assert ioc.acquire.value == 1, ioc.last_error.value
        await publish_when_ready(ioc)
        for i, (mass, intensity) in enumerate(zip(masses, values), 1):
            assert getattr(ioc, f"mass{i}").value == mass
            assert getattr(ioc, f"mid{i}").value == intensity
        for i in range(len(masses) + 1, 21):
            assert getattr(ioc, f"mass{i}").value == 0
            assert getattr(ioc, f"mid{i}").value == 0
        if hasattr(ioc, "data_raw_line"):
            assert ioc.data_raw_line.value == sim.row

    asyncio.run(scenario())


def test_failure_resets_acquire_and_operator_can_recover(ioc, sim):
    async def scenario():
        await ioc.acquire.write(1)
        assert ioc.acquire.value == 0
        await ioc.open_exp.write(1)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        sim.drop_links.set()
        for _ in range(100):
            if not ioc.client._data_link.alive:
                break
            await asyncio.sleep(0.01)
        async with ioc._operation_lock:
            try:
                await ioc._publish_once()
            except Exception as exc:
                await ioc._fail(exc)
        assert ioc.acquire.value == 0
        assert ioc.connected.value == 0
        assert ioc.status.value == "Error"
        assert ioc.last_error.value
        sim.drop_links.clear()
        await ioc.open_exp.write(1)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.connected.value == 1
        assert ioc.acquire.value == 1

    asyncio.run(scenario())


def test_acquire_zero_does_not_abort_and_age_continues(ioc, sim):
    async def scenario():
        await ioc.open_exp.write(1)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        before = ioc.mid20.timestamp
        await ioc.acquire.write(0)
        paused = ioc.mid20.timestamp
        await asyncio.sleep(0.04)
        await ioc._publish_once()
        assert ioc.mid20.timestamp == paused
        assert paused >= before
        assert ioc.data_age.value >= 0
        assert not any("-xAbort" in cmd for _, cmd in sim.commands)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.mid20.alarm.severity == AlarmSeverity.NO_ALARM

    asyncio.run(scenario())


def test_stale_measurements_are_invalidated(ioc):
    async def scenario():
        await ioc.open_exp.write(1)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        ioc._stale_after_s = 1e-9
        await ioc._publish_once()
        assert ioc.mid1.alarm.severity == AlarmSeverity.INVALID_ALARM
        assert "stale" in ioc.last_error.value

    asyncio.run(scenario())


def test_concurrent_open_requests_are_serialized(ioc):
    async def scenario():
        await asyncio.gather(ioc.open_exp.write(1), ioc.open_exp.write(1))
        assert ioc.connected.value == 1
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)

    asyncio.run(scenario())


def test_invalid_acquire_write_preserves_running_state(ioc):
    async def scenario():
        await ioc.open_exp.write(1)
        await ioc.acquire.write(1)
        await ioc.acquire.write(2)
        assert ioc.acquire.value == 1
        assert ioc._publishing
        assert ioc.connected.value == 1

    asyncio.run(scenario())


def test_view_change_requires_pause_then_clears_old_metadata(ioc):
    async def scenario():
        await ioc.open_exp.write(1)
        await ioc.acquire.write(1)
        with pytest.raises(ValueError, match="Acquire=0"):
            await ioc.view.write(2)
        assert ioc.view.value == 1
        await ioc.acquire.write(0)
        await ioc.view.write(2)
        assert not ioc._links_started
        assert ioc.mass20.value == 0
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.mass20.value == 20

    asyncio.run(scenario())


def test_extended_commissioning_disabled_without_disconnect(sim):
    from cap2_aj2 import RGAIOC

    ioc = RGAIOC(prefix="", mas_host="127.0.0.1", mas_port=sim.server_address[1])

    async def scenario():
        await ioc.open_exp.write(1)
        try:
            await ioc.raw_send.write(1)
            assert "disabled" in ioc.last_error.value
            assert ioc.connected.value == 1
            assert not any("-xGo" in cmd for _, cmd in sim.commands)
        finally:
            await worker(ioc.client.disconnect)

    asyncio.run(scenario())


def test_worker_cancellation_waits_for_inflight_command():
    done = threading.Event()

    def command():
        time.sleep(0.05)
        done.set()

    async def scenario():
        task = asyncio.create_task(worker(command))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert done.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("module", ["cap2", "cap2_aj2"])
def test_real_ca_server_startup_readback_and_readonly(module, monkeypatch, tmp_path):
    from caproto.sync.client import read, write

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    for key, value in {
        "EPICS_CA_SERVER_PORT": str(port),
        "EPICS_CA_ADDR_LIST": f"127.0.0.1:{port}",
        "EPICS_CA_AUTO_ADDR_LIST": "NO",
        "EPICS_CAS_BEACON_ADDR_LIST": "127.0.0.1",
        "EPICS_CAS_AUTO_BEACON_ADDR_LIST": "NO",
    }.items():
        monkeypatch.setenv(key, value)
    root = Path(__file__).resolve().parents[1]
    logfile = tmp_path / "ioc.log"
    with logfile.open("w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(root / "hiden" / f"{module}.py"),
                "--prefix",
                "TEST:",
                "--interfaces",
                "127.0.0.1",
                "--mas-host",
                "127.0.0.1",
            ],
            stdout=log,
            stderr=log,
            env=os.environ.copy(),
        )
        try:
            pv = "TEST:XF:08IDB-SE{RGA:1}:Connected"
            result = None
            for _ in range(20):
                if proc.poll() is not None:
                    pytest.fail(logfile.read_text())
                try:
                    result = read(pv, timeout=0.3)
                    break
                except TimeoutError:
                    pass
            assert result is not None, logfile.read_text()
            assert result.data[0] == 0
            value_pv = "TEST:XF:08IDB-SE{RGA:1}P:MID20-I"
            assert read(value_pv, timeout=2).data[0] == 0
            with pytest.raises(Exception, match="[Aa]ccess|[Rr]ead|[Ww]rite"):
                write(value_pv, 123, timeout=2, notify=True)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
