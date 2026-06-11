"""
Reliable MASsoft sockets client (rewrite).

This rewrite follows the key rules in the MASsoft Sockets manual:

- Command and response strings are CRLF terminated. 
- Read a response for every command before sending the next command. 
- Use dedicated sockets for hot links; once a link is established, do not send further
  commands on that socket. 
- Do not close a socket while a command is in progress; the worst-case duration is
  bounded by the "-d<t>" retry window you request. 

Compared with the original implementation, this version removes the main instability
sources:
- No fixed "-d20" injected into every command.
- Socket timeouts are consistent with the chosen -d retry window.
- CRLF framing is implemented correctly (no single recv(4096) assumptions).
- Link commands are never issued on the command socket.
- Data acquisition is performed using a true hot-link reader thread, not by repeatedly
  sending -lData polls.

Intended usage:
- Create one MASsoftClient per IOC process.
- Use `open_experiment()` once.
- Use `start_status_link()` / `start_data_link()` to stream updates.
- Use `abort()` / `close()` via the command socket.

This file is the stable fixed MASsoft client.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import socket
import threading
import time
from typing import Any, Callable, Iterable, Optional, Sequence


LOG = logging.getLogger(__name__)
CRLF = b"\r\n"
DEFAULT_CONFIG_FILE_NAME = "hiden_config.json"


def get_runtime_config_path(config_path: Optional[str] = None) -> Path:
    """
    Resolve runtime config path.

    Priority:
      1) explicit `config_path`
      2) `HIDEN_CONFIG` environment variable
      3) `<this module directory>/hiden_config.json`
    """
    if config_path:
        return Path(config_path).expanduser()

    env_path = os.getenv("HIDEN_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    return Path(__file__).with_name(DEFAULT_CONFIG_FILE_NAME)


def load_runtime_config(config_path: Optional[str] = None) -> dict[str, Any]:
    """
    Load runtime JSON config. Returns empty dict when file is missing.
    """
    path = get_runtime_config_path(config_path=config_path)
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in runtime config: {path}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"Runtime config must be a JSON object: {path}")

    return raw


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class MASsoftError(Exception):
    """Base exception for MASsoft socket client errors."""


class MASsoftDisconnected(MASsoftError, ConnectionError):
    """Raised when the remote side closes the socket or the socket is unusable."""


class MASsoftTimeout(MASsoftError, TimeoutError):
    """Raised when a read/write exceeds the requested timeout."""


class MASsoftProtocolError(MASsoftError):
    """Raised when MASsoft returns '0' (failure) or protocol framing is violated."""


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class MASsoftConfig:
    beamline_name: str = "08IDB"
    host: str = "10.66.58.227"
    port: int = 5026

    # Directory used when `open_experiment("file56.exp")` is called.
    # experiment_directory: str = r"C:\Users\08id-user\Documents\Hiden Analytical\MASsoft\11"
    experiment_directory: str = r"C:\Users\xf08id1\Documents\Hiden Analytical\MASsoft\11"

    # Default MASsoft "-d<t>" retry window for commands that support it.
    retry_s: int = 15

    # IMPORTANT: must be >= retry_s (+ margin), otherwise you can time out locally while MASsoft
    # continues retrying and later sends a "late" response that desynchronizes the stream.
    command_timeout_s: float = 20.0

    # Per-recv timeout used by link reader threads.
    link_chunk_timeout_s: float = 1.0

    # Group multiple CRLF lines that arrive in a short burst into one callback.
    # This is useful for multi-line items like -lData in Bar/Profile views.
    link_burst_gap_s: float = 0.10

    enable_keepalive: bool = True

    @classmethod
    def from_runtime_config(cls, config_path: Optional[str] = None) -> "MASsoftConfig":
        """
        Build config from runtime JSON.
        """
        raw = load_runtime_config(config_path=config_path)
        massoft = raw.get("massoft", {})
        if not isinstance(massoft, dict):
            raise ValueError("runtime config key 'massoft' must be an object")

        def _get(key: str, default: Any) -> Any:
            if key in massoft:
                return massoft[key]
            return raw.get(key, default)

        return cls(
            beamline_name=str(raw.get("beamline_name", cls.beamline_name)),
            host=str(_get("host", cls.host)),
            port=int(_get("port", cls.port)),
            experiment_directory=str(_get("experiment_directory", cls.experiment_directory)),
            retry_s=int(_get("retry_s", cls.retry_s)),
            command_timeout_s=float(_get("command_timeout_s", cls.command_timeout_s)),
            link_chunk_timeout_s=float(_get("link_chunk_timeout_s", cls.link_chunk_timeout_s)),
            link_burst_gap_s=float(_get("link_burst_gap_s", cls.link_burst_gap_s)),
            enable_keepalive=bool(_get("enable_keepalive", cls.enable_keepalive)),
        )


# -----------------------------------------------------------------------------
# Low-level CRLF framed socket
# -----------------------------------------------------------------------------

class _CRLFSocket:
    """
    A single TCP connection to MASsoft with CRLF framed reads.

    - `request(...)` is serialized by a lock so command/response pairs cannot interleave.
    - For hot-links, call `send(...)` once, then only call `read_line(...)` from one thread.

    MASsoft sends a short greeting upon connect (2–3 digits) that can be discarded. 
    """

    def __init__(self, host: str, port: int, *, name: str, timeout_s: float):
        self.host = host
        self.port = port
        self.name = name
        self._timeout_s = float(timeout_s)

        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._req_lock = threading.Lock()

    def connect(self, *, enable_keepalive: bool = True) -> None:
        self.close()

        sock = socket.create_connection((self.host, self.port), timeout=self._timeout_s)
        sock.settimeout(self._timeout_s)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        if enable_keepalive:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass

        self._sock = sock
        self._buf.clear()

        # Best-effort: consume the greeting if it arrives quickly.
        try:
            _ = self.read_line(timeout_s=1.0)
        except MASsoftTimeout:
            pass
        except MASsoftDisconnected:
            raise

        LOG.info("%s connected to %s:%d", self.name, self.host, self.port)

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        self._buf.clear()
        if sock is None:
            return

        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        LOG.info("%s closed", self.name)

    def is_connected(self) -> bool:
        return self._sock is not None

    def send(self, command: str, *, retry_s: Optional[int] = None) -> None:
        if self._sock is None:
            raise MASsoftDisconnected(f"{self.name}: not connected")

        cmd = command.strip()

        # Append -d<t> only if requested and not already present.
        if retry_s is not None and " -d" not in cmd:
            cmd = f"{cmd} -d{int(retry_s)}"

        wire = (cmd + "\r\n").encode("utf-8")
        try:
            self._sock.sendall(wire)
        except OSError as exc:
            raise MASsoftDisconnected(f"{self.name}: send failed: {exc!r}") from exc

    def read_line(self, *, timeout_s: Optional[float] = None) -> str:
        if self._sock is None:
            raise MASsoftDisconnected(f"{self.name}: not connected")

        sock = self._sock
        old_timeout = sock.gettimeout()
        if timeout_s is not None:
            sock.settimeout(float(timeout_s))

        try:
            while True:
                idx = self._buf.find(CRLF)
                if idx != -1:
                    raw = bytes(self._buf[:idx])
                    del self._buf[: idx + 2]
                    return raw.decode("utf-8", errors="replace")

                try:
                    chunk = sock.recv(4096)
                except socket.timeout as exc:
                    raise MASsoftTimeout(f"{self.name}: read timed out") from exc
                except OSError as exc:
                    raise MASsoftDisconnected(f"{self.name}: read failed: {exc!r}") from exc

                if chunk == b"":
                    raise MASsoftDisconnected(f"{self.name}: remote closed connection")

                self._buf.extend(chunk)
        finally:
            if timeout_s is not None:
                sock.settimeout(old_timeout)

    def request(self, command: str, *, retry_s: Optional[int] = None, timeout_s: Optional[float] = None) -> str:
        """
        Send one command and read one CRLF-terminated response line.

        MASsoft rule: receive a response for each command before sending another. 
        """
        with self._req_lock:
            self.send(command, retry_s=retry_s)
            return self.read_line(timeout_s=timeout_s)


# -----------------------------------------------------------------------------
# Hot-link subscription helper
# -----------------------------------------------------------------------------

class MASsoftHotlink:
    """
    A dedicated listening socket reading a MASsoft -l<Item> hot link.

    The callback is invoked with a *burst* of one or more lines. Bursts are separated by
    idle gaps > config.link_burst_gap_s.

    Once a hot link has been established, no further commands should be sent on that socket. 
    """

    def __init__(
        self,
        sock: _CRLFSocket,
        *,
        chunk_timeout_s: float,
        burst_gap_s: float,
        on_burst: Callable[[list[str]], None],
        name: str,
    ):
        self._sock = sock
        self._chunk_timeout_s = float(chunk_timeout_s)
        self._burst_gap_s = float(burst_gap_s)
        self._on_burst = on_burst
        self._name = name

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread = t
        t.start()

    def stop(self, *, join_timeout_s: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=float(join_timeout_s))
        self._thread = None

    def _run(self) -> None:
        pending: list[str] = []
        last_rx: float | None = None

        def _emit_pending() -> None:
            nonlocal pending, last_rx
            if not pending:
                return
            try:
                self._on_burst(pending)
            except Exception:
                LOG.exception("%s callback error", self._name)
            pending = []
            last_rx = None

        while not self._stop.is_set():
            try:
                line = self._sock.read_line(timeout_s=self._chunk_timeout_s)
                s = line.strip()
                if s:
                    now = time.monotonic()
                    # Flush the previous burst when an inter-line idle gap is observed.
                    # This is required for steady streams where read_line() may never time out.
                    if pending and last_rx is not None and (now - last_rx) >= self._burst_gap_s:
                        _emit_pending()
                    pending.append(s)
                    last_rx = now
            except MASsoftTimeout:
                # Timeout can also delimit a burst.
                if pending and last_rx is not None:
                    if (time.monotonic() - last_rx) >= self._burst_gap_s:
                        _emit_pending()
            except MASsoftDisconnected as exc:
                LOG.warning("%s disconnected: %s", self._name, exc)
                break
            except Exception:
                LOG.exception("%s reader error", self._name)
                break

            if pending and last_rx is not None and self._burst_gap_s <= 0:
                _emit_pending()

        # Flush any remaining lines
        if pending:
            _emit_pending()


# -----------------------------------------------------------------------------
# High-level MASsoft client
# -----------------------------------------------------------------------------

class MASsoftClient:
    """
    High-level MASsoft client using:
      - One command socket for -f / -x* commands
      - Separate hot-link sockets for -lStatus and -lData (and other link items if needed)

    MASsoft guideline: use at least two sockets; one for commands and one for a status hot-link. 
    """

    def __init__(self, cfg: Optional[MASsoftConfig] = None, *, config_path: Optional[str] = None):
        self.cfg = cfg if cfg is not None else MASsoftConfig.from_runtime_config(config_path=config_path)

        self.command = _CRLFSocket(self.cfg.host, self.cfg.port, name="MASsoftCommand", timeout_s=self.cfg.command_timeout_s)
        self.status_sock = _CRLFSocket(self.cfg.host, self.cfg.port, name="MASsoftStatus", timeout_s=self.cfg.link_chunk_timeout_s)
        self.data_sock = _CRLFSocket(self.cfg.host, self.cfg.port, name="MASsoftData", timeout_s=self.cfg.link_chunk_timeout_s)

        self.current_file: Optional[str] = None
        self._command_assoc_file: Optional[str] = None
        self._status_assoc_file: Optional[str] = None
        self._data_assoc_file: Optional[str] = None
        self._assoc_fallback_logged: set[str] = set()

        self._status_link: Optional[MASsoftHotlink] = None
        self._data_link: Optional[MASsoftHotlink] = None

        self._latest_status_lock = threading.Lock()
        self._latest_status: Optional[str] = None
        self._latest_status_ts = 0.0

        self._latest_row_lock = threading.Lock()
        self._latest_row: Optional[list[float]] = None
        self._latest_row_ts = 0.0
        self._latest_raw_row: Optional[str] = None
        self._latest_raw_row_ts = 0.0

        self._last_error_lock = threading.Lock()
        self._last_error: Optional[str] = None

    # -------------------------
    # Connection lifecycle
    # -------------------------

    def connect(self) -> None:
        """Connect all sockets (command + link sockets)."""
        self.command.connect(enable_keepalive=self.cfg.enable_keepalive)
        self.status_sock.connect(enable_keepalive=self.cfg.enable_keepalive)
        self.data_sock.connect(enable_keepalive=self.cfg.enable_keepalive)

    def disconnect(self) -> None:
        """Stop hot-links and close sockets."""
        self.stop_links()
        self.command.close()
        self.status_sock.close()
        self.data_sock.close()
        self.current_file = None
        self._command_assoc_file = None
        self._status_assoc_file = None
        self._data_assoc_file = None
        self._assoc_fallback_logged.clear()

    # -------------------------
    # File open / association
    # -------------------------

    def _resolve_path(self, file_name_or_path: str) -> str:
        # If user passes a full path or an env-var path (%HIDEN_LastFile%), keep as-is.
        if file_name_or_path.startswith("%") or (":" in file_name_or_path) or file_name_or_path.startswith("\\\\"):
            return file_name_or_path
        return str(PureWindowsPath(self.cfg.experiment_directory) / file_name_or_path)

    def open_experiment(self, file_name_or_path: str, *, retry_s: Optional[int] = None) -> str:
        """
        Open/associate an experiment file on all sockets.

        Per the manual, each socket that will issue commands or links must be associated with
        the experiment file (via -f). 
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s

        path = self._resolve_path(file_name_or_path)

        r = self.command.request(f'-f"{path}"', retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
        if r == "0":
            raise MASsoftProtocolError(f"MASsoft failed to open/associate: {path}")
        self._command_assoc_file = path

        # Associate the link sockets too (do this BEFORE issuing any -l* command).
        for s in (self.status_sock, self.data_sock):
            rr = s.request(f'-f"{path}"', retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
            if rr == "0":
                raise MASsoftProtocolError(f"MASsoft failed to associate {s.name} with: {path}")
            if s is self.status_sock:
                self._status_assoc_file = path
            elif s is self.data_sock:
                self._data_assoc_file = path

        self.current_file = path
        return path

    def query_filename(self, *, retry_s: Optional[int] = None, update_current: bool = True) -> str:
        """
        Return the experiment filename currently associated with the command socket (-xFilename).
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s

        path = self.command.request("-xFilename", retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
        if path == "0" or not path:
            raise MASsoftProtocolError("MASsoft refused -xFilename (returned 0/empty)")

        # MASsoft may return the path quoted.
        path = path.strip().strip('"')
        if not path:
            raise MASsoftProtocolError("MASsoft returned an empty filename for -xFilename")

        if update_current:
            self.current_file = path

        return path

    def _associate_socket_with_active_file(self, sock: _CRLFSocket, *, retry_s: Optional[int] = None) -> str:
        """
        Associate a non-hotlink socket with the currently active experiment file.

        Use this immediately before issuing a new -l* command on that socket.
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s

        if sock is self.command:
            fallback_assoc = self._command_assoc_file
        elif sock is self.status_sock:
            fallback_assoc = self._status_assoc_file
        elif sock is self.data_sock:
            fallback_assoc = self._data_assoc_file
        else:
            fallback_assoc = None

        # Prefer the live filename reported by MASsoft; fall back to cached value.
        active_file = self.current_file
        try:
            active_file = self.query_filename(retry_s=retry_s, update_current=True)
        except Exception:
            if not active_file:
                raise

        rr = sock.request(f'-f"{active_file}"', retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
        if rr != "0":
            if sock is self.command:
                self._command_assoc_file = active_file
            elif sock is self.status_sock:
                self._status_assoc_file = active_file
            elif sock is self.data_sock:
                self._data_assoc_file = active_file
            self._assoc_fallback_logged.discard(sock.name)
            return active_file

        # Some MASsoft builds reject -f on the generated run file path after -xGo -O*.
        # In that case keep the prior socket association (from open_experiment) and continue.
        if fallback_assoc:
            msg = (
                f"{sock.name} association to active file failed; "
                f"keeping existing association: {fallback_assoc}"
            )
            if sock.name not in self._assoc_fallback_logged:
                LOG.info(msg)
                self._assoc_fallback_logged.add(sock.name)
            return fallback_assoc

        raise MASsoftProtocolError(f"MASsoft failed to associate {sock.name} with: {active_file}")

    # -------------------------
    # Execute (-x*) commands
    # -------------------------

    def x_status(self, *, retry_s: Optional[int] = None) -> str:
        """Return current MSIU status via -xStatus. """
        if retry_s is None:
            retry_s = self.cfg.retry_s
        return self.command.request("-xStatus", retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()

    def x_go(self, *, filename: Optional[str] = None, od: bool = True, ot: bool = True, retry_s: Optional[int] = None) -> None:
        """
        Start the experiment via -xGo. 

        If filename is None, MASsoft uses its default naming rules.
        The -O flags generate filename based on date/time (od => directory; ot => time). 
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s

        o_flags = ""
        if od or ot:
            o_flags = "-O" + ("d" if od else "") + ("t" if ot else "")

        parts = ["-xGo"]
        if filename:
            parts.append(filename)
        if o_flags:
            parts.append(o_flags)

        cmd = " ".join(parts)
        r = self.command.request(cmd, retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
        if r == "0":
            raise MASsoftProtocolError("MASsoft refused -xGo (returned 0)")

        # -xGo with -O* may switch to a newly created file; refresh our cached path.
        try:
            self.query_filename(retry_s=retry_s, update_current=True)
        except Exception:
            # Keep running even if MASsoft cannot report the filename immediately.
            pass

    def x_abort(self, *, retry_s: Optional[int] = None) -> None:
        """Abort acquisition via -xAbort. """
        if retry_s is None:
            retry_s = self.cfg.retry_s
        r = self.command.request("-xAbort", retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
        if r == "0":
            raise MASsoftProtocolError("MASsoft refused -xAbort (returned 0)")

    def x_close(self, *, retry_s: Optional[int] = None) -> None:
        """
        Close the experiment via -xClose. 

        IMPORTANT: MASsoft will terminate ALL connections associated with the file after close. 
        The caller should reconnect sockets after calling this.
        """
        if retry_s is None:
            retry_s = self.cfg.retry_s

        r = self.command.request("-xClose", retry_s=retry_s, timeout_s=self.cfg.command_timeout_s).strip()
        if r == "0":
            raise MASsoftProtocolError("MASsoft refused -xClose (returned 0)")

        # Our sockets will likely be dropped by MASsoft shortly; proactively stop links now.
        self.stop_links()
        self.current_file = None
        self._command_assoc_file = None
        self._status_assoc_file = None
        self._data_assoc_file = None
        self._assoc_fallback_logged.clear()

    def safe_abort_and_wait(self, *, timeout_s: float = 30.0) -> str:
        """
        Abort and wait until status becomes StoppedActive or StoppedShutdown. 

        Prefers the status hot-link if running; otherwise polls -xStatus.
        Returns the final status string observed.
        """
        self.x_abort()

        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            st = self.get_latest_status()
            if st and st.lower().startswith("stopped"):
                return st
            # Fallback: poll if link is not active / not receiving
            try:
                st2 = self.x_status()
                if st2 and st2.lower().startswith("stopped"):
                    self._set_latest_status(st2)
                    return st2
            except Exception:
                pass
            time.sleep(0.2)

        raise MASsoftTimeout(f"Timed out waiting for stopped status after abort ({timeout_s}s)")


    def safe_abort_and_close(
        self,
        *,
        abort_timeout_s: float = 30.0,
        close_retry_s: Optional[int] = None,
        reconnect: bool = False,
    ) -> str:
        """
        Safely stop (if running), then close the associated experiment file.

        Recommended sequencing from the MASsoft Sockets manual:
          1) If the MSIU is actively running (StartingActive / ScanningActive / StoppingActive / Degas),
             issue -xAbort and wait until a Stopped* state is observed.
          2) Issue -xClose.
          3) Disconnect local sockets because MASsoft terminates ALL connections associated with the file after close.

        Returns the final *stopped* status observed (or the last known status if the MSIU was already stopped).
        """
        if close_retry_s is None:
            close_retry_s = self.cfg.retry_s

        # Determine current state (prefer hot-link; fall back to polling).
        st = self.get_latest_status()
        if not st:
            try:
                st = self.x_status()
            except Exception:
                st = None

        def _is_running(s: str) -> bool:
            ss = s.strip().lower()
            return (
                ss.startswith("starting")
                or ss.startswith("scanning")
                or ss.startswith("stopping")
                or ss == "degas"
            )

        final_status = st or ""

        # If status is running (or unknown), attempt a safe abort first.
        if st and _is_running(st):
            final_status = self.safe_abort_and_wait(timeout_s=abort_timeout_s)
        elif st is None:
            # Conservative path: try abort->wait, but tolerate failure if MASsoft already stopped.
            try:
                final_status = self.safe_abort_and_wait(timeout_s=abort_timeout_s)
            except Exception:
                pass

        # Close the experiment. MASsoft will drop file-associated sockets after this.
        try:
            self.x_close(retry_s=close_retry_s)
        finally:
            # Ensure our local sockets are also torn down to avoid half-open states.
            try:
                self.disconnect()
            except Exception:
                pass

            if reconnect:
                self.connect()

        return final_status or (self.get_latest_status() or "")

        # -------------------------
    # Links (-l*) for status/data
    # -------------------------

    def start_status_link(self, *, view: int = 1) -> None:
        """
        Start a -lStatus hot-link on the dedicated status socket. 
        """
        if self.current_file is None:
            raise RuntimeError("No experiment is associated. Call open_experiment(...) first.")

        # Ensure this dedicated socket is bound to the currently active file.
        self._associate_socket_with_active_file(self.status_sock, retry_s=self.cfg.retry_s)

        # Establish the link. After this, no more commands on status_sock. 
        self.status_sock.send(f"-lStatus -v{int(view)}", retry_s=self.cfg.retry_s)

        # Create the subscription and start background reader.
        def _on(lines: list[str]) -> None:
            for line in lines:
                if line == "0":
                    self._set_last_error("Status link returned '0' (view missing or MASsoft busy)")
                    continue
                self._set_latest_status(line)

        self._status_link = MASsoftHotlink(
            self.status_sock,
            chunk_timeout_s=self.cfg.link_chunk_timeout_s,
            burst_gap_s=self.cfg.link_burst_gap_s,
            on_burst=_on,
            name="MASsoftStatusHotlink",
        )
        self._status_link.start()

    def start_data_link(
        self,
        *,
        view: int = 1,
        mid_cycles: int = 1,
        include_time: bool = False,
        include_ms: bool = False,
    ) -> None:
        """
        Start a -lData hot-link on the dedicated data socket. 

        For MID graphical/tabular views, MASsoft supports:
          -c<x> number of cycles returned per update
          -t<x> time formatting ON(1)/OFF(0)
          -m<x> millisecond formatting ON(1)/OFF(0) 

        This method assumes MID-style rows and stores only the latest row (last cycle).
        By default, it sends minimal "-lData -v<view>" for widest compatibility.
        """
        if self.current_file is None:
            raise RuntimeError("No experiment is associated. Call open_experiment(...) first.")

        # Ensure this dedicated socket is bound to the currently active file.
        self._associate_socket_with_active_file(self.data_sock, retry_s=self.cfg.retry_s)

        c = max(1, int(mid_cycles))
        t = 1 if include_time else 0
        m = 1 if include_ms else 0

        cmd = f"-lData -v{int(view)}"
        # Only add MID-specific options when non-default behavior is requested.
        if c != 1 or t != 0 or m != 0:
            cmd += f" -c{c} -t{t} -m{m}"
        self.data_sock.send(cmd, retry_s=self.cfg.retry_s)

        drop = (1 if include_time else 0) + (1 if include_ms else 0)

        def _to_float(tok: str) -> Optional[float]:
            try:
                return float(tok)
            except Exception:
                return None

        def _on(lines: list[str]) -> None:
            # Each line is one cycle row.
            # Keep the last valid numeric row in the burst.
            last_row: Optional[list[float]] = None
            last_raw_line: Optional[str] = None
            for line in lines:
                if not line:
                    continue
                if line == "0":
                    self._set_last_error("Data link returned '0' (view missing or MASsoft busy)")
                    continue
                last_raw_line = line
                parts = line.split()

                # Request flags are not always honored by every MASsoft view; some views still
                # prepend elapsed-time text and/or time(ms). Parse defensively.
                start = 0
                if parts and ":" in parts[0]:
                    # Elapsed-time text token (e.g. "00:00:51").
                    start = 1
                start += drop
                if start >= len(parts):
                    continue

                numeric: list[float] = []
                for p in parts[start:]:
                    v = _to_float(p)
                    if v is None:
                        continue
                    numeric.append(v)

                if not numeric:
                    self._set_last_error(f"Unparseable MID data line: {line!r}")
                    continue

                # If include_ms was not requested, drop leading time(ms) when present.
                if not include_ms and len(numeric) >= 2:
                    first_raw = parts[start]
                    second_raw = parts[start + 1] if (start + 1) < len(parts) else ""
                    first_is_intish = first_raw.isdigit()
                    second_is_scan_like = ("e" in second_raw.lower()) or ("." in second_raw)
                    if first_is_intish and second_is_scan_like:
                        numeric = numeric[1:]

                if not numeric:
                    continue
                last_row = numeric

            if last_row is not None:
                with self._latest_row_lock:
                    self._latest_row = last_row
                    self._latest_row_ts = time.monotonic()
            if last_raw_line is not None:
                with self._latest_row_lock:
                    self._latest_raw_row = last_raw_line
                    self._latest_raw_row_ts = time.monotonic()

        self._data_link = MASsoftHotlink(
            self.data_sock,
            chunk_timeout_s=self.cfg.link_chunk_timeout_s,
            # Emit data rows as soon as they arrive; do not wait for burst-gap delimiters.
            burst_gap_s=0.0,
            on_burst=_on,
            name="MASsoftDataHotlink",
        )
        self._data_link.start()

    def stop_links(self) -> None:
        """Stop status/data hot-links (does not close sockets)."""
        if self._status_link is not None:
            self._status_link.stop()
            self._status_link = None
        if self._data_link is not None:
            self._data_link.stop()
            self._data_link = None

    # -------------------------
    # One-shot metadata
    # -------------------------

    def fetch_legends(self, *, view: int = 1) -> list[str]:
        """
        Fetch -lLegends once on a temporary socket so the command socket stays usable. 
        """
        if self.current_file is None:
            raise RuntimeError("No experiment is associated. Call open_experiment(...) first.")

        # Always interrogate MASsoft for the currently associated file so we do not
        # accidentally re-bind to the originally opened .exp after -xGo -O* creates
        # and switches to a run file.
        active_file = self.query_filename(update_current=True)

        tmp = _CRLFSocket(self.cfg.host, self.cfg.port, name="MASsoftLegendsTmp", timeout_s=self.cfg.command_timeout_s)
        try:
            tmp.connect(enable_keepalive=self.cfg.enable_keepalive)
            r = tmp.request(f'-f"{active_file}"', retry_s=self.cfg.retry_s, timeout_s=self.cfg.command_timeout_s).strip()
            if r == "0":
                raise MASsoftProtocolError("Failed to associate temp legends socket with experiment")

            # Link command; MASsoft will return a CRLF terminated line of legends. 
            line = tmp.request(f"-lLegends -v{int(view)}", retry_s=self.cfg.retry_s, timeout_s=self.cfg.command_timeout_s).strip()
            if line == "0":
                raise MASsoftProtocolError("MASsoft refused -lLegends (returned 0)")

            # Legends are tab-separated; legends themselves may include spaces ("mass 1"). 
            if "\t" in line:
                parts = [p.strip().strip('"') for p in line.split("\t") if p.strip()]
                return parts
            return [line]
        finally:
            tmp.close()

    # -------------------------
    # Readback helpers
    # -------------------------

    def _set_latest_status(self, status: str) -> None:
        with self._latest_status_lock:
            self._latest_status = status
            self._latest_status_ts = time.monotonic()

    def get_latest_status(self) -> Optional[str]:
        with self._latest_status_lock:
            return self._latest_status

    def get_latest_status_timestamp(self) -> float:
        with self._latest_status_lock:
            return self._latest_status_ts

    def get_latest_row(self) -> Optional[list[float]]:
        with self._latest_row_lock:
            return list(self._latest_row) if self._latest_row is not None else None

    def get_latest_row_timestamp(self) -> float:
        with self._latest_row_lock:
            return self._latest_row_ts

    def get_latest_raw_line(self) -> Optional[str]:
        with self._latest_row_lock:
            return self._latest_raw_row

    def get_latest_raw_line_timestamp(self) -> float:
        with self._latest_row_lock:
            return self._latest_raw_row_ts

    def _set_last_error(self, msg: str) -> None:
        with self._last_error_lock:
            self._last_error = msg

    def get_last_error(self) -> Optional[str]:
        with self._last_error_lock:
            return self._last_error
