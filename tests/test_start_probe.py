"""No external endpoints: timing model and loopback-only start observation."""

import json
import math
import queue
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import wait_for
from massoft_protocol import MASsoftConfig, MASsoftProtocolError
from massoft_start_probe import StartObservation, StatusTimingProbe
from massoft_timing import MASsoftClock

BASE = 1790950000.0
FILE = r"C:\MASsoft\fresh.exp"
CLOCK = MASsoftClock("America/New_York", "mdy")


def raw(epoch, ms, values="-1e-9\t2e-10"):
    text = datetime.fromtimestamp(epoch, ZoneInfo("America/New_York")).strftime(
        "%m/%d/%Y %I:%M:%S %p"
    )
    return f"{text}\t{ms}\t{values}"


def model(rows=5, correction=0):
    return StartObservation(
        file=FILE,
        view=2,
        masses=[2, 12],
        clock=CLOCK,
        correction_s=correction,
        rows=rows,
        wall=BASE,
        monotonic=100,
    )


def status(m, text, age):
    m.status(text, wall=BASE + age, monotonic=100 + age)


def add(m, text, age):
    m.data(text, wall=BASE + age, monotonic=100 + age)


def started(rows=5, correction=0):
    m = model(rows, correction)
    status(m, "StoppedActive", 0)
    status(m, "StartingActive", 1)
    status(m, "ScanningActive", 2)
    return m


def test_status_origin_is_an_estimate_with_explicit_correction_not_a_time_source():
    m = started(rows=2, correction=0.25)
    add(m, raw(BASE + 1.4, 400), 2.5)
    add(m, raw(BASE + 1.7, 700), 2.6)
    r = m.snapshot()
    assert r["state"] == "Complete"
    assert r["status_origin_uncorrected_unix"] == pytest.approx(BASE + 1.6)
    assert r["status_origin_estimate_unix"] == pytest.approx(BASE + 1.35)
    assert r["calendar_origin_interval_unix"] == pytest.approx([BASE + 0.6, BASE + 1.3])
    assert r["combined_acquisition_notification_delay_interval_s"] == pytest.approx([0.3, 1.0])
    assert not r["accuracy_certified"]
    assert not r["production_timestamps_changed"]
    assert r["first_row"]["values"] == [-1e-9, 2e-10]
    assert r["first_row"]["status_estimated_row_start_unix"] == pytest.approx(BASE + 1.75)
    assert r["last_row"]["status_estimated_row_start_unix"] - r["first_row"][
        "status_estimated_row_start_unix"
    ] == pytest.approx(0.3, abs=1e-6)
    assert "rows" not in r
    assert len(m.snapshot(full=True)["rows"]) == 2


@pytest.mark.parametrize(
    "initial", ["ScanningActive", "StartingActive", "Disconnected", "0", "Paused"]
)
def test_initial_snapshot_is_not_a_new_start(initial):
    m = model()
    with pytest.raises(MASsoftProtocolError, match="Initial status"):
        status(m, initial, 1)
    assert m.transition is None


def test_stopped_shutdown_and_older_servers_without_starting_status():
    m = model()
    status(m, '"StoppedShutdown"', 0)
    status(m, "StoppedActive", 0.5)
    status(m, "ScanningActive", 1)
    assert m.state == "Capturing"


def test_duplicate_status_does_not_move_anchor_and_resume_fails():
    m = started()
    status(m, "ScanningActive", 3)
    assert m.transition["receipt_unix"] == BASE + 2
    with pytest.raises(MASsoftProtocolError, match="Unexpected status"):
        status(m, "StoppedActive", 4)


def test_aborted_start_fails():
    m = model()
    status(m, "StoppedActive", 0)
    status(m, "StartingActive", 1)
    with pytest.raises(MASsoftProtocolError, match="Unexpected status"):
        status(m, "StoppedActive", 2)


@pytest.mark.parametrize("ms", [200, 399, 400])
def test_repeated_or_reset_counter_fails(ms):
    m = started()
    add(m, raw(BASE + 1, 400), 2.5)
    with pytest.raises(MASsoftProtocolError, match="counter"):
        add(m, raw(BASE + 1, ms), 3)


def test_history_future_invalid_and_inconsistent_calendar_are_not_calibration():
    for line, error in [
        (raw(BASE - 10, 400), "Historical"),
        (raw(BASE + 20, 400), "Future"),
        ("00:00:00\t400\t1\t2", "Real Time|date/time"),
        ("0", "row|data|column"),
    ]:
        with pytest.raises(MASsoftProtocolError, match=error):
            add(started(), line, 3)
    m = started()
    add(m, raw(BASE + 1, 400), 3)
    with pytest.raises(MASsoftProtocolError, match="intervals disagree"):
        add(m, raw(BASE + 4, 500), 5)


def test_clock_step_in_status_or_data_is_rejected():
    m = started()
    with pytest.raises(MASsoftProtocolError, match="clock stepped"):
        m.data(raw(BASE + 1, 400), wall=BASE + 3, monotonic=102)
    with pytest.raises(MASsoftProtocolError, match="clock stepped"):
        m.status("ScanningActive", wall=BASE + 3, monotonic=102)


def test_five_recorded_jorge2_counters_leave_823_ms_origin_interval():
    m = started()
    for second, ms in zip([0, 2, 4, 6, 8], [443, 2416, 4389, 6362, 8266]):
        add(m, raw(BASE + 1 + second, ms), 3 + second)
    assert m.snapshot()["calendar_origin_width_s"] == pytest.approx(0.823, abs=1e-6)


def test_varying_phases_narrow_bounds_but_identical_phases_do_not():
    m = started(rows=100)
    for i in range(100):
        ms = 100 + i * 1010
        add(m, raw(BASE + 1.2 + ms / 1000, ms), 4 + ms / 1000)
    assert m.snapshot()["calendar_origin_width_s"] < 0.011
    m = started(rows=20)
    for i in range(20):
        ms = 100 + i * 1000
        add(m, raw(BASE + 1.2 + ms / 1000, ms), 4 + ms / 1000)
    assert m.snapshot()["calendar_origin_width_s"] == 1


@pytest.mark.parametrize("correction", [-1, 61, math.inf, math.nan])
def test_bad_corrections(correction):
    with pytest.raises(ValueError, match="TimingCorrection"):
        model(correction=correction)


@pytest.mark.parametrize("rows", [0, 1001, True, 1.5])
def test_bad_row_limits(rows):
    with pytest.raises(ValueError, match="TimingRows"):
        model(rows=rows)


@pytest.fixture
def probe(sim, tmp_path):
    sim.status = "StoppedActive"
    sim.data_rows = queue.Queue()
    obj = StatusTimingProbe(
        MASsoftConfig(
            host="127.0.0.1", port=sim.server_address[1], command_timeout_s=0.8, retry_s=0
        ),
        file=FILE,
        view=2,
        masses=[2, 12],
        clock=CLOCK,
        rows=2,
        output_dir=tmp_path,
        timeout_s=5,
    )
    yield obj
    obj.stop()


def test_loopback_capture_waits_for_manual_start_and_saves_evidence(probe, sim):
    probe.start()
    wait_for(lambda: probe.snapshot()["state"] == "Armed")
    assert not any(cmd.startswith("-lData") for _, cmd in sim.commands)
    sim.status = "StartingActive"
    wait_for(lambda: probe.snapshot()["state"] == "Starting")
    sim.status = "ScanningActive"
    wait_for(lambda: any(cmd.startswith("-lData") for _, cmd in sim.commands))
    sim.data_rows.put(raw(time.time(), 10))
    sim.data_rows.put(raw(time.time(), 20))
    wait_for(lambda: not probe.running)
    r = probe.snapshot()
    assert r["state"] == "Complete", r
    assert r["saved"]
    with open(r["output_file"]) as stream:
        stored = json.load(stream)
    assert len(stored["rows"]) == 2
    assert stored["transition"]["receipt_unix"] <= stored["first_row"]["receipt_unix"]
    for _, cmd in sim.commands:
        assert cmd.startswith(("-f", "-lStatus -v1", "-xFilename", "-lData -v2"))


def test_already_running_does_not_open_data_link(probe, sim):
    sim.status = "ScanningActive"
    probe.start()
    wait_for(lambda: not probe.running)
    assert probe.snapshot()["state"] == "Error"
    assert "Initial status" in probe.snapshot()["error"]
    assert not any(cmd.startswith("-lData") for _, cmd in sim.commands)


def test_cancel_armed_probe_is_not_an_abort(probe, sim):
    probe.start()
    wait_for(lambda: probe.snapshot()["state"] == "Armed")
    probe.stop()
    r = probe.snapshot()
    assert r["state"] == "Cancelled"
    assert r["saved"]
    assert not any(cmd.startswith("-x") for _, cmd in sim.commands)


def test_disconnect_invalidates_probe_without_reconnecting(probe, sim):
    probe.start()
    wait_for(lambda: probe.snapshot()["state"] == "Armed")
    sim.drop_links.set()
    wait_for(lambda: not probe.running)
    assert probe.snapshot()["state"] == "Error"
    assert sum(cmd.startswith("-lStatus") for _, cmd in sim.commands) == 1


def test_timeout_is_bounded_and_evidence_retained(probe):
    probe.timeout_s = 0.2
    probe.start()
    wait_for(lambda: not probe.running)
    assert probe.snapshot()["state"] == "Error"
    assert "budget" in probe.snapshot()["error"]
    assert probe.snapshot()["saved"]


def test_renamed_file_fails_before_data_request(probe, sim):
    probe.start()
    wait_for(lambda: probe.snapshot()["state"] == "Armed")
    sim.filename_override = r"C:\MASsoft\renamed.exp"
    sim.status = "ScanningActive"
    wait_for(lambda: not probe.running)
    assert "Filename changed" in probe.snapshot()["error"]
    assert not any(cmd.startswith("-lData") for _, cmd in sim.commands)
