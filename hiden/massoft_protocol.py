"""Shared MASsoft framing, configuration and strict MID parsing.

Protocol reference: Hiden HA-085-109, sections 1.2 and 3.2.
No command is replayed after a timeout: its hardware outcome may be unknown.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path

LOG = logging.getLogger(__name__)
CRLF = b"\r\n"
MAX_LINE_BYTES = 1024 * 1024


class MASsoftError(Exception):
    """Base protocol/transport error."""


class MASsoftDisconnected(MASsoftError, ConnectionError):
    pass


class MASsoftTimeout(MASsoftError, TimeoutError):
    pass


class MASsoftProtocolError(MASsoftError):
    pass


def get_runtime_config_path(config_path=None):
    return Path(
        config_path or os.getenv("HIDEN_CONFIG") or Path(__file__).with_name("hiden_config.json")
    ).expanduser()


def load_runtime_config(config_path=None):
    path = get_runtime_config_path(config_path)
    # Never silently fall back to a real instrument after a path typo.
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Runtime config must be an object: {path}")
    for key in ("massoft", "ioc", "epics", "archiver"):
        if key in raw and not isinstance(raw[key], dict):
            raise ValueError(f"Config {key!r} must be an object")
    return raw


@dataclass(frozen=True)
class MASsoftConfig:
    beamline_name: str = "08IDB"
    host: str = "10.66.58.227"
    port: int = 5026
    experiment_directory: str = r"C:\Users\xf08id1\Documents\Hiden Analytical\MASsoft\11"
    retry_s: int = 15
    command_timeout_s: float = 20.0
    link_chunk_timeout_s: float = 1.0
    link_burst_gap_s: float = 0.1
    enable_keepalive: bool = True

    def __post_init__(self):
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("MASsoft host must be nonempty")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("MASsoft port must be in 1..65535")
        if type(self.retry_s) is not int or self.retry_s < 0:
            raise ValueError("retry_s must be a nonnegative integer")
        for name in ("command_timeout_s", "link_chunk_timeout_s", "link_burst_gap_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.command_timeout_s <= self.retry_s or self.link_chunk_timeout_s <= 0:
            raise ValueError("command_timeout_s must exceed retry_s; link timeout must be positive")
        if type(self.enable_keepalive) is not bool:
            raise ValueError("enable_keepalive must be a JSON boolean")
        if not isinstance(self.experiment_directory, str) or not self.experiment_directory:
            raise ValueError("experiment_directory must be a nonempty string")

    @classmethod
    def from_runtime_config(cls, config_path=None):
        raw = load_runtime_config(config_path)
        cfg = raw.get("massoft", {})
        return cls(**{f.name: cfg.get(f.name, raw.get(f.name, f.default)) for f in fields(cls)})


def quote_path(path):
    if not path or any(c in path for c in '\r\n\0"'):
        raise ValueError("Filename must be nonempty and contain no quotes or control characters")
    return f'"{path}"'


class _CRLFSocket:
    """One stream owner; requests are atomic and timed-out streams are discarded."""

    def __init__(self, host, port, *, name, timeout_s):
        self.host, self.port, self.name = host, port, name
        self._timeout_s = float(timeout_s)
        self._sock = None
        self._buf = bytearray()
        self._req_lock = threading.RLock()
        self._hotlink = False

    def is_connected(self):
        return self._sock is not None

    def connect(self, *, enable_keepalive=True):
        with self._req_lock:
            self.close()
            try:
                self._sock = socket.create_connection((self.host, self.port), self._timeout_s)
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if enable_keepalive:
                    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # Read one numeric greeting before commands. HA-085-109 describes
                # two/three digits, but deployed MASsoft also sends values like 3968.
                greeting = self.read_line(timeout_s=self._timeout_s)
                if not re.fullmatch(r"[0-9]+", greeting.strip()):
                    raise MASsoftProtocolError(f"{self.name}: invalid greeting {greeting!r}")
            except Exception:
                self.close()
                raise
            LOG.info("%s connected to %s:%s", self.name, self.host, self.port)

    def close(self):
        with self._req_lock:
            sock, self._sock = self._sock, None
            self._buf.clear()
            self._hotlink = False
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()

    def send(self, command, *, retry_s=None):
        if any(c in command for c in "\r\n\0") or not command.strip():
            raise ValueError("Send exactly one nonempty CRLF-free MASsoft command")
        if self._hotlink:
            raise MASsoftProtocolError(
                f"{self.name}: reconnect before sending another command on a hot-link"
            )
        if self._sock is None:
            raise MASsoftDisconnected(f"{self.name}: not connected")
        cmd = command.strip()
        if retry_s is not None and not re.search(r"(?:^|\s)-d\d+", cmd):
            cmd += f" -d{int(retry_s)}"
        try:
            self._sock.sendall((cmd + "\r\n").encode("utf-8"))
        except OSError as exc:
            self.close()
            raise MASsoftDisconnected(f"{self.name}: send failed") from exc
        self._hotlink = bool(re.search(r"(?:^|\s)-l[A-Za-z]", cmd))

    def read_line(self, *, timeout_s=None):
        sock = self._sock
        if sock is None:
            raise MASsoftDisconnected(f"{self.name}: not connected")
        deadline = time.monotonic() + (self._timeout_s if timeout_s is None else timeout_s)
        try:
            while True:
                end = self._buf.find(CRLF)
                if end >= 0:
                    if end > MAX_LINE_BYTES:
                        raise MASsoftProtocolError("MASsoft response exceeds line limit")
                    line = bytes(self._buf[:end])
                    del self._buf[: end + 2]
                    return line.decode("utf-8", errors="strict")
                if len(self._buf) > MAX_LINE_BYTES:
                    raise MASsoftProtocolError("MASsoft response exceeds line limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MASsoftTimeout(f"{self.name}: response deadline exceeded")
                sock.settimeout(remaining)
                data = sock.recv(4096)
                if not data:
                    raise MASsoftDisconnected(f"{self.name}: peer closed connection")
                self._buf.extend(data)
        except socket.timeout as exc:
            raise MASsoftTimeout(f"{self.name}: response deadline exceeded") from exc
        except OSError as exc:
            raise MASsoftDisconnected(f"{self.name}: receive failed") from exc

    def request(self, command, *, retry_s=None, timeout_s=None):
        timeout = self._timeout_s if timeout_s is None else timeout_s
        retries = [int(v) for v in re.findall(r"(?:^|\s)-d(\d+)", command)]
        if timeout <= max(retries + [retry_s or 0]):
            raise ValueError("Response timeout must exceed MASsoft retry window")
        with self._req_lock:
            try:
                if self._sock is not None:
                    self._sock.settimeout(timeout)
                self.send(command, retry_s=retry_s)
                return self.read_line(timeout_s=timeout)
            except (MASsoftError, OSError, UnicodeError):
                self.close()
                raise

    def send_command(self, command, *, expect_response=True):
        if not expect_response:
            raise ValueError("Use a dedicated hot-link; command replies must be consumed")
        return self.request(command, timeout_s=self._timeout_s)

    def receive(self):
        return self.read_line()


class MASsoftHotlink:
    def __init__(self, sock, *, chunk_timeout_s, burst_gap_s, on_burst, name):
        self._sock = sock
        self._timeout = min(float(chunk_timeout_s), 1.0)
        self._callback, self._name = on_burst, name
        self._stop = threading.Event()
        self._thread = None
        self.error = None

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.alive:
            raise RuntimeError("Hot-link reader already running")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, *, join_timeout_s=2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(join_timeout_s, self._timeout + 0.5))
            if self._thread.is_alive():
                raise RuntimeError(f"{self._name}: reader did not stop")
            self._thread = None

    def _run(self):
        # Deliver complete CRLF records immediately; continuous streams cannot
        # accumulate an unbounded burst while waiting for an idle gap.
        while not self._stop.is_set():
            try:
                line = self._sock.read_line(timeout_s=self._timeout)
                if not self._stop.is_set():
                    self._callback([line])
            except MASsoftTimeout:
                continue
            except Exception as exc:
                self.error = f"{self._name}: {exc}"
                LOG.warning("%s", self.error)
                return


def parse_legends(line):
    if "\t" in line:
        return [p.strip().strip('"') for p in line.split("\t")]
    # A single mass is valid; do not split "mass 2" into two columns.
    return [line.strip().strip('"')]


def extract_masses(legends):
    labels = [legend.strip() for legend in legends]
    start = 0
    # MASsoft can prepend these time columns to its legends response. Only
    # consume the known leading prefix; never discard arbitrary/interleaved cells.
    for time_labels in (("elapsed time", "real time"), ("time (ms)", "ms")):
        if start < len(labels) and " ".join(labels[start].split()).casefold() in time_labels:
            start += 1
    masses = []
    for legend in labels[start:]:
        # Tabular views may append Torr; accept that known suffix, not arbitrary text.
        match = re.fullmatch(
            r"(?:scan\s+[0-9]+\s*:\s*)?mass\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s+Torr)?",
            legend,
            re.IGNORECASE,
        )
        if match is None:
            raise MASsoftProtocolError(
                f"Unsupported MID legend {legend!r}; column order cannot be established"
            )
        masses.append(float(match.group(1)))
    if not 1 <= len(masses) <= 20:
        raise MASsoftProtocolError("A MID view must have 1..20 mass legends")
    return masses


def parse_numeric_row(line, *, expected_count=None, include_time=False, include_ms=False):
    values, _ = parse_data_row(
        line, expected_count=expected_count, include_time=include_time, include_ms=include_ms
    )
    return values


def parse_data_row(line, *, expected_count=None, include_time=False, include_ms=False):
    """Return (MID values, elapsed milliseconds), retaining source timing metadata."""
    # Tab splitting preserves empty data cells. Dropping nonnumeric cells shifts
    # every subsequent mass, creating plausible but scientifically wrong data.
    parts = [p.strip() for p in line.split("\t")] if "\t" in line else line.split()
    elapsed_ms = None
    has_time = bool(parts and re.fullmatch(r"\d+:\d{2}:\d{2}(?:\.\d+)?", parts[0]))
    if has_time:
        parts = parts[1:]
    elif include_time:
        raise MASsoftProtocolError("Missing elapsed-time column")
    if expected_count is not None:
        if len(parts) == expected_count + 1:
            if not has_time and not include_ms:
                raise MASsoftProtocolError(
                    "Ambiguous extra column; enable DataMsFmt for an ms-only prefix"
                )
            if not re.fullmatch(r"\d+", parts[0]):
                raise MASsoftProtocolError("Invalid millisecond counter")
            elapsed_ms = int(parts[0])
            parts = parts[1:]
        elif include_ms:
            raise MASsoftProtocolError("Missing millisecond counter")
        if len(parts) != expected_count:
            raise MASsoftProtocolError(
                f"Expected {expected_count} MID values, received {len(parts)}"
            )
    elif include_ms:
        if not parts or not re.fullmatch(r"\d+", parts[0]):
            raise MASsoftProtocolError("Missing millisecond counter")
        elapsed_ms = int(parts[0])
        parts = parts[1:]
    elif has_time:
        raise MASsoftProtocolError("Expected channel count required for optional time columns")
    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise MASsoftProtocolError("Nonnumeric or empty MID cell") from exc
    if not values or not all(math.isfinite(v) for v in values):
        raise MASsoftProtocolError("MID row is empty or contains nonfinite values")
    return values, elapsed_ms


def parse_realtime_row(line, *, expected_count):
    """Preserve the tabular date/time and ms fields without guessing their relationship."""
    if "\t" not in line:
        raise MASsoftProtocolError("Real-time rows require tab-separated date/time, ms and masses")
    parts = line.split("\t")
    if len(parts) != expected_count + 2:
        raise MASsoftProtocolError(f"Expected date/time, ms and {expected_count} MID values")
    if not re.fullmatch(r"\d+", parts[1].strip()):
        raise MASsoftProtocolError("Missing or invalid MASsoft millisecond field")
    time_text, data = line.split("\t", 1)
    time_text = time_text.strip()
    if not time_text or "/" not in time_text:
        raise MASsoftProtocolError(
            "Missing MASsoft date/time; select Real Time in the tabular view"
        )
    values, milliseconds = parse_data_row(data, expected_count=expected_count, include_ms=True)
    return values, milliseconds, time_text
