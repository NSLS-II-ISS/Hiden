"""Independent MASsoft wire capture: standard library only, no IOC/client imports.

Only the greeting and file-association reply are validated. Data and legends
are NOT decoded, split into cells, converted, sorted, or filtered. CRLF framing
is used to count records; .rx.bin preserves every byte returned by recv(),
including partial records and any extra records in the final receive chunk.
"""

import argparse
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

CRLF = b"\r\n"
RETRY_SECONDS = 15
REPLY_TIMEOUT = 30
DATA_TIMEOUT = 60
MAX_RECEIVE_BYTES = 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class WireCapture:
    def __init__(self, sock, rx, tx, events, session):
        self.sock, self.rx, self.tx = sock, rx, tx
        self.events, self.session = events, session
        self.buffer = bytearray()
        self.received = 0

    def event(self, kind, **details):
        self.events.write(
            json.dumps(
                {"session": self.session, "event": kind, "observed_utc": utc_now(), **details}
            )
            + "\n"
        )
        self.events.flush()

    def send(self, command):
        payload = command.encode("utf-8") + CRLF
        self.event("tx_attempt", bytes_repr=repr(payload), bytes_hex=payload.hex())
        self.sock.settimeout(REPLY_TIMEOUT)
        self.sock.sendall(payload)
        self.tx.write(payload)
        self.tx.flush()
        self.event("tx_complete", length=len(payload))
        print(f"{self.session} TX: {payload!r}", flush=True)

    def record(self, label, deadline):
        while True:
            end = self.buffer.find(CRLF)
            if end >= 0:
                payload = bytes(self.buffer[: end + len(CRLF)])
                del self.buffer[: end + len(CRLF)]
                self.event("record", label=label, bytes_repr=repr(payload))
                print(f"{self.session} {label}: {payload!r}", flush=True)
                return payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for {label}; received bytes are preserved")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(4096)
            observed = utc_now()
            if not chunk:
                raise ConnectionError(f"Peer closed before {label}; received bytes are preserved")
            offset = self.received
            self.rx.write(chunk)
            self.rx.flush()
            self.received += len(chunk)
            self.event("rx", received_utc=observed, offset=offset, length=len(chunk))
            self.buffer.extend(chunk)
            if self.received > MAX_RECEIVE_BYTES:
                raise ValueError("Capture exceeded 1 MiB per connection; stopping")


def capture_session(host, port, experiment, view, item, rows, time_fmt, ms_fmt, output, events):
    session = f"view{view}-{item.lower()}"
    with (
        (output / f"{session}.rx.bin").open("xb") as rx,
        (output / f"{session}.tx.bin").open("xb") as tx,
    ):
        with socket.create_connection((host, port), timeout=10) as sock:
            wire = WireCapture(sock, rx, tx, events, session)
            wire.event("connected", peer=list(sock.getpeername()))
            try:
                greeting = wire.record("greeting", time.monotonic() + REPLY_TIMEOUT)
                if not greeting[:-2].strip().isdigit():
                    raise ValueError("Non-numeric greeting; no commands sent")
                wire.send(f'-f"{experiment}" -d{RETRY_SECONDS}')
                reply = wire.record("association", time.monotonic() + REPLY_TIMEOUT)
                if not reply[:-2].strip().isdigit() or int(reply[:-2]) == 0:
                    raise ValueError("File association was not acknowledged; no link requested")
                command = f"-l{item} -v{view}"
                if item == "Data":
                    command += f" -c1 -t{time_fmt} -m{ms_fmt}"
                wire.send(command + f" -d{RETRY_SECONDS}")
                deadline = time.monotonic() + DATA_TIMEOUT
                for number in range(1, (rows if item == "Data" else 1) + 1):
                    wire.record(f"{item}[{number}]", deadline)
                wire.event("capture_complete", buffered_bytes=len(wire.buffer))
            except Exception as exc:
                wire.event("error", error=f"{type(exc).__name__}: {exc}")
                raise
            # A link owns its socket. Send no further commands, especially not -xClose.


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="10.66.58.227")
    parser.add_argument("--port", type=int, default=5026)
    parser.add_argument("--file", required=True, help="Full Windows path to the already-open .exp")
    parser.add_argument("--rows", type=int, default=5, help="Data records per view (1..20)")
    parser.add_argument("--time-format", type=int, choices=(0, 1), default=1)
    parser.add_argument("--ms-format", type=int, choices=(0, 1), default=1)
    parser.add_argument("--output", type=Path, help="New output directory; existing paths refused")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.rows <= 20:
        parser.error("port must be 1..65535 and rows must be 1..20")
    if (
        not PureWindowsPath(args.file).is_absolute()
        or PureWindowsPath(args.file).suffix.lower() != ".exp"
        or any(ord(char) < 32 or char == '"' or ord(char) == 127 for char in args.file)
    ):
        parser.error("--file requires a full Windows .exp path without quotes/control characters")
    return args


def main(argv=None):
    args = arguments(argv)
    output = args.output or Path("analysis") / datetime.now(timezone.utc).strftime(
        "massoft-wire-%Y%m%dT%H%M%S-%fZ"
    )
    output.mkdir(parents=True, exist_ok=False)
    print(f"Capture directory: {output.resolve()}", flush=True)
    print("No IOC imports, field parsing, time conversion, or hardware-control commands.")
    errors = []
    with (output / "events.jsonl").open("x", encoding="utf-8", newline="\n") as events:
        for view in (1, 2):
            for item in ("Legends", "Data"):
                try:
                    capture_session(
                        args.host,
                        args.port,
                        args.file,
                        view,
                        item,
                        args.rows,
                        args.time_format,
                        args.ms_format,
                        output,
                        events,
                    )
                except (OSError, ValueError) as exc:
                    message = f"view{view}-{item.lower()}: {type(exc).__name__}: {exc}"
                    errors.append(message)
                    print(message, file=sys.stderr, flush=True)
    (output / "summary.json").write_text(
        json.dumps(
            {
                "host": args.host,
                "port": args.port,
                "file": args.file,
                "views": [1, 2],
                "records_requested_per_view": args.rows,
                "time_format": args.time_format,
                "ms_format": args.ms_format,
                "errors": errors,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Complete. Errors: {len(errors)}. Original bytes: *.rx.bin; transcript: events.jsonl")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
