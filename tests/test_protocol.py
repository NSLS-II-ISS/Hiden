import math
import socket
import threading
import time

import pytest
from conftest import wait_for
from massoft_protocol import (
    MASsoftConfig,
    MASsoftDisconnected,
    MASsoftProtocolError,
    MASsoftTimeout,
    _CRLFSocket,
    extract_masses,
    load_runtime_config,
    parse_legends,
    parse_numeric_row,
)


@pytest.mark.parametrize("prefix", ["", "0\t", "125\t", "00:00:00\t", "00:00:00\t0\t"])
def test_mid_mapping_never_drops_zero_or_small_counter(prefix):
    values = [0, -1.075e-10, 2.15e-10, 0, 0, 0, 5.4825e-9, 8.0625e-14, 1.29e-9, 0]
    assert (
        parse_numeric_row(
            prefix + "\t".join(map(str, values)),
            expected_count=10,
            include_ms=prefix in ("0\t", "125\t"),
        )
        == values
    )


def test_ambiguous_extra_numeric_column_is_not_guessed():
    with pytest.raises(MASsoftProtocolError):
        parse_numeric_row("0\t1\t2", expected_count=2)


@pytest.mark.parametrize(
    "time_fmt,ms_fmt", [(False, False), (True, False), (False, True), (True, True)]
)
def test_all_requested_time_formats(time_fmt, ms_fmt):
    prefix = ("00:00:00\t" if time_fmt else "") + ("0\t" if ms_fmt else "")
    assert parse_numeric_row(
        prefix + "0\t5e-9", expected_count=2, include_time=time_fmt, include_ms=ms_fmt
    ) == [0, 5e-9]


@pytest.mark.parametrize("row", ["1\t\t3", "1\tbad\t3", "1\tnan\t3", "1\tinf\t3", "1\t2"])
def test_bad_rows_are_rejected_not_compacted(row):
    with pytest.raises(MASsoftProtocolError):
        parse_numeric_row(row, expected_count=3)


def test_single_zero_and_legends():
    assert parse_numeric_row("0", expected_count=1) == [0]
    assert extract_masses(parse_legends('"mass 2"')) == [2]
    for legends in (["mass 2", "unknown", "mass 4"], ["mass 2"] * 21, []):
        with pytest.raises(MASsoftProtocolError):
            extract_masses(legends)


@pytest.mark.parametrize(
    "time_legends",
    [
        [],
        ["Elapsed time"],
        ["Time (ms)"],
        ["Elapsed time", "Time (ms)"],
        ["Real time", "ms"],
        ["Real time", "Time (ms)"],
    ],
)
@pytest.mark.parametrize("unit", ["", " Torr"])
def test_scan_legends_ignore_only_leading_time_metadata(time_legends, unit):
    # Keep wire order, not the numeric Scan ID order.
    legends = time_legends + [f"Scan 7 : mass 40.00{unit}", f"Scan 2 : mass 2.00{unit}"]
    assert extract_masses(legends) == [40.0, 2.0]


@pytest.mark.parametrize("unit", ["", " Torr"])
def test_quoted_historical_legends_and_twenty_mass_limit(unit):
    raw = f'"Elapsed time"\t"Time (ms)"\t"Scan 1 : mass 18.00{unit}"\t"Scan 2 : mass 28.00{unit}"'
    assert extract_masses(parse_legends(raw)) == [18.0, 28.0]
    full = ["Elapsed time", "Time (ms)"] + [f"Scan {i} : mass {i}.00{unit}" for i in range(1, 21)]
    assert extract_masses(full) == list(range(1, 21))
    with pytest.raises(MASsoftProtocolError):
        extract_masses(full + [f"Scan 21 : mass 21.00{unit}"])


def test_reported_tabular_torr_legends_keep_wire_order():
    raw = '"Real time"\t"ms"\t"Scan 1 : mass 2.00 Torr"\t"Scan 9 : mass 40.00 Torr"'
    assert extract_masses(parse_legends(raw)) == [2.0, 40.0]
    assert extract_masses(["  SCAN 1 : MASS 2.00   tOrR  ", "mass 40 Torr"]) == [2, 40]


@pytest.mark.parametrize(
    "legends",
    [
        ["Elapsed time", "Time (ms)"],
        ["Elapsed time", "Elapsed time", "mass 2"],
        ["Real time", "Elapsed time", "mass 2"],
        ["ms", "Real time", "mass 2"],
        ["Time (ms)", "Elapsed time", "mass 2"],
        ["mass 2", "Time (ms)", "mass 40"],
        ["mass 2", "Elapsed time"],
        ["Elapsed time", "Pressure", "mass 2"],
        ["Elapsed time", "Time (ms)", "", "mass 2"],
        ["Scan 1 : unknown 2", "mass 40"],
        ["Scan 1 : mass 2.00Torr"],
        ["Scan 1 : mass 2.00 Torr extra"],
        ["Scan 1 : mass 2.00 Torr Torr"],
        ["Scan 1 : mass 2.00 Amps"],
        ["mass 2 Torr", "unknown", "mass 40 Torr"],
        ["mass 2 Torr", "Real time", "mass 40 Torr"],
    ],
)
def test_unknown_or_misplaced_legends_still_fail(legends):
    with pytest.raises(MASsoftProtocolError):
        extract_masses(legends)


def test_historical_legends_match_stream_values(client, sim):
    sim.legends = ["Elapsed time", "Time (ms)"] + [
        f"Scan {i} : mass {mass:.2f}" for i, mass in enumerate([18, 28, 32, 40, 44], 1)
    ]
    sim.row = "00:00:18\t18521\t0\t-1.075e-10\t2.15e-10\t5.6e-9\t0"
    client.open_experiment("first.exp")
    assert client.fetch_legends() == sim.legends
    client.start_status_link()
    client.start_data_link()
    wait_for(lambda: client.get_latest_row() is not None)
    assert client.get_latest_row() == [0, -1.075e-10, 2.15e-10, 5.6e-9, 0]
    assert client.get_latest_raw_line() == sim.row
    client.check_links()


def test_extended_one_shot_data_counts_masses_not_time_headers(sim):
    from massoft_client import MASsoftClient

    sim.legends = ["Elapsed time", "Time (ms)", "Scan 1 : mass 2.00", "Scan 2 : mass 40.00"]
    sim.row = "00:00:00\t0\t0\t5.6e-9"
    obj = MASsoftClient(
        MASsoftConfig(
            host="127.0.0.1", port=sim.server_address[1], retry_s=0, command_timeout_s=0.8
        )
    )
    try:
        obj.open_experiment("first.exp")
        assert obj.fetch_data_once() == [0.0, 5.6e-9]
    finally:
        obj.disconnect()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"port": 0},
        {"retry_s": -1},
        {"command_timeout_s": 10},
        {"link_chunk_timeout_s": 0},
        {"enable_keepalive": "false"},
        {"command_timeout_s": math.inf},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        MASsoftConfig(**kwargs)


def test_missing_explicit_config_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_runtime_config(str(tmp_path / "missing.json"))


def test_open_links_switch_file_clear_cache(client, sim):
    client.open_experiment("first.exp")
    assert len(client.fetch_legends()) == 20
    client.start_status_link()
    client.start_data_link()
    wait_for(lambda: client.get_latest_row() is not None)
    client.check_links()
    assert client.get_latest_row() == pytest.approx([i * 1e-10 for i in range(20)])
    client.open_experiment("second.exp")
    assert client.current_file.endswith("second.exp")
    assert client.get_latest_row() is None
    assert not any("-xClose" in cmd or "-xAbort" in cmd for _, cmd in sim.commands)


def test_timeout_invalidates_stream_and_does_not_replay(client, sim):
    client.open_experiment("first.exp")
    with pytest.raises(MASsoftTimeout):
        client.command.request("-xSlow", retry_s=0, timeout_s=0.04)
    assert not client.command.is_connected()
    with pytest.raises(MASsoftDisconnected):
        client.x_status()
    assert sum("-xSlow" in cmd for _, cmd in sim.commands) == 1
    client.open_experiment("first.exp")
    assert client.x_status() == "ScanningActive"


def test_hotlink_socket_never_reused_for_commands(client):
    client.open_experiment("first.exp")
    client.start_status_link()
    with pytest.raises(MASsoftProtocolError):
        client.status_sock.send("-xStatus")
    client.start_status_link()  # Must use a fresh, associated socket.
    wait_for(lambda: client.get_latest_status() == "ScanningActive")


def test_close_requires_fresh_stopped_and_abort_success(client, sim):
    client.open_experiment("first.exp")
    client._set_latest_status("StoppedActive")  # Stale cache must not authorize close.
    sim.abort_fails = True
    with pytest.raises(MASsoftProtocolError):
        client.safe_abort_and_close()
    assert not any("-xClose" in cmd for _, cmd in sim.commands)
    sim.abort_fails = False
    assert client.safe_abort_and_close() == "StoppedActive"
    commands = [cmd.split()[0] for _, cmd in sim.commands]
    assert commands[-3:] == ["-xAbort", "-xStatus", "-xClose"]


def test_abort_does_not_accept_cached_stopped(client, sim):
    client.open_experiment("first.exp")
    client._set_latest_status("StoppedActive")
    sim.abort_stuck = True
    with pytest.raises(MASsoftTimeout):
        client.safe_abort_and_wait(timeout_s=0.06)


def test_dead_link_is_reported(client, sim):
    client.open_experiment("first.exp")
    client.start_status_link()
    client.start_data_link()
    sim.drop_links.set()
    wait_for(lambda: not client._data_link.alive)
    with pytest.raises(MASsoftDisconnected):
        client.check_links()


def test_invalid_row_stops_reader_instead_of_shifting_channels(client, sim):
    client.open_experiment("first.exp")
    sim.legends = ["mass 2", "mass 40"]
    sim.row = "00:00:01\t1000\tbad\t5.6e-9"
    client.start_data_link()
    wait_for(lambda: not client._data_link.alive)
    assert client.get_latest_row() is None
    assert "Nonnumeric" in client._data_link.error


def test_command_injection_is_rejected(client):
    client.open_experiment("first.exp")
    with pytest.raises(ValueError):
        client.command.request("-xStatus\r\n-xGo")
    assert client.x_status() == "ScanningActive"


def test_diagnostic_queries_and_extra_hotlink(sim):
    from massoft_client import MASsoftClient

    obj = MASsoftClient(
        MASsoftConfig(
            host="127.0.0.1",
            port=sim.server_address[1],
            retry_s=0,
            command_timeout_s=0.8,
            link_chunk_timeout_s=0.03,
        )
    )
    try:
        obj.open_experiment("first.exp")
        assert obj.request_raw("-xStatus") == "ScanningActive"
        assert obj.x_call("Filename").endswith("first.exp")
        assert len(obj.fetch_data_once()) == 20
        with pytest.raises(ValueError):
            obj.request_raw("-xGo")
        with pytest.raises(ValueError):
            obj.l_call_once("Data -xGo")
        obj.start_hotlink("Status", name="extra")
        wait_for(lambda: obj.get_hotlink_latest("extra") is not None)
        assert obj.get_hotlink_latest("extra") == ["ScanningActive"]
        obj.stop_hotlink("extra")
        assert obj.list_hotlinks() == []
    finally:
        obj.disconnect()


def test_disconnect_before_connect_is_idempotent(client):
    client.disconnect()
    client.disconnect()
    assert client.current_file is None
    assert client.list_hotlinks() == []
    assert client.get_latest_row() is None
    for sock in (client.command_socket, client.status_socket, client.data_socket):
        assert not sock.is_connected()


def test_reopen_cleans_core_and_diagnostic_links(client):
    client.open_experiment("first.exp")
    client.start_status_link()
    client.start_data_link()
    client.start_hotlink("Data", name="diagnostic")
    wait_for(lambda: client.get_hotlink_latest("diagnostic") is not None)
    wait_for(lambda: client.get_latest_row() is not None)
    extra_socket, extra_reader = client._extra_links["diagnostic"]
    readers = (client._status_link, client._data_link, extra_reader)
    client.open_experiment("second.exp")
    assert all(not reader.alive for reader in readers)
    assert not extra_socket.is_connected()
    assert client.list_hotlinks() == []
    assert client.get_hotlink_latest("diagnostic") is None
    assert client.get_hotlink_latest_timestamp("diagnostic") == 0
    assert client.get_latest_row() is None
    assert client.get_latest_raw_line() is None
    assert client.query_filename().endswith("second.exp")


def test_greeting_is_consumed_even_if_delayed(client, sim):
    sim.greeting_delay = 0.08
    client.open_experiment("first.exp")
    assert client.query_filename().endswith("first.exp")


@pytest.mark.parametrize("greeting", ["10", "101", "3968", "10000"])
def test_numeric_greeting_has_no_fixed_digit_count(client, sim, greeting):
    sim.greeting = greeting
    client.open_experiment("first.exp")
    assert client.query_filename().endswith("first.exp")
    assert client.x_status() == "ScanningActive"
    assert len(client.fetch_legends()) == 20


@pytest.mark.parametrize("greeting", ["", "ready", "101 junk"])
def test_invalid_greeting_closes_socket_without_sending_commands(client, sim, greeting):
    sim.greeting = greeting
    with pytest.raises(MASsoftProtocolError, match="invalid greeting"):
        client.open_experiment("first.exp")
    assert not client.command.is_connected()
    assert client.current_file is None
    assert sim.commands == []


def test_fragmented_crlf_and_absolute_read_deadline():
    left, right = socket.socketpair()
    transport = _CRLFSocket("127.0.0.1", 0, name="test", timeout_s=0.1)
    transport._sock = left
    try:
        right.sendall(b"one\r\ntwo\r\n")
        assert transport.read_line() == "one"
        assert transport.read_line() == "two"

        def drip():
            try:
                for _ in range(30):
                    right.sendall(b"x")
                    time.sleep(0.01)
            except OSError:
                pass

        thread = threading.Thread(target=drip)
        thread.start()
        started = time.monotonic()
        with pytest.raises(MASsoftTimeout):
            transport.read_line(timeout_s=0.05)
        assert time.monotonic() - started < 0.2
        transport.close()
        thread.join(1)
    finally:
        transport.close()
        right.close()
