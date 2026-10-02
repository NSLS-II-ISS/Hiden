"""Unified MASsoft client. No failed hardware command is automatically replayed.

Command/status/data streams have separate owners. Diagnostic one-shot requests
use temporary sockets; the IOC serializes lifecycle operations.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import replace
from pathlib import PureWindowsPath
from typing import Callable, Sequence

from massoft_protocol import (
    MASsoftConfig,
    MASsoftDisconnected,
    MASsoftError,
    MASsoftHotlink,
    MASsoftProtocolError,
    MASsoftTimeout,
    _CRLFSocket,
    extract_masses,
    get_runtime_config_path,
    load_runtime_config,
    parse_data_row,
    parse_legends,
    parse_numeric_row,
    parse_realtime_row,
    quote_path,
)

MAS_HOST = "10.66.58.227"
MAS_PORT = 5026
EXPERIMENT_DIRECTORY = MASsoftConfig.experiment_directory
EXPERIMENT_DIRECTORY_ENV = "HIDEN_FilePath"
MOST_RECENT_FILE = "HIDEN_LastFile"
TEMPLATE_DICT = {f"exp{i}": f"HIDEN_{i}.exp" for i in range(1, 5)}


class MASsoftClient:
    def __init__(self, cfg=None, *, config_path=None, host=None, port=None):
        cfg = cfg or MASsoftConfig.from_runtime_config(config_path)
        self.cfg = replace(
            cfg, **{k: v for k, v in {"host": host, "port": port}.items() if v is not None}
        )
        self.command = self._socket("MASsoftCommand")
        self.status_sock = self._socket("MASsoftStatus")
        self.data_sock = self._socket("MASsoftData")
        self.current_file = None
        self._command_assoc_file = self._status_assoc_file = self._data_assoc_file = None
        self._status_link = self._data_link = None
        self._latest_lock = threading.Lock()
        self._extra_links_lock = threading.Lock()
        self._extra_links = {}
        self._extra_latest_lock = threading.Lock()
        self._extra_latest = {}
        self._extra_latest_ts = {}
        self._reset_cache()

    def _socket(self, name):
        return _CRLFSocket(
            self.cfg.host, self.cfg.port, name=name, timeout_s=self.cfg.command_timeout_s
        )

    def _reset_cache(self):
        with self._latest_lock:
            self._latest_status = self._latest_row = self._latest_raw_row = None
            self._latest_status_ts = self._latest_row_ts = self._latest_raw_row_ts = 0.0
            self._latest_row_wall_ts = 0.0
            self._last_error = None
            self._expected_count = None
            self._timing = None

    def connect(self):
        self.disconnect()
        try:
            for sock in (self.command, self.status_sock, self.data_sock):
                sock.connect(enable_keepalive=self.cfg.enable_keepalive)
        except Exception:
            self.disconnect()
            raise

    def disconnect(self):
        self.stop_links()
        for sock in (self.command, self.status_sock, self.data_sock):
            sock.close()
        self.current_file = None
        self._command_assoc_file = self._status_assoc_file = self._data_assoc_file = None
        self._reset_cache()
        with self._extra_latest_lock:
            self._extra_latest.clear()
            self._extra_latest_ts.clear()

    def _resolve_path(self, name):
        name = str(name).strip()
        quote_path(name)
        if name.startswith(("%", "\\\\")) or ":" in name:
            return str(PureWindowsPath(name)) if not name.startswith("%") else name
        return str(PureWindowsPath(self.cfg.experiment_directory) / name)

    def _request(self, cmd, *, sock=None, retry_s=None, timeout_s=None):
        return (
            (sock or self.command)
            .request(
                cmd,
                retry_s=self.cfg.retry_s if retry_s is None else retry_s,
                timeout_s=self.cfg.command_timeout_s if timeout_s is None else timeout_s,
            )
            .strip()
        )

    def _associate(self, sock, path, *, retry_s=None):
        if self._request(f"-f{quote_path(path)}", sock=sock, retry_s=retry_s) in ("", "0"):
            raise MASsoftProtocolError(f"{sock.name}: failed to associate {path}")

    def open_experiment(self, file_name_or_path=None, *, retry_s=None):
        if file_name_or_path is None:
            file_name_or_path = self.query_filename()
        if isinstance(file_name_or_path, (tuple, list)):
            file_name_or_path = file_name_or_path[0]
        path = self._resolve_path(file_name_or_path)
        # HA-085-109: an associated socket cannot switch to a different file.
        self.connect()
        try:
            for sock in (self.command, self.status_sock, self.data_sock):
                self._associate(sock, path, retry_s=retry_s)
            self.current_file = path
            self._command_assoc_file = self._status_assoc_file = self._data_assoc_file = path
        except Exception:
            self.disconnect()
            raise
        return path

    def query_filename(self, *, retry_s=None, update_current=True):
        path = self._request("-xFilename", retry_s=retry_s).strip('"')
        if path in ("", "0"):
            raise MASsoftProtocolError("MASsoft refused -xFilename")
        quote_path(path)
        if update_current:
            self.current_file = path
        return path

    def x_status(self, *, retry_s=None):
        status = self._request("-xStatus", retry_s=retry_s)
        if status in ("", "0"):
            raise MASsoftProtocolError("MASsoft refused -xStatus")
        return status

    def x_go(self, *, filename=None, od=True, ot=True, retry_s=None):
        cmd = "-xGo"
        if filename:
            cmd += " " + quote_path(filename)
        if od or ot:
            cmd += " -O" + ("d" if od else "") + ("t" if ot else "")
        if self._request(cmd, retry_s=retry_s) in ("", "0"):
            raise MASsoftProtocolError("MASsoft refused -xGo")
        self.query_filename()

    def x_abort(self, *, retry_s=None):
        if self._request("-xAbort", retry_s=retry_s) in ("", "0"):
            raise MASsoftProtocolError("MASsoft refused -xAbort")

    def x_close(self, *, retry_s=None):
        if self._request("-xClose", retry_s=retry_s) in ("", "0"):
            raise MASsoftProtocolError("MASsoft refused -xClose")
        self.disconnect()

    def safe_abort_and_wait(self, *, timeout_s=30.0):
        self.x_abort()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # Cached Stopped* may predate this abort. Require a fresh reply.
            remaining = deadline - time.monotonic()
            status = self._request(
                "-xStatus", retry_s=0, timeout_s=min(self.cfg.command_timeout_s, remaining)
            )
            self._set_latest_status(status)
            if status.lower().startswith("stopped"):
                return status
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        raise MASsoftTimeout("Abort did not reach a fresh Stopped* status; file not closed")

    def safe_abort_and_close(self, *, abort_timeout_s=30.0, close_retry_s=None, reconnect=False):
        # Never close following an unknown state or failed abort.
        status = self.x_status()
        if not status.lower().startswith("stopped"):
            status = self.safe_abort_and_wait(timeout_s=abort_timeout_s)
        self.x_close(retry_s=close_retry_s)
        if reconnect:
            self.connect()
        return status

    def _prepare_link(self, kind):
        if self.current_file is None:
            raise MASsoftProtocolError("OpenExp is required before acquiring")
        sock = getattr(self, f"{kind}_sock")
        link = getattr(self, f"_{kind}_link")
        if link is not None:
            link.stop()
            setattr(self, f"_{kind}_link", None)
            sock.close()
        if not sock.is_connected():
            sock.connect(enable_keepalive=self.cfg.enable_keepalive)
            self._associate(sock, self.query_filename())
        # Existing associations survive -xGo renaming. Do not reassociate them.
        return sock

    def start_status_link(self, *, view=1):
        sock = self._prepare_link("status")
        sock.send(f"-lStatus -v{int(view)}", retry_s=self.cfg.retry_s)

        def receive(lines):
            for line in lines:
                if line in ("", "0"):
                    raise MASsoftProtocolError("MASsoft refused status link")
                self._set_latest_status(line)

        self._status_link = self._link(sock, receive, "MASsoftStatusHotlink")

    def start_data_link(self, *, view=1, mid_cycles=1, include_time=False, include_ms=False):
        if self._expected_count is None:
            self.fetch_legends(view=view)
        sock = self._prepare_link("data")
        cmd = f"-lData -v{int(view)}"
        if mid_cycles != 1 or include_time or include_ms:
            cmd += f" -c{int(mid_cycles)} -t{int(include_time)} -m{int(include_ms)}"
        sock.send(cmd, retry_s=self.cfg.retry_s)

        guard = self._timing

        def receive(lines):
            for line in lines:
                # A one-channel zero is a valid measurement, not an error code.
                source_text = ""
                if guard is not None and guard.clock is not None:
                    row, elapsed_ms, source_text = parse_realtime_row(
                        line, expected_count=self._expected_count
                    )
                else:
                    row, elapsed_ms = parse_data_row(
                        line,
                        expected_count=self._expected_count,
                        include_time=include_time,
                        include_ms=include_ms,
                    )
                with self._latest_lock:
                    self._latest_row = row
                    self._latest_raw_row = line
                    self._latest_row_ts = self._latest_raw_row_ts = time.monotonic()
                    # Receipt time only: a new MASsoft link may replay old experiment rows.
                    self._latest_row_wall_ts = time.time()
                    if self._timing is not None:
                        self._timing.push(
                            row,
                            line,
                            elapsed_ms,
                            wall=self._latest_row_wall_ts,
                            monotonic=self._latest_row_ts,
                            source_text=source_text,
                        )

        self._data_link = self._link(sock, receive, "MASsoftDataHotlink")

    def _link(self, sock, callback, name):
        link = MASsoftHotlink(
            sock,
            chunk_timeout_s=self.cfg.link_chunk_timeout_s,
            burst_gap_s=self.cfg.link_burst_gap_s,
            on_burst=callback,
            name=name,
        )
        link.start()
        return link

    def stop_links(self, *, close_core_sockets=True):
        for kind in ("status", "data"):
            link = getattr(self, f"_{kind}_link")
            if link is not None:
                link.stop()
                setattr(self, f"_{kind}_link", None)
                # Stopping a reader does not turn a hot-link into a command socket.
                getattr(self, f"{kind}_sock").close()
            elif close_core_sockets:
                getattr(self, f"{kind}_sock").close()
        self.stop_all_hotlinks()

    def reconnect_link_sockets(self):
        self.stop_links()
        for kind in ("status", "data"):
            self._prepare_link(kind)

    def check_links(self):
        for link in (self._status_link, self._data_link):
            if link is None or not link.alive:
                raise MASsoftDisconnected(
                    link.error if link and link.error else "Hot-link reader stopped"
                )

    def fetch_legends(self, *, view=1):
        if self.current_file is None:
            raise MASsoftProtocolError("OpenExp is required before fetching legends")
        tmp = self._socket("MASsoftLegendsTmp")
        try:
            tmp.connect(enable_keepalive=self.cfg.enable_keepalive)
            self._associate(tmp, self.query_filename())
            line = self._request(f"-lLegends -v{int(view)}", sock=tmp)
            legends = parse_legends(line)
            self._expected_count = len(extract_masses(legends))
            return legends
        finally:
            tmp.close()

    def snapshot(self):
        with self._latest_lock:
            return {
                "status": self._latest_status,
                "status_ts": self._latest_status_ts,
                "row": None if self._latest_row is None else list(self._latest_row),
                "row_ts": self._latest_row_ts,
                "row_wall_ts": self._latest_row_wall_ts,
                "raw": self._latest_raw_row,
                "raw_ts": self._latest_raw_row_ts,
                "error": self._last_error,
                "timing": None if self._timing is None else self._timing.snapshot(),
            }

    def configure_timing(self, guard):
        if self._data_link is not None and self._data_link.alive:
            raise ValueError("Stop data link before configuring source timing")
        with self._latest_lock:
            self._timing = guard

    def enable_publication(self, enabled, *, not_before=None):
        with self._latest_lock:
            if self._timing is None:
                if enabled:
                    raise ValueError("Set SourceStartUTC before Acquire")
                return
            self._timing.set_enabled(enabled, not_before=not_before)

    def drain_live_rows(self):
        with self._latest_lock:
            if self._timing is None:
                raise ValueError("Set SourceStartUTC before Acquire")
            return self._timing.drain(wall=time.time(), monotonic=time.monotonic())

    def check_source_clock(self):
        with self._latest_lock:
            if self._timing is not None:
                self._timing.check_clock(time.time(), time.monotonic())

    def note_dropped_row(self):
        with self._latest_lock:
            self._timing.dropped_rows += 1

    def _set_latest_status(self, status):
        with self._latest_lock:
            self._latest_status, self._latest_status_ts = status, time.monotonic()

    def _set_last_error(self, msg):
        with self._latest_lock:
            self._last_error = msg

    def get_latest_status(self):
        return self.snapshot()["status"]

    def get_latest_status_timestamp(self):
        return self.snapshot()["status_ts"]

    def get_latest_row(self):
        return self.snapshot()["row"]

    def get_latest_row_timestamp(self):
        return self.snapshot()["row_ts"]

    def get_latest_raw_line(self):
        return self.snapshot()["raw"]

    def get_latest_raw_line_timestamp(self):
        return self.snapshot()["raw_ts"]

    def get_last_error(self):
        return self.snapshot()["error"]

    # Compatibility and commissioning API; no second client implementation.
    parse_legends = staticmethod(parse_legends)
    parse_numeric_row = staticmethod(parse_numeric_row)
    open_experiment_commands = open_experiment

    @property
    def command_socket(self) -> _CRLFSocket:
        return self.command

    @property
    def status_socket(self) -> _CRLFSocket:
        return self.status_sock

    @property
    def data_socket(self) -> _CRLFSocket:
        return self.data_sock

    def initialize(self) -> None:
        """Compat: connect all sockets.  Delegates to ``connect()``."""
        self.connect()

    def shutdown(self) -> None:
        """Compat: close all sockets.  Delegates to ``disconnect()``."""
        self.disconnect()

    def _socket_by_name(self, name: str) -> _CRLFSocket:
        """Return a core socket by friendly name."""
        n = name.strip().lower()
        if n in ("command", "cmd"):
            return self.command
        if n in ("status", "stat"):
            return self.status_sock
        if n in ("data",):
            return self.data_sock
        msg = f"Unknown socket name: {name!r}"
        raise ValueError(msg)

    def _mark_associated_file(self, sock: _CRLFSocket, path: str) -> None:
        """Update per-socket association tracking."""
        if sock is self.command:
            self._command_assoc_file = path
        elif sock is self.status_sock:
            self._status_assoc_file = path
        elif sock is self.data_sock:
            self._data_assoc_file = path

    def _resolve_l_file(self, *, file_name_or_path, retry_s):
        if file_name_or_path:
            return self._resolve_path(file_name_or_path)
        return self.query_filename(retry_s=retry_s)

    def open_experiment_data(self, file_name: str | None = None) -> str:
        """Compat: associate an experiment file on the data socket only."""
        if file_name is None:
            path = self.query_filename_data()
        else:
            if isinstance(file_name, (list, tuple)):
                file_name = file_name[0]
            path = self._resolve_path(file_name)
        r = self.data_sock.request(
            f'-f"{path}"',
            retry_s=self.cfg.retry_s,
            timeout_s=self.cfg.command_timeout_s,
        ).strip()
        if r == "0":
            msg = f"Failed to associate data socket: {path}"
            raise MASsoftProtocolError(msg)
        self._data_assoc_file = path
        self.current_file = path
        return path

    def open_experiment_status(self, file_name: str | None = None) -> str:
        """Compat: associate an experiment file on the status socket only."""
        if file_name is None:
            path = self.query_filename(update_current=False)
        else:
            if isinstance(file_name, (list, tuple)):
                file_name = file_name[0]
            path = self._resolve_path(file_name)
        r = self.status_sock.request(
            f'-f"{path}"',
            retry_s=self.cfg.retry_s,
            timeout_s=self.cfg.command_timeout_s,
        ).strip()
        if r == "0":
            msg = f"Failed to associate status socket: {path}"
            raise MASsoftProtocolError(msg)
        self._status_assoc_file = path
        self.current_file = path
        return path

    def query_filename_data(self) -> str:
        """Compat: return the filename currently associated with the data socket."""
        resp = self.data_sock.request(
            "-xFilename",
            retry_s=self.cfg.retry_s,
            timeout_s=self.cfg.command_timeout_s,
        ).strip()
        if resp in ("0", ""):
            msg = "Failed to retrieve filename from data socket"
            raise MASsoftProtocolError(msg)
        return resp.strip('"')

    def request_raw(self, command, *, retry_s=None, timeout_s=None):
        # Raw diagnostic PVs must not bypass safe lifecycle or create hot-links.
        if command.strip() not in ("-xStatus", "-xFilename"):
            raise ValueError("Raw commands limited to -xStatus/-xFilename; use dedicated controls")
        return self._request(command, retry_s=retry_s, timeout_s=timeout_s)

    def query_socket_filename(
        self,
        socket_name: str = "command",
        *,
        retry_s: int | None = None,
    ) -> str:
        """Query ``-xFilename`` on a specific socket: command/status/data."""
        if retry_s is None:
            retry_s = self.cfg.retry_s
        sock = self._socket_by_name(socket_name)
        path = sock.request(
            "-xFilename", retry_s=retry_s, timeout_s=self.cfg.command_timeout_s
        ).strip()
        if path == "0" or not path:
            msg = f"{sock.name}: -xFilename returned 0/empty"
            raise MASsoftProtocolError(msg)
        return path.strip().strip('"')

    def associate_file(
        self,
        file_name_or_path: str,
        *,
        sockets: Sequence[str] = ("command", "status", "data"),
        retry_s: int | None = None,
    ) -> str:
        """Associate one or more sockets to the file via ``-f"<path>"``."""
        if retry_s is None:
            retry_s = self.cfg.retry_s
        path = self._resolve_path(file_name_or_path)

        for name in sockets:
            sock = self._socket_by_name(name)
            r = sock.request(
                f'-f"{path}"', retry_s=retry_s, timeout_s=self.cfg.command_timeout_s
            ).strip()
            if r == "0":
                msg = f"MASsoft failed to associate {sock.name} with: {path}"
                raise MASsoftProtocolError(msg)
            self._mark_associated_file(sock, path)

        self.current_file = path
        return path

    def x_call(self, name, *args, retry_s=None, timeout_s=None):
        name = name.removeprefix("-x")
        if name not in ("Status", "Filename") or args:
            raise ValueError("XSend supports Status/Filename; use dedicated scan controls")
        return self.request_raw("-x" + name, retry_s=retry_s, timeout_s=timeout_s)

    def wait_for_status_prefix(self, prefixes, *, timeout_s=30.0, poll_s=0.2):
        wants = tuple(p.lower() for p in prefixes if p)
        if not wants:
            raise ValueError("prefixes must be nonempty")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self._request(
                "-xStatus",
                retry_s=0,
                timeout_s=min(self.cfg.command_timeout_s, deadline - time.monotonic()),
            )
            self._set_latest_status(status)
            if status.lower().startswith(wants):
                return status
            time.sleep(min(poll_s, max(0, deadline - time.monotonic())))
        raise MASsoftTimeout("Timed out waiting for fresh status")

    def run_experiment(self, mode: str = "-Odt") -> None:
        """Compat: start the experiment.  Delegates to ``x_go()``."""
        od = "d" in mode if mode else False
        ot = "t" in mode if mode else False
        self.x_go(od=od, ot=ot)

    def abort_experiment(self) -> None:
        """Compat: abort the experiment.  Delegates to ``x_abort()``."""
        self.x_abort()

    def close_experiment(self) -> None:
        """Compat: safely stop and close the experiment."""
        self.safe_abort_and_close()

    def get_legends(self, view: int = 1) -> tuple[list[str], str]:
        """Compat: return ``(legend_list, path)`` tuple."""
        path = self.query_filename(update_current=True)
        legends = self.fetch_legends(view=view)
        return legends, path

    def l_call_once(
        self,
        item: str,
        *,
        view: int | None = None,
        options: Sequence[str] | None = None,
        file_name_or_path: str | None = None,
        retry_s: int | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Generic one-shot ``-l*`` call on a temporary socket."""
        if retry_s is None:
            retry_s = self.cfg.retry_s
        if timeout_s is None:
            timeout_s = self.cfg.command_timeout_s

        path = self._resolve_l_file(file_name_or_path=file_name_or_path, retry_s=retry_s)
        if not path:
            msg = "No experiment is associated. Call open_experiment(...) first."
            raise RuntimeError(msg)

        n = item.strip()
        if not re.fullmatch(r"(?:-l)?[A-Za-z][A-Za-z0-9]*", n):
            raise ValueError("Invalid link item name")
        if options and any(not re.fullmatch(r"-[vdcmt][0-9]+", o) for o in options):
            raise ValueError("Unsupported link options")
        if n.startswith("-l"):
            cmd = n
        elif n.startswith("l"):
            cmd = "-" + n
        else:
            cmd = "-l" + n

        parts = [cmd]
        if view is not None:
            parts.append(f"-v{int(view)}")
        if options:
            parts.extend(o for o in options if o)

        tmp = _CRLFSocket(self.cfg.host, self.cfg.port, name=f"MASsoftTmp{n}", timeout_s=timeout_s)
        try:
            tmp.connect(enable_keepalive=self.cfg.enable_keepalive)
            r = tmp.request(f'-f"{path}"', retry_s=retry_s, timeout_s=timeout_s).strip()
            if r == "0":
                msg = f"Failed to associate temp socket with: {path}"
                raise MASsoftProtocolError(msg)
            line = tmp.request(" ".join(parts), retry_s=retry_s, timeout_s=timeout_s).strip()
            if line == "0":
                msg = f"MASsoft refused {' '.join(parts)!r} (returned 0)"
                raise MASsoftProtocolError(msg)
            return line
        finally:
            tmp.close()

    def fetch_legends_once(self, *, view=1):
        return self.fetch_legends(view=view)

    def fetch_status_once(self, *, view: int = 1) -> str:
        """Convenience: one-shot ``-lStatus``."""
        return self.l_call_once("Status", view=view)

    def fetch_data_once(self, *, view=1, cycles=1, include_time=False, include_ms=False):
        count = len(extract_masses(self.fetch_legends(view=view)))
        options = [f"-c{int(cycles)}", f"-t{int(include_time)}", f"-m{int(include_ms)}"]
        line = self.l_call_once("Data", view=view, options=options)
        return self.parse_numeric_row(
            line, expected_count=count, include_time=include_time, include_ms=include_ms
        )

    def start_hotlink(
        self,
        item: str,
        *,
        name: str | None = None,
        view: int | None = None,
        options: Sequence[str] | None = None,
        retry_s: int | None = None,
        chunk_timeout_s: float | None = None,
        burst_gap_s: float | None = None,
        on_burst: Callable[[list[str]], None] | None = None,
    ) -> str:
        """Start an additional dedicated hot-link for any ``-l<Item>``."""
        if retry_s is None:
            retry_s = self.cfg.retry_s
        if chunk_timeout_s is None:
            chunk_timeout_s = self.cfg.link_chunk_timeout_s
        if burst_gap_s is None:
            burst_gap_s = self.cfg.link_burst_gap_s

        n = item.strip()
        if not re.fullmatch(r"(?:-l)?[A-Za-z][A-Za-z0-9]*", n):
            raise ValueError("Invalid link item name")
        if options and any(not re.fullmatch(r"-[vdcmt][0-9]+", o) for o in options):
            raise ValueError("Unsupported link options")
        link_name = name or n

        with self._extra_links_lock:
            if link_name in self._extra_links:
                self._stop_hotlink_locked(link_name)

        path = self._resolve_l_file(file_name_or_path=None, retry_s=retry_s)
        if not path:
            msg = "No experiment is associated. Call open_experiment(...) first."
            raise RuntimeError(msg)

        sock = _CRLFSocket(
            self.cfg.host,
            self.cfg.port,
            name=f"MASsoftHotlink:{link_name}",
            timeout_s=chunk_timeout_s,
        )
        try:
            sock.connect(enable_keepalive=self.cfg.enable_keepalive)
            r = sock.request(
                f'-f"{path}"', retry_s=retry_s, timeout_s=self.cfg.command_timeout_s
            ).strip()
            if r == "0":
                sock.close()
                msg = f"Failed to associate hot-link socket with: {path}"
                raise MASsoftProtocolError(msg)

            if n.startswith("-l"):
                cmd = n
            elif n.startswith("l"):
                cmd = "-" + n
            else:
                cmd = "-l" + n
            parts = [cmd]
            if view is not None:
                parts.append(f"-v{int(view)}")
            if options:
                parts.extend(o for o in options if o)
            sock.send(" ".join(parts), retry_s=retry_s)

        except Exception:
            sock.close()
            raise

        def _default_on(lines: list[str]) -> None:
            with self._extra_latest_lock:
                self._extra_latest[link_name] = list(lines)
                self._extra_latest_ts[link_name] = time.monotonic()
            if on_burst is not None:
                on_burst(lines)

        hl = MASsoftHotlink(
            sock,
            chunk_timeout_s=chunk_timeout_s,
            burst_gap_s=burst_gap_s,
            on_burst=_default_on,
            name=f"MASsoftHotlinkReader:{link_name}",
        )
        hl.start()

        with self._extra_links_lock:
            self._extra_links[link_name] = (sock, hl)

        return link_name

    def stop_hotlink(self, name: str) -> None:
        """Stop a single extra hot-link by name."""
        with self._extra_links_lock:
            self._stop_hotlink_locked(name)

    def stop_all_hotlinks(self) -> None:
        """Stop all extra hot-links."""
        with self._extra_links_lock:
            for n in list(self._extra_links.keys()):
                self._stop_hotlink_locked(n)

    def list_hotlinks(self) -> list[str]:
        """Return sorted list of active extra hot-link names."""
        with self._extra_links_lock:
            return sorted(self._extra_links.keys())

    def get_hotlink_latest(self, name: str) -> list[str] | None:
        """Return the latest burst lines for a named extra hot-link."""
        with self._extra_latest_lock:
            lines = self._extra_latest.get(name)
            return list(lines) if lines is not None else None

    def get_hotlink_latest_timestamp(self, name: str) -> float:
        """Return the monotonic timestamp of the latest burst for a named hot-link."""
        with self._extra_latest_lock:
            return float(self._extra_latest_ts.get(name, 0.0))

    def _stop_hotlink_locked(self, name: str) -> None:
        """Stop and close one extra hot-link.  Caller must hold ``_extra_links_lock``."""
        entry = self._extra_links.pop(name, None)
        if entry is None:
            return
        sock, hl = entry
        try:
            hl.stop()
        finally:
            sock.close()
