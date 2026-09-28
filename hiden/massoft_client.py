"""Core MASsoft client. No failed hardware command is automatically replayed."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import PureWindowsPath

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
    parse_legends,
    parse_numeric_row,
    quote_path,
)


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

        def receive(lines):
            for line in lines:
                # A one-channel zero is a valid measurement, not an error code.
                row = parse_numeric_row(
                    line,
                    expected_count=self._expected_count,
                    include_time=include_time,
                    include_ms=include_ms,
                )
                with self._latest_lock:
                    self._latest_row = row
                    self._latest_raw_row = line
                    self._latest_row_ts = self._latest_raw_row_ts = time.monotonic()
                    self._latest_row_wall_ts = time.time()

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
            }

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
