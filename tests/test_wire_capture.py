"""The diagnostic must preserve even rows the production parser would reject."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_massoft_wire.py"
SPEC = importlib.util.spec_from_file_location("capture_massoft_wire", SCRIPT)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)
EXPERIMENT = r"C:\Users\xf08id1\Documents\Hiden Analytical\MASsoft\11\current.exp"


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, timeout):
        assert timeout > 0

    def getpeername(self):
        return ("127.0.0.1", 5026)

    def sendall(self, payload):
        self.sent.append(payload)

    def recv(self, count):
        try:
            return next(self.chunks)
        except StopIteration:
            raise TimeoutError("scripted timeout") from None


@pytest.mark.parametrize("item", ["Legends", "Data"])
def test_wire_bytes_preserved_without_data_decoding_or_filtering(monkeypatch, tmp_path, item):
    records = (
        [b'"Real time"\t"ms"\t"Scan 1 : mass 2.00 Torr"\r\n']
        if item == "Legends"
        else [
            b" 10/1/2026 3:51:41 PM\t43\t4.70849e-09\t\xff \r\n",
            b"\r\n",
            b"0\r\n",
            b"bad\t\tNaN\ttoo many columns\r\n",
            b"0\r\n",
        ]
    )
    # CRLF can be split across TCP reads; one read may contain multiple records.
    chunks = [b"39", b"68\r", b"\n", b"1\r\n", records[0][:-1]]
    extra = b"additional\trow\r\npartial"
    chunks.append(records[0][-1:] + b"".join(records[1:]) + extra)
    sock = FakeSocket(chunks)
    monkeypatch.setattr(capture.socket, "create_connection", lambda *a, **k: sock)
    events = io.StringIO()
    capture.capture_session("127.0.0.1", 5026, EXPERIMENT, 2, item, 5, 1, 1, tmp_path, events)
    assert sock.closed
    stem = f"view2-{item.lower()}"
    assert (tmp_path / f"{stem}.rx.bin").read_bytes() == b"".join(chunks)
    command = b"-lLegends -v2 -d15\r\n" if item == "Legends" else b"-lData -v2 -c1 -t1 -m1 -d15\r\n"
    assert sock.sent == [f'-f"{EXPERIMENT}" -d15\r\n'.encode(), command]
    assert (tmp_path / f"{stem}.tx.bin").read_bytes() == b"".join(sock.sent)
    log = [json.loads(line) for line in events.getvalue().splitlines()]
    reported = [entry["bytes_repr"] for entry in log if entry.get("label", "").startswith(item)]
    assert reported == [repr(row) for row in records]
    assert log[-1]["buffered_bytes"] == len(extra)
    received = [entry for entry in log if entry["event"] == "rx"]
    offset = 0
    for entry, chunk in zip(received, chunks, strict=True):
        assert entry["offset"] == offset
        assert entry["length"] == len(chunk)
        offset += len(chunk)


@pytest.mark.parametrize("association", [b"0\r\n", b"not an acknowledgement\r\n"])
def test_refused_association_never_sends_link(monkeypatch, tmp_path, association):
    sock = FakeSocket([b"3968\r\n", association])
    monkeypatch.setattr(capture.socket, "create_connection", lambda *a, **k: sock)
    with pytest.raises(ValueError, match="association"):
        capture.capture_session(
            "127.0.0.1", 5026, EXPERIMENT, 1, "Data", 5, 1, 1, tmp_path, io.StringIO()
        )
    assert len(sock.sent) == 1
    assert sock.closed


def test_partial_response_is_preserved_on_timeout(monkeypatch, tmp_path):
    chunks = [b"3968\r\n", b"1\r\n", b"10/2/2026 10:00:00 AM\t43\tpartial"]
    sock = FakeSocket(chunks)
    monkeypatch.setattr(capture.socket, "create_connection", lambda *a, **k: sock)
    events = io.StringIO()
    with pytest.raises(TimeoutError):
        capture.capture_session("127.0.0.1", 5026, EXPERIMENT, 1, "Data", 5, 1, 1, tmp_path, events)
    assert (tmp_path / "view1-data.rx.bin").read_bytes() == b"".join(chunks)
    assert json.loads(events.getvalue().splitlines()[-1])["event"] == "error"
    assert sock.closed


@pytest.mark.parametrize(
    "filename", ["current.exp", EXPERIMENT + "\r\n-xGo", EXPERIMENT + '"', r"C:\run.csv"]
)
def test_unsafe_or_incomplete_filename_is_rejected_before_connection(filename):
    with pytest.raises(SystemExit):
        capture.arguments(["--file", filename])


def test_main_captures_both_views_and_refuses_output_overwrite(monkeypatch, tmp_path):
    sessions = []

    def connect(*args, **kwargs):
        sock = FakeSocket([b"3968\r\n", b"1\r\n", b"raw\tresponse\r\n"])
        sessions.append(sock)
        return sock

    monkeypatch.setattr(capture.socket, "create_connection", connect)
    output = tmp_path / "capture"
    args = ["--file", EXPERIMENT, "--rows", "1", "--output", str(output)]
    assert capture.main(args) == 0
    assert len(sessions) == 4
    assert all(sock.closed for sock in sessions)
    assert [sock.sent[1] for sock in sessions] == [
        b"-lLegends -v1 -d15\r\n",
        b"-lData -v1 -c1 -t1 -m1 -d15\r\n",
        b"-lLegends -v2 -d15\r\n",
        b"-lData -v2 -c1 -t1 -m1 -d15\r\n",
    ]
    assert json.loads((output / "summary.json").read_text())["errors"] == []
    with pytest.raises(FileExistsError):
        capture.main(args)
    assert len(sessions) == 4


def test_loopback_capture_keeps_unrecognized_legends_and_data(sim, tmp_path):
    sim.greeting = "3968"
    sim.legends = ["Real time", "ms", "custom label not understood by the IOC"]
    sim.row = " not a date\t\tNaN\t-0.000000\tunknown "
    output = tmp_path / "loopback"
    assert (
        capture.main(
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(sim.server_address[1]),
                "--file",
                EXPERIMENT,
                "--rows",
                "2",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    log = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    for view in (1, 2):
        records = [
            entry["bytes_repr"]
            for entry in log
            if entry["session"] == f"view{view}-data" and entry.get("label", "").startswith("Data[")
        ]
        assert records == [repr((sim.row + "\r\n").encode())] * 2
        raw = (output / f"view{view}-data.rx.bin").read_bytes()
        assert raw.startswith(b"3968\r\n1\r\n")
        assert (sim.row + "\r\n").encode() in raw
