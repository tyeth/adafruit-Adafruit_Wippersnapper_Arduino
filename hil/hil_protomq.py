"""Run a ProtoMQ broker and drive its HTTP control API, for HIL tests
that check what the DUT does on the broker. See ProtoMQ's
docs/api/README.md.

    with ProtoMQServer("~/ProtoMQ", mqtt_port=1884, log_path=log) as pmq:
        pmq.status()["protomq"]["gitSha"]
        pmq.wait_for_client("io-wipper-...", timeout=5)
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_DIR = "~/ProtoMQ"
DEFAULT_MQTT_PORT = 1884

log = logging.getLogger("hil.protomq")


class ProtoMQError(Exception):
    """The API is unreachable or answered with an error."""


class ProtoMQTimeout(ProtoMQError, AssertionError):
    """A wait_for_* call ran out of time."""


class ProtoMQ:
    def __init__(self, url, timeout=5):
        self.url = url.rstrip("/")
        self.timeout = timeout  # seconds, per request

    def _request(self, method, path, body=None):
        """Send a request to /api<path>; the reply, which must be status OK."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{self.url}/api{path}", data=data,
                                     method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                reply = json.load(resp)
        except urllib.error.HTTPError as e:  # before OSError: it's one too
            raise ProtoMQError(f"{method} {path}: HTTP {e.code}") from e
        except (OSError, ValueError) as e:  # unreachable, or not JSON
            raise ProtoMQError(f"{method} {path} at {self.url}: {e}") from e
        if not isinstance(reply, dict) or reply.get("status") != "OK":
            message = reply.get("message") if isinstance(reply, dict) else None
            raise ProtoMQError(f"{method} {path}: {message or reply!r}")
        return reply

    def status(self):
        """ProtoMQ's version, proto bundle hashes and source, ports, seq."""
        return self._request("GET", "/status")

    def clients(self):
        """{client id: [subscribed topics]} for the clients connected now."""
        return {c["id"]: [s["topic"] for s in c["subscriptions"]]
                for c in self._request("GET", "/clients")["clients"]}

    def is_connected(self, client_id):
        clients = self.clients()
        log.info("is_connected(%s): %s; connected: %s", client_id,
                 client_id in clients, sorted(clients) or "none")
        return client_id in clients

    def wait_for_client(self, client_id, timeout, interval=0.05):
        """Poll every interval seconds until client_id is connected; its
        subscribed topics. A poll is a local HTTP request of a few ms, so
        it notices the connect within about interval. The last poll is at
        the deadline."""
        start = time.monotonic()
        deadline = start + timeout
        polls = 0
        while True:
            clients = self.clients()
            polls += 1
            now = time.monotonic()
            if client_id in clients:
                log.info("wait_for_client(%s): connected after %.2fs (%d "
                         "poll(s)), subscribed to %s", client_id,
                         now - start, polls, clients[client_id])
                return clients[client_id]
            if now >= deadline:
                raise ProtoMQTimeout(
                    f"{client_id} not connected to ProtoMQ at {self.url} after "
                    f"{timeout}s ({polls} polls); connected: "
                    f"{sorted(clients) or 'none'}")
            time.sleep(min(interval, deadline - now))


def lan_ip():
    """This host's address on the interface with the default route, which
    the DUT can reach if it's on the same network. Sends no packets."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: only picks the route
        except OSError as e:
            raise ProtoMQError(f"no default route to find this host's IP: {e}") from e
        return s.getsockname()[0]


def free_port():
    """A TCP port nothing listens on now."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def check_port_free(port):
    """Raise ProtoMQError if something listens on port. SO_REUSEADDR, as
    node uses, so the TIME_WAIT sockets of the last broker don't count."""
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
        except OSError as e:
            raise ProtoMQError(
                f"port {port} is in use ({e.strerror}); is another ProtoMQ "
                f"running? (pgrep -af 'node main.js')") from e


class ProtoMQServer(ProtoMQ):
    """A ProtoMQ broker run from its checkout in directory, with MQTT on
    mqtt_port, the HTTP API and web UI on http_port (default a free port)
    and the WebSocket on a free port, its output written to log_path. A
    context manager: start() on enter, stop() on exit."""

    def __init__(self, directory=DEFAULT_DIR, mqtt_port=DEFAULT_MQTT_PORT,
                 http_port=None, log_path=None, node=None):
        self.directory = Path(directory).expanduser()
        self.mqtt_port = mqtt_port
        self.http_port = http_port or free_port()
        self.ws_port = free_port()
        self.log_path = Path(log_path) if log_path else None
        self.node = node or shutil.which("node")
        self.proc = None
        super().__init__(f"http://localhost:{self.http_port}")

    def _log_tail(self, lines=20):
        if not self.log_path or not self.log_path.exists():
            return ""
        tail = self.log_path.read_text(errors="replace").splitlines()[-lines:]
        return "\n".join(tail)

    def start(self, timeout=20):
        """Start the broker and wait until its API answers."""
        if not (self.directory / "main.js").is_file():
            raise ProtoMQError(f"no ProtoMQ checkout at {self.directory}")
        if not self.node:
            raise ProtoMQError("node not found on PATH")
        for port in (self.mqtt_port, self.http_port, self.ws_port):
            check_port_free(port)
        log = open(self.log_path, "w") if self.log_path else subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                [self.node, "main.js", f"--mqtt-port={self.mqtt_port}",
                 f"--ws-port={self.ws_port}", f"--http-port={self.http_port}"],
                cwd=self.directory, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, env={**os.environ, "FORCE_COLOR": "0"})
        finally:
            if log is not subprocess.DEVNULL:
                log.close()  # the child has its own copy
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.poll() is not None:
                code = self.proc.returncode
                self.proc = None
                raise ProtoMQError(f"ProtoMQ exited with {code} on startup:\n"
                                   f"{self._log_tail()}")
            try:
                status = self.status()
                break
            except ProtoMQError:
                if time.monotonic() >= deadline:
                    self.stop()
                    raise ProtoMQError(f"ProtoMQ API didn't answer within "
                                       f"{timeout}s:\n{self._log_tail()}") from None
                time.sleep(0.2)
        if status.get("ports", {}).get("mqtt") != self.mqtt_port:
            self.stop()
            raise ProtoMQError(f"ProtoMQ reports MQTT port "
                               f"{status.get('ports')}, not {self.mqtt_port}")
        return status

    def stop(self, timeout=5):
        """Stop the broker: SIGTERM, then SIGKILL after timeout seconds."""
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
