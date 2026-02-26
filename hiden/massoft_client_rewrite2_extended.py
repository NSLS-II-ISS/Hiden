"""
Extended MASsoft sockets client built on top of the stable rewrite.

This module keeps the proven behavior in `massoft_client_rewrite2_fixed.py` and
adds generic/manual-oriented APIs so you can reach additional `-x*` / `-l*`
commands without changing the base client again.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
import threading
import time
from typing import Callable, Optional, Sequence

from massoft_client_rewrite2_fixed import (
    MASsoftClient,
    MASsoftConfig,
    MASsoftHotlink,
    MASsoftProtocolError,
    MASsoftTimeout,
    _CRLFSocket,
)


class MASsoftClientExtended(MASsoftClient):
    """
    Extended client with:
      - raw command helpers
      - generic -x* wrapper
      - one-shot generic -l* wrapper
      - dynamic extra hot-links for arbitrary link items
    """

    def __init__(self, cfg: Optional[MASsoftConfig] = None, *, config_path: Optional[str] = None):
        super().__init__(cfg, config_path=config_path)
        self._extra_links_lock = threading.Lock()
        self._extra_links: dict[str, tuple[_CRLFSocket, MASsoftHotlink]] = {}
        self._extra_latest_lock = threading.Lock()
        self._extra_latest: dict[str, list[str]] = {}
        self._extra_latest_ts: dict[str, float] = {}

    # -------------------------
    # Generic command helpers
    # -------------------------

    def request_raw(
        self,
        command: str,
        *,
        retry_s: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> str:
        """Send a raw command on the command socket and return one response line."""
        if retry_s is None:
            retry_s = self.cfg.retry_s
        if timeout_s is None:
            timeout_s = self.cfg.command_timeout_s
        return self.command.request(command, retry_s=retry_s, timeout_s=timeout_s).strip()

    def query_socket_filename(
        self,
        socket_name: str = "command",
        *,
        retry_s: Optional[int] = None,
    ) -> str:
        """
        Query `-xFilename` on a specific socket: command/status/data.
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s
        sock = self._socket_by_name(socket_name)
        path = sock.request("-xFilename", retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
        if path == "0" or not path:
            raise MASsoftProtocolError(f"{sock.name}: -xFilename returned 0/empty")
        return path.strip().strip('"')

    def associate_file(
        self,
        file_name_or_path: str,
        *,
        sockets: Sequence[str] = ("command", "status", "data"),
        retry_s: Optional[int] = None,
    ) -> str:
        """
        Associate one or more sockets to the file via `-f"<path>"`.
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s
        path = self._resolve_path(file_name_or_path)

        for name in sockets:
            sock = self._socket_by_name(name)
            r = sock.request(f'-f"{path}"', retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
            if r == "0":
                raise MASsoftProtocolError(f"MASsoft failed to associate {sock.name} with: {path}")
            self._mark_associated_file(sock, path)

        self.current_file = path
        return path

    def x_call(
        self,
        name: str,
        *args: str,
        retry_s: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> str:
        """
        Generic `-x*` helper.
        Example: `x_call("Status")`, `x_call("Go", "-Odt")`.
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s
        if timeout_s is None:
            timeout_s = self.cfg.command_timeout_s

        n = name.strip()
        if n.startswith("-x"):
            cmd = n
        elif n.startswith("x"):
            cmd = "-" + n
        else:
            cmd = "-x" + n

        if args:
            cmd = cmd + " " + " ".join(a for a in args if a)

        r = self.command.request(cmd, retry_s=retry_s, timeout_s=timeout_s).strip()
        if r == "0":
            raise MASsoftProtocolError(f"MASsoft refused {cmd!r} (returned 0)")

        # Refresh cached filename after xGo-like commands.
        if cmd.lower().startswith("-xgo"):
            try:
                self.query_filename(retry_s=retry_s, update_current=True)
            except Exception:
                pass

        if cmd.lower().startswith("-xclose"):
            self.stop_links()
            self.current_file = None

        return r

    def l_call_once(
        self,
        item: str,
        *,
        view: Optional[int] = None,
        options: Optional[Sequence[str]] = None,
        file_name_or_path: Optional[str] = None,
        retry_s: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> str:
        """
        Generic one-shot `-l*` call on a temporary socket.
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s
        if timeout_s is None:
            timeout_s = self.cfg.command_timeout_s

        path = self._resolve_l_file(file_name_or_path=file_name_or_path, retry_s=retry_s)
        if not path:
            raise RuntimeError("No experiment is associated. Call open_experiment(...) first.")

        n = item.strip()
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
                raise MASsoftProtocolError(f"Failed to associate temp socket with: {path}")
            line = tmp.request(" ".join(parts), retry_s=retry_s, timeout_s=timeout_s).strip()
            if line == "0":
                raise MASsoftProtocolError(f"MASsoft refused {' '.join(parts)!r} (returned 0)")
            return line
        finally:
            tmp.close()

    # -------------------------
    # Convenience parsers
    # -------------------------

    @staticmethod
    def parse_legends(line: str) -> list[str]:
        """Parse one legends line to string labels."""
        if not line:
            return []
        if "\t" in line:
            return [p.strip().strip('"') for p in line.split("\t") if p.strip()]
        return [p.strip().strip('"') for p in line.split() if p.strip()]

    @staticmethod
    def parse_numeric_row(
        line: str,
        *,
        include_time: bool = False,
        include_ms: bool = False,
    ) -> list[float]:
        """
        Parse a mixed-format `-lData` row into numeric values.
        """
        if not line:
            return []

        parts = line.split()
        if not parts:
            return []

        start = 0
        if parts and ":" in parts[0]:
            start = 1
        if include_time:
            start += 1
        if include_ms:
            start += 1
        if start >= len(parts):
            return []

        vals: list[float] = []
        for tok in parts[start:]:
            try:
                vals.append(float(tok))
            except Exception:
                continue

        # Common MASsoft shape: elapsed-time, time(ms), scans...
        if not include_ms and len(vals) >= 2:
            first_raw = parts[start]
            second_raw = parts[start + 1] if (start + 1) < len(parts) else ""
            if first_raw.isdigit() and (("e" in second_raw.lower()) or ("." in second_raw)):
                vals = vals[1:]

        return vals

    def fetch_legends_once(self, *, view: int = 1) -> list[str]:
        line = self.l_call_once("Legends", view=view)
        return self.parse_legends(line)

    def fetch_status_once(self, *, view: int = 1) -> str:
        return self.l_call_once("Status", view=view)

    def fetch_data_once(
        self,
        *,
        view: int = 1,
        cycles: int = 1,
        include_time: bool = False,
        include_ms: bool = False,
    ) -> list[float]:
        options = []
        if cycles is not None:
            options.append(f"-c{int(cycles)}")
        options.append(f"-t{1 if include_time else 0}")
        options.append(f"-m{1 if include_ms else 0}")
        line = self.l_call_once("Data", view=view, options=options)
        return self.parse_numeric_row(line, include_time=include_time, include_ms=include_ms)

    # -------------------------
    # Dynamic extra hot-links
    # -------------------------

    def start_hotlink(
        self,
        item: str,
        *,
        name: Optional[str] = None,
        view: Optional[int] = None,
        options: Optional[Sequence[str]] = None,
        retry_s: Optional[int] = None,
        chunk_timeout_s: Optional[float] = None,
        burst_gap_s: Optional[float] = None,
        on_burst: Optional[Callable[[list[str]], None]] = None,
    ) -> str:
        """
        Start an additional dedicated hot-link for any `-l<Item>`.
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s
        if chunk_timeout_s is None:
            chunk_timeout_s = self.cfg.link_chunk_timeout_s
        if burst_gap_s is None:
            burst_gap_s = self.cfg.link_burst_gap_s

        n = item.strip()
        link_name = name or n

        with self._extra_links_lock:
            if link_name in self._extra_links:
                self._stop_hotlink_locked(link_name)

        path = self._resolve_l_file(file_name_or_path=None, retry_s=retry_s)
        if not path:
            raise RuntimeError("No experiment is associated. Call open_experiment(...) first.")

        sock = _CRLFSocket(
            self.cfg.host,
            self.cfg.port,
            name=f"MASsoftHotlink:{link_name}",
            timeout_s=chunk_timeout_s,
        )
        sock.connect(enable_keepalive=self.cfg.enable_keepalive)
        r = sock.request(f'-f"{path}"', retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
        if r == "0":
            sock.close()
            raise MASsoftProtocolError(f"Failed to associate hot-link socket with: {path}")

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
        with self._extra_links_lock:
            self._stop_hotlink_locked(name)

    def stop_all_hotlinks(self) -> None:
        with self._extra_links_lock:
            for name in list(self._extra_links.keys()):
                self._stop_hotlink_locked(name)

    def list_hotlinks(self) -> list[str]:
        with self._extra_links_lock:
            return sorted(self._extra_links.keys())

    def get_hotlink_latest(self, name: str) -> Optional[list[str]]:
        with self._extra_latest_lock:
            lines = self._extra_latest.get(name)
            return list(lines) if lines is not None else None

    def get_hotlink_latest_timestamp(self, name: str) -> float:
        with self._extra_latest_lock:
            return float(self._extra_latest_ts.get(name, 0.0))

    # -------------------------
    # Helpers / overrides
    # -------------------------

    def stop_links(self) -> None:
        super().stop_links()
        self.stop_all_hotlinks()

    def wait_for_status_prefix(
        self,
        prefixes: Sequence[str],
        *,
        timeout_s: float = 30.0,
        poll_s: float = 0.2,
    ) -> str:
        """
        Wait until latest/polled status starts with any prefix in `prefixes`.
        """
        wants = tuple(p.lower() for p in prefixes if p)
        if not wants:
            raise ValueError("prefixes must be non-empty")

        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            st = self.get_latest_status() or ""
            if st and st.lower().startswith(wants):
                return st
            try:
                st2 = self.x_status()
                if st2 and st2.lower().startswith(wants):
                    self._set_latest_status(st2)
                    return st2
            except Exception:
                pass
            time.sleep(float(poll_s))
        raise MASsoftTimeout(f"Timed out waiting for status prefix {wants!r}")

    def _socket_by_name(self, name: str) -> _CRLFSocket:
        n = name.strip().lower()
        if n in ("command", "cmd"):
            return self.command
        if n in ("status", "stat"):
            return self.status_sock
        if n in ("data",):
            return self.data_sock
        raise ValueError(f"Unknown socket name: {name!r}")

    def _mark_associated_file(self, sock: _CRLFSocket, path: str) -> None:
        if sock is self.command:
            self._command_assoc_file = path
        elif sock is self.status_sock:
            self._status_assoc_file = path
        elif sock is self.data_sock:
            self._data_assoc_file = path

    def _resolve_l_file(self, *, file_name_or_path: Optional[str], retry_s: int) -> Optional[str]:
        if file_name_or_path:
            return self._resolve_path(file_name_or_path)

        # Prefer MASsoft's currently associated command-socket file.
        try:
            return self.query_filename(retry_s=retry_s, update_current=True)
        except Exception:
            pass

        if self.current_file:
            return self.current_file
        if self._command_assoc_file:
            return self._command_assoc_file
        return None

    def _resolve_path(self, file_name_or_path: str) -> str:
        if file_name_or_path.startswith("%") or (":" in file_name_or_path) or file_name_or_path.startswith("\\\\"):
            return file_name_or_path
        return str(PureWindowsPath(self.cfg.experiment_directory) / file_name_or_path)

    def _stop_hotlink_locked(self, name: str) -> None:
        entry = self._extra_links.pop(name, None)
        if entry is None:
            return
        sock, hl = entry
        try:
            hl.stop()
        finally:
            sock.close()


__all__ = ["MASsoftClientExtended", "MASsoftConfig"]
