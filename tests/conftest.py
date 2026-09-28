"""Loopback-only MASsoft emulator: never reaches beamline equipment."""

import contextlib
import re
import socketserver
import threading
import time

import pytest


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        sim = self.server
        self.request.settimeout(2)
        associated = None
        try:
            time.sleep(sim.greeting_delay)
            self.wfile.write(b"101\r\n")
            while not sim.stopping.is_set():
                line = self.rfile.readline()
                if not line:
                    return
                cmd = line.decode().strip()
                sim.commands.append((id(self), cmd))
                if cmd.startswith('-f"'):
                    path = re.match(r'-f"([^"]+)"', cmd)[1]
                    if associated is not None and associated != path:
                        reply = "0"
                    else:
                        associated, reply = path, "1"
                elif cmd.startswith("-xFilename"):
                    reply = associated or "0"
                elif cmd.startswith("-xStatus"):
                    reply = sim.status
                elif cmd.startswith("-xAbort"):
                    reply = "0" if sim.abort_fails else "1"
                    if not sim.abort_fails and not sim.abort_stuck:
                        sim.status = "StoppedActive"
                elif cmd.startswith("-xClose"):
                    reply = "1"
                elif cmd.startswith("-xSlow"):
                    time.sleep(0.2)
                    reply = "LATE"
                elif cmd.startswith("-lLegends"):
                    reply = "\t".join(sim.legends)
                elif cmd.startswith("-lStatus"):
                    self.wfile.write((sim.status + "\r\n").encode())
                    while not sim.stopping.wait(0.01) and not sim.drop_links.is_set():
                        pass
                    return
                elif cmd.startswith("-lData"):
                    while not sim.stopping.is_set() and not sim.drop_links.is_set():
                        self.wfile.write((sim.row + "\r\n").encode())
                        if sim.stopping.wait(0.02):
                            return
                    return
                else:
                    reply = "1"
                self.wfile.write((reply + "\r\n").encode())
        except (OSError, ValueError):
            pass


class Simulator(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.commands = []
        self.legends = [f"mass {i}" for i in range(1, 21)]
        self.row = "00:00:00\t0\t" + "\t".join(str(i * 1e-10) for i in range(20))
        self.status = "ScanningActive"
        self.abort_fails = self.abort_stuck = False
        self.greeting_delay = 0
        self.stopping = threading.Event()
        self.drop_links = threading.Event()


@pytest.fixture
def sim():
    server = Simulator()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield server
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture(params=["massoft_client", "massoft_client_aj2"])
def client(request, sim):
    import importlib

    from massoft_protocol import MASsoftConfig

    cls = importlib.import_module(request.param).MASsoftClient
    obj = cls(
        MASsoftConfig(
            host="127.0.0.1",
            port=sim.server_address[1],
            retry_s=0,
            command_timeout_s=0.8,
            link_chunk_timeout_s=0.03,
        )
    )
    try:
        yield obj
    finally:
        with contextlib.suppress(Exception):
            obj.disconnect()


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Simulator condition timed out")
