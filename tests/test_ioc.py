import asyncio
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cap3 import RGAIOC, worker
from caproto import AlarmSeverity
from massoft_protocol import MASsoftConfig


@pytest.fixture
def ioc(sim):
    sim.live_data = True
    group = RGAIOC(prefix="", mas_host="127.0.0.1", mas_port=sim.server_address[1])
    group.sim = sim
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
        await ioc._publish_once()
        if ioc._data_valid:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("No data arrived")


async def set_verified_origin(ioc):
    origin = datetime.fromtimestamp(ioc.sim.run_start, timezone.utc).isoformat()
    await ioc.source_start.write(origin)
    assert ioc.source_start.value, ioc.last_error.value


async def open_with_time(ioc):
    await ioc.open_exp.write(1)
    await set_verified_origin(ioc)


def test_twenty_channels_schema_and_readonly(ioc):
    for i in range(1, 21):
        assert f"XF:08IDB-SE{{RGA:1}}P:MID{i}-I" in ioc.pvdb
        assert f"XF:08IDB-VA{{RGA:1}}Mass:MID{i}" in ioc.pvdb
        assert type(getattr(ioc, f"mid{i}")).__name__.endswith("RO")
        assert type(getattr(ioc, f"mass{i}")).__name__.endswith("RO")
    assert ioc.mid1.alarm is not ioc.mid2.alarm


def test_open_acquire_close_reopen_and_short_recipe(ioc, sim):
    async def scenario():
        await open_with_time(ioc)
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
        await open_with_time(ioc)
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
        await open_with_time(ioc)
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
        await open_with_time(ioc)
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
        assert ioc.data_raw_line.value.split("\t")[2:] == sim.row.split("\t")[2:]

    asyncio.run(scenario())


def test_failure_resets_acquire_and_operator_can_recover(ioc, sim):
    async def scenario():
        await ioc.acquire.write(1)
        assert ioc.acquire.value == 0
        await open_with_time(ioc)
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
        await open_with_time(ioc)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.connected.value == 1
        assert ioc.acquire.value == 1

    asyncio.run(scenario())


def test_acquire_zero_does_not_abort_and_age_continues(ioc, sim):
    async def scenario():
        await open_with_time(ioc)
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
        await open_with_time(ioc)
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
        await set_verified_origin(ioc)
        assert ioc.connected.value == 1
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)

    asyncio.run(scenario())


def test_invalid_acquire_write_preserves_running_state(ioc):
    async def scenario():
        await open_with_time(ioc)
        await ioc.acquire.write(1)
        await ioc.acquire.write(2)
        assert ioc.acquire.value == 1
        assert ioc._publishing
        assert ioc.connected.value == 1

    asyncio.run(scenario())


def test_view_change_requires_pause_then_clears_old_metadata(ioc):
    async def scenario():
        await open_with_time(ioc)
        await ioc.acquire.write(1)
        with pytest.raises(ValueError, match="Acquire=0"):
            await ioc.view.write(2)
        assert ioc.view.value == 1
        await ioc.acquire.write(0)
        await ioc.view.write(2)
        assert not ioc._links_started
        assert ioc.mass20.value == 0
        assert ioc.source_start.value == ""
        await set_verified_origin(ioc)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.mass20.value == 20

    asyncio.run(scenario())


def test_commissioning_disabled_without_disconnect(sim):
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


def test_data_options_and_raw_diagnostics_in_unified_ioc(ioc, sim):
    async def scenario():
        await open_with_time(ioc)
        assert ioc.active_file.value == ioc.client.current_file
        await ioc.data_cycles.write(3)
        await ioc.data_time_fmt.write(1)
        await ioc.data_ms_fmt.write(1)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert any(cmd == "-lData -v1 -c3 -t1 -m1 -d0" for _, cmd in sim.commands)
        assert ioc.data_raw_line.value.split("\t")[2:] == sim.row.split("\t")[2:]
        assert ioc.data_raw_line.timestamp <= time.time()
        assert ioc.mid20.timestamp <= time.time()
        with pytest.raises(ValueError, match="Acquire=0"):
            await ioc.data_cycles.write(1)
        assert ioc.acquire.value == 1
        assert ioc.data_cycles.value == 3
        await ioc.acquire.write(0)
        with pytest.raises(ValueError, match="Invalid"):
            await ioc.data_cycles.write(101)
        await ioc.data_cycles.write(1)
        assert not ioc._links_started
        assert ioc.data_raw_line.value == ""
        assert ioc.data_raw_age.value == -1
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.data_raw_line.value.split("\t")[2:] == sim.row.split("\t")[2:]
        assert ioc.mid20.value == pytest.approx(19e-10)

    asyncio.run(scenario())


async def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for scripted stream")


def scripted_row(sim, elapsed_ms, value):
    seconds = elapsed_ms // 1000
    return (
        f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}\t{elapsed_ms}\t"
        + "\t".join([str(value)] * len(sim.legends))
    )


async def wait_for_counter(ioc, elapsed):
    await wait_until(
        lambda: (
            ioc.client.snapshot()["timing"]["latest"] is not None
            and ioc.client.snapshot()["timing"]["latest"].elapsed_ms == elapsed
        )
    )


def test_acquire_requires_verified_origin_without_starting_data_stream(ioc, sim):
    async def scenario():
        await ioc.acquire.write(0)
        assert ioc.data_state.value == "Disconnected"
        await ioc.open_exp.write(1)
        await ioc.acquire.write(1)
        assert ioc.acquire.value == 0
        assert ioc.connected.value == 1
        assert "SourceStartUTC" in ioc.last_error.value
        assert ioc.data_state.value == "AwaitingTime"
        assert not any(cmd.startswith("-lData") for _, cmd in sim.commands)
        await ioc.source_start.write("2026-10-01T12:00:00")
        assert ioc.source_start.value == ""
        assert "offset" in ioc.last_error.value
        await set_verified_origin(ioc)
        await ioc.acquire.write(1)
        await publish_when_ready(ioc)
        assert ioc.data_state.value == "Live"

    asyncio.run(scenario())


def test_reference_controls_cannot_change_while_publishing_and_go_clears_origin(ioc, sim):
    async def scenario():
        await open_with_time(ioc)
        reference = ioc.source_start.value
        await ioc.acquire.write(1)
        await ioc.source_start.write(reference)
        assert "Acquire=0" in ioc.last_error.value
        assert ioc.source_start.value == reference
        assert ioc.acquire.value == 1
        with pytest.raises(ValueError, match="Acquire=0"):
            await ioc.source_max_age.write(20)
        await ioc.go.write(1)
        assert "Acquire=0" in ioc.last_error.value
        assert not any(cmd.startswith("-xGo") for _, cmd in sim.commands)
        await ioc.acquire.write(0)
        with pytest.raises(ValueError, match="DataMsFmt=1"):
            await ioc.data_ms_fmt.write(0)
        await ioc.restart_links.write(1)
        assert ioc.data_state.value == "Paused"
        assert ioc.source_start.value == reference
        # Hardware-changing command is tested only against the loopback simulator.
        await ioc.go.write(1)
        assert any(cmd.startswith("-xGo") for _, cmd in sim.commands)
        assert ioc.source_start.value == ""
        assert ioc.data_state.value == "AwaitingTime"
        await ioc.acquire.write(1)
        assert ioc.acquire.value == 0
        assert "SourceStartUTC" in ioc.last_error.value

    asyncio.run(scenario())


def test_replay_fifo_source_timestamps_and_reopen_boundary(ioc, sim, monkeypatch):
    sim.run_start = time.time() - 3600
    sim.data_rows = queue.Queue()
    published = []
    original_write = ioc.mid1.write

    async def record(value, **kwargs):
        if kwargs.get("severity") == AlarmSeverity.NO_ALARM:
            published.append((value, kwargs["timestamp"]))
        await original_write(value, **kwargs)

    monkeypatch.setattr(ioc.mid1, "write", record)

    async def scenario():
        await open_with_time(ioc)
        await ioc.acquire.write(1)
        sim.data_rows.put(scripted_row(sim, 1000, 123))
        sim.data_rows.put(scripted_row(sim, 2000, 456))
        await wait_for_counter(ioc, 2000)
        await ioc._publish_once()
        assert published == []
        assert ioc.history_rows.value == 2
        assert ioc.published_rows.value == 0
        assert ioc.data_state.value == "CatchingUp"
        assert ioc.data_age.value < 1
        assert ioc.source_age.value > 3500
        assert ioc.mid1.alarm.severity == AlarmSeverity.INVALID_ALARM
        elapsed = int((time.time() - sim.run_start) * 1000) + 5
        for index, value in enumerate((0, -5.6e-9, 8.1e-7)):
            sim.data_rows.put(scripted_row(sim, elapsed + index, value))
        await wait_for_counter(ioc, elapsed + 2)
        await ioc._publish_once()
        assert [value for value, _ in published] == [0, -5.6e-9, 8.1e-7]
        assert [ts for _, ts in published] == [
            ioc._source_epoch + (elapsed + i) / 1000 for i in range(3)
        ]
        assert ioc.published_rows.value == 3
        assert ioc.dropped_rows.value == 0
        assert ioc.data_state.value == "Live"
        assert ioc.mid20.timestamp == pytest.approx(published[-1][1], abs=1e-6, rel=0)
        # A fresh connection starts over, but even recent pre-boundary data stays out.
        sim.data_rows = queue.Queue()
        await ioc.open_exp.write(1)
        assert ioc.source_start.value == ""
        await set_verified_origin(ioc)
        await ioc.acquire.write(1)
        sim.data_rows.put(scripted_row(sim, 1000, 123))
        sim.data_rows.put(scripted_row(sim, elapsed + 2, 8.1e-7))
        await wait_for_counter(ioc, elapsed + 2)
        await ioc._publish_once()
        assert len(published) == 3
        assert ioc.history_rows.value == 2
        assert ioc.published_rows.value == 0
        assert ioc.data_state.value == "CatchingUp"
        live = int((time.time() - sim.run_start) * 1000) + 5
        sim.data_rows.put(scripted_row(sim, live, 9e-7))
        await publish_when_ready(ioc)
        assert len(published) == 4
        assert ioc.published_rows.value == 1
        assert published[-1][1] > published[-2][1]
        assert not any(cmd.startswith(("-xGo", "-xAbort", "-xClose")) for _, cmd in sim.commands)

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["overflow", "reset", "missing_ms", "future"])
def test_stream_timing_faults_stop_publication_and_require_new_reference(ioc, sim, fault):
    sim.run_start = time.time() - 3600
    sim.data_rows = queue.Queue()
    ioc._queue_capacity = 2

    async def scenario():
        await open_with_time(ioc)
        await ioc.acquire.write(1)
        elapsed = int((time.time() - sim.run_start) * 1000) + 5
        sim.data_rows.put(scripted_row(sim, elapsed, 1e-9))
        if fault == "overflow":
            sim.data_rows.put(scripted_row(sim, elapsed + 1, 2e-9))
            sim.data_rows.put(scripted_row(sim, elapsed + 2, 3e-9))
        elif fault == "reset":
            sim.data_rows.put(scripted_row(sim, 0, 0))
        elif fault == "missing_ms":
            sim.data_rows.put("\t".join(["0"] * len(sim.legends)))
        else:
            sim.data_rows.put(scripted_row(sim, elapsed + 60000, 2e-9))
        await wait_until(lambda: not ioc.client._data_link.alive)
        try:
            await ioc._publish_once()
        except Exception as exc:
            await ioc._fail(exc)
        assert ioc.acquire.value == 0
        assert ioc.connected.value == 0
        assert ioc.data_state.value == "Error"
        assert ioc.source_start.value == ""
        assert ioc.published_rows.value == 0
        assert ioc.last_error.value
        assert ioc.mid1.alarm.severity == AlarmSeverity.INVALID_ALARM
        if fault == "overflow":
            assert ioc.dropped_rows.value == 3
            assert "overflow" in ioc.last_error.value
        assert not any(cmd.startswith(("-xGo", "-xAbort", "-xClose")) for _, cmd in sim.commands)

    asyncio.run(scenario())


def test_pause_drains_without_overflow_and_alarms_preserve_source_time(ioc, sim):
    sim.data_rows = queue.Queue()
    ioc._queue_capacity = 1

    async def scenario():
        await open_with_time(ioc)
        await ioc.acquire.write(1)
        elapsed = int((time.time() - sim.run_start) * 1000) + 5
        sim.data_rows.put(scripted_row(sim, elapsed, 1e-9))
        await publish_when_ready(ioc)
        stamp = ioc.mid1.timestamp
        link = ioc.client._data_link
        await ioc.acquire.write(0)
        for index in range(1, 5):
            sim.data_rows.put(scripted_row(sim, elapsed + index, 1e-9))
        await wait_for_counter(ioc, elapsed + 4)
        await ioc._publish_once()
        assert ioc.queue_depth.value == 0
        assert ioc.dropped_rows.value == 0
        assert ioc.data_state.value == "Paused"
        assert ioc.mid1.timestamp == stamp
        assert ioc.mid1.alarm.severity == AlarmSeverity.INVALID_ALARM
        await ioc.acquire.write(1)
        await ioc._publish_once()
        assert ioc.data_state.value != "Live"
        assert ioc.published_rows.value == 1
        elapsed = int((time.time() - sim.run_start) * 1000) + 5
        sim.data_rows.put(scripted_row(sim, elapsed, 2e-9))
        await publish_when_ready(ioc)
        assert ioc.client._data_link is link
        assert ioc.mid1.timestamp > stamp
        assert ioc.published_rows.value == 2
        assert ioc.data_state.value == "Live"

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


def test_real_ca_server_startup_readback_and_readonly(monkeypatch, tmp_path, sim):
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
    sim.run_start = time.time() - 3600
    sim.data_rows = queue.Queue()
    logfile = tmp_path / "ioc.log"
    with logfile.open("w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(root / "hiden" / "cap3.py"),
                "--prefix",
                "TEST:",
                "--interfaces",
                "127.0.0.1",
                "--mas-host",
                "127.0.0.1",
                "--mas-port",
                str(sim.server_address[1]),
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
            control = "TEST:XF:08IDB-SE{RGA:1}:"
            write(control + "OpenExp", 1, timeout=3, notify=True)
            assert read(control + "Connected", timeout=2).data[0] == 1
            origin = datetime.fromtimestamp(sim.run_start, timezone.utc).isoformat()
            write(control + "SourceStartUTC", origin, timeout=3, notify=True)
            write(control + "Acquire", 1, timeout=3, notify=True)
            assert read(control + "Acquire", timeout=2).data[0] == 1
            sim.data_rows.put(scripted_row(sim, 1000, 123))
            for _ in range(50):
                if read(control + "HistoryRows", timeout=2).data[0] == 1:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("Historical row was not received")
            result = read(value_pv, data_type="time", timeout=2)
            assert result.data[0] == 0
            assert result.metadata.severity == AlarmSeverity.INVALID_ALARM
            assert read(control + "PublishedRows", timeout=2).data[0] == 0
            elapsed = int((time.time() - sim.run_start) * 1000) + 5
            sim.data_rows.put(scripted_row(sim, elapsed, -5.6e-9))
            for _ in range(50):
                result = read(value_pv, data_type="time", timeout=2)
                if result.metadata.severity == AlarmSeverity.NO_ALARM:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("Live row was not published")
            assert result.data[0] == -5.6e-9
            assert result.metadata.timestamp == pytest.approx(
                datetime.fromisoformat(origin).timestamp() + elapsed / 1000,
                abs=1e-6,
                rel=0,
            )
            assert read(control + "PublishedRows", timeout=2).data[0] == 1
            with pytest.raises(Exception, match="[Aa]ccess|[Rr]ead|[Ww]rite"):
                write(control + "SourceTime", 0, timeout=2, notify=True)
            assert not any(
                cmd.startswith(("-xGo", "-xAbort", "-xClose")) for _, cmd in sim.commands
            )
        finally:
            proc.terminate()
            proc.wait(timeout=10)
