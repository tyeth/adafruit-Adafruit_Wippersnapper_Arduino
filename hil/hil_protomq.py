"""Run a ProtoMQ broker and drive its HTTP control API, for HIL tests
that check what the DUT does on the broker. See ProtoMQ's
docs/api/README.md.

    with ProtoMQServer("~/ProtoMQ", mqtt_port=1884, log_path=log) as pmq:
        pmq.enable_checkin_fallback(False)
        pmq.set_autorespond("checkin.request",
                            {"checkin": {"response": {"response": "R_OK"}}})
        ...                                  # boot the DUT
        pmq.track(uid)                       # before it publishes
        pmq.expect_client(uid, timeout=60)
        pmq.expect_d2b_message(uid, "checkin.request", timeout=30)
        pmq.publish(uid, {"digital": {"add": {"pin": "D14",
                                              "gpioDirection": "D_OUTPUT"}}})

Messages are dicts as ProtoMQ decodes them: camelCase field names, enum
names as strings, and fields at their default value (0, false, "")
left out. A path like "checkin.request" names a nested field, and a
match is a partial pattern on the value at the path: dicts match
recursively, lists by index, floats to float precision, "*" matches
anything.
"""

import ctypes
import json
import logging
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_DIR = "~/ProtoMQ"
DEFAULT_MQTT_PORT = 1884

log = logging.getLogger("hil.protomq")

PR_SET_PDEATHSIG = 1  # <linux/prctl.h>


class ProtoMQError(Exception):
    """The API is unreachable or answered with an error."""


class ProtoMQTimeout(ProtoMQError, AssertionError):
    """An expect_* call ran out of time."""


class ProtoMQUnexpected(ProtoMQError, AssertionError):
    """An assert_no_* call saw the message it was told not to."""


def get_path(message, path):
    """The value at the dot path in message, or None if it isn't there."""
    for key in path.split("."):
        if not isinstance(message, dict) or key not in message:
            return None
        message = message[key]
    return message


def nest(path, value):
    """value nested under the dot path: nest("a.b", 1) == {"a": {"b": 1}}."""
    for key in reversed(path.split(".")):
        value = {key: value}
    return value


def matches(actual, pattern):
    """Whether actual matches the partial pattern, as ProtoMQ's match:
    "*" matches anything, dicts recursively, lists by index. A float
    matches to float precision: a proto float field holding 3.3 decodes
    as 3.299999952316284."""
    if pattern == "*":
        return True
    if isinstance(pattern, float) and isinstance(actual, (int, float)) \
            and not isinstance(actual, bool):
        return math.isclose(actual, pattern, rel_tol=1e-6)
    if isinstance(pattern, dict):
        return isinstance(actual, dict) and all(
            k in actual and matches(actual[k], v) for k, v in pattern.items())
    if isinstance(pattern, list):
        return (isinstance(actual, list) and len(actual) >= len(pattern)
                and all(matches(a, p) for a, p in zip(actual, pattern)))
    return actual == pattern


class ProtoMQ:
    def __init__(self, url, timeout=5):
        self.url = url.rstrip("/")
        self.timeout = timeout  # seconds, per request
        self._tracked = set()
        # (client id, "inbox"/"outbox") -> seq of the last message expected
        self._cursors = {}
        # (client_id, box) -> the last message an expect_* call matched
        self._last = {}

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
            try:
                message = json.load(e).get("message")
            except (OSError, ValueError, AttributeError):
                message = None
            raise ProtoMQError(f"{method} {path}: HTTP {e.code}"
                               + (f": {message}" if message else "")) from e
        except (OSError, ValueError) as e:  # unreachable, or not JSON
            raise ProtoMQError(f"{method} {path} at {self.url}: {e}") from e
        if not isinstance(reply, dict) or reply.get("status") != "OK":
            message = reply.get("message") if isinstance(reply, dict) else None
            raise ProtoMQError(f"{method} {path}: {message or reply!r}")
        return reply

    def _poll(self, check, timeout, interval):
        """Call check() every interval seconds until it returns something
        other than None; that, or None at the deadline, where the last
        call is. Returns (result, polls, seconds)."""
        start = time.monotonic()
        deadline = start + timeout
        polls = 0
        while True:
            result = check()
            polls += 1
            now = time.monotonic()
            if result is not None or now >= deadline:
                return result, polls, now - start
            time.sleep(min(interval, deadline - now))

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

    def expect_client(self, client_id, timeout, interval=0.05):
        """Poll every interval seconds until client_id is connected; its
        subscribed topics. A poll is a local HTTP request of a few ms, so
        it notices the connect within about interval. The last poll is at
        the deadline."""
        seen = {}
        def check():
            seen.update(clients=self.clients())
            return seen["clients"].get(client_id)
        topics, polls, took = self._poll(check, timeout, interval)
        if topics is None:
            raise ProtoMQTimeout(
                f"{client_id} not connected to ProtoMQ at {self.url} after "
                f"{timeout}s ({polls} polls); connected: "
                f"{sorted(seen['clients']) or 'none'}")
        log.info("expect_client(%s): connected after %.2fs (%d poll(s)), "
                 "subscribed to %s", client_id, took, polls, topics)
        return topics

    # --- Auto-responses ---------------------------------------------------

    def set_autorespond(self, request, response, *, match=None):
        """Have the broker answer each D2B message that has the dot path
        request (e.g. "checkin.request"), and whose value there matches
        match, with the B2D message response. The first matching one
        registered answers, ahead of the checkin fallback. ProtoMQ rejects
        a response that isn't a valid B2D message, but drops unknown keys:
        a misspelled field is sent as its default."""
        body = {"trigger": request, "response": response}
        if match is not None:
            body["match"] = nest(request, match)
        count = self._request("POST", "/autoresponse", body)["count"]
        log.info("set_autorespond(%s%s): %s", request,
                 f", match={match}" if match is not None else "", response)
        return count

    def clear_autorespond(self):
        """Remove every set_autorespond() answer; how many there were."""
        removed = self._request("DELETE", "/autoresponse")["removed"]
        log.info("clear_autorespond(): removed %d", removed)
        return removed

    def enable_checkin_fallback(self, enabled):
        """Turn the broker's own answer to a checkin.request (R_OK with
        made-up pin counts, on by default) on or off."""
        reply = self._request("POST", "/scripts/fallback-checkin",
                              {"enabled": bool(enabled)})
        if reply.get("fallbackCheckinEnabled") is not bool(enabled):
            raise ProtoMQError(f"checkin fallback is "
                               f"{reply.get('fallbackCheckinEnabled')}, "
                               f"not {bool(enabled)}")
        log.info("enable_checkin_fallback(%s)", bool(enabled))

    # --- Messages ---------------------------------------------------------

    def publish(self, client_id, message):
        """Send client_id the B2D message (a dict, e.g. {"digital": {"add":
        {...}}}) now, on its ws-b2d topic; the topic. ProtoMQ rejects a
        message that isn't a valid B2D message, but drops unknown keys, as
        in set_autorespond()."""
        topic = self._request("POST", "/publish",
                              {"client": client_id, "message": message})["topic"]
        log.info("publish(%s) on %s: %s", client_id, topic, message)
        return topic

    def track(self, client_id):
        """Start capturing the messages client_id sends (its outbox) and
        receives (its inbox). Call it before the messages: ProtoMQ keeps
        nothing from before. Once per client: ProtoMQ drops the capture
        if asked again."""
        if client_id in self._tracked:
            raise ProtoMQError(f"{client_id} is already tracked")
        self._request("POST", "/track_deliveries", {"client": client_id})
        self._tracked.add(client_id)
        log.info("track(%s)", client_id)

    def _messages(self, client_id, box):
        """The client's captured messages in box ("inbox" or "outbox"),
        oldest first, without clearing them: [{seq, time, topic, payload}],
        time in ms since the epoch when the broker received it."""
        if client_id not in self._tracked:
            raise ProtoMQError(f"{client_id} isn't tracked: call track() "
                               "before it sends anything")
        reply = self._request("POST", "/dump_deliveries",
                              {"client": client_id, "clear": False})
        return sorted(reply["deliveries"][box], key=lambda m: m["seq"])

    def _find(self, client_id, box, path, match):
        """The first message in box after the cursor that has path, and
        whose value there matches match; (message, value) or None. Also
        the messages after the cursor, for error messages."""
        cursor = self._cursors.get((client_id, box), 0)
        later = [m for m in self._messages(client_id, box) if m["seq"] > cursor]
        for m in later:
            value = get_path(m["payload"], path)
            if value is not None and (match is None or matches(value, match)):
                return (m, value), later
        return None, later

    @staticmethod
    def _describe(messages):
        """Messages as "#seq a.b", their first two levels of keys, for
        error messages."""
        def keys(payload):
            path = []
            while isinstance(payload, dict) and payload and len(path) < 2:
                key = next(iter(payload))
                path.append(key)
                payload = payload[key]
            return ".".join(path) or "?"
        return ", ".join(f"#{m['seq']} {keys(m['payload'])}"
                         for m in messages) or "none"

    def _expect(self, client_id, box, direction, path, match, timeout, interval):
        found = {}
        def check():
            result, found["later"] = self._find(client_id, box, path, match)
            return result
        result, polls, took = self._poll(check, timeout, interval)
        what = path + (f" matching {match}" if match is not None else "")
        if result is None:
            raise ProtoMQTimeout(
                f"no {direction} {what} for {client_id} after {timeout}s "
                f"({polls} polls); {direction} since the last expected: "
                f"{self._describe(found['later'])}")
        message, value = result
        self._cursors[(client_id, box)] = message["seq"]
        self._last[(client_id, box)] = message
        log.info("expect_%s_message(%s, %s): #%d after %.2fs: %s", direction,
                 client_id, path, message["seq"], took, value)
        return value

    def expect_d2b_message(self, client_id, path, *, match=None, timeout,
                           interval=0.05):
        """Poll until client_id has sent a D2B message with the dot path
        (e.g. "checkin.request") whose value there matches match, after
        the last one expected; that value. Raises ProtoMQTimeout, a test
        failure, listing what it did send. Needs track(client_id)."""
        return self._expect(client_id, "outbox", "d2b", path, match, timeout,
                            interval)

    def expect_b2d_message(self, client_id, path, *, match=None, timeout,
                           interval=0.05):
        """As expect_d2b_message(), for the B2D messages client_id received
        on the topics it subscribed to."""
        return self._expect(client_id, "inbox", "b2d", path, match, timeout,
                            interval)

    def last_d2b_time(self, client_id):
        """When the broker received the D2B message the last
        expect_d2b_message() for client_id matched, in seconds since the
        epoch like time.time(); None if ProtoMQ doesn't timestamp
        deliveries."""
        message = self._last.get((client_id, "outbox"))
        if message is None:
            raise ProtoMQError(f"no d2b message expected yet for {client_id}")
        ms = message.get("time")
        return None if ms is None else ms / 1000

    def assert_no_d2b_message(self, client_id, path, *, match=None, within,
                              interval=0.05):
        """Watch for within seconds that client_id sends no D2B message with
        the dot path whose value matches match, after the last one
        expected. Raises ProtoMQUnexpected, a test failure, if it does."""
        result, _, took = self._poll(
            lambda: self._find(client_id, "outbox", path, match)[0],
            within, interval)
        if result is not None:
            message, value = result
            raise ProtoMQUnexpected(
                f"{client_id} sent d2b {path} (#{message['seq']}) after "
                f"{took:.2f}s: {value}")
        log.info("assert_no_d2b_message(%s, %s): none in %ss", client_id,
                 path, within)


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


def _die_with_parent(parent_pid):
    """A preexec_fn for Popen that has Linux SIGKILL the child when
    parent_pid dies, even by SIGKILL, when no teardown runs."""
    def preexec():
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
            os._exit(127)
        if os.getppid() != parent_pid:  # it died before the prctl
            os._exit(127)
    return preexec


class ProtoMQServer(ProtoMQ):
    """A ProtoMQ broker run from its checkout in directory, with MQTT on
    mqtt_port, the HTTP API and web UI on http_port (default a free port)
    and the WebSocket on a free port, its output written to log_path. A
    context manager: start() on enter, stop() on exit. On Linux, the
    broker is also killed if this process dies without calling stop()."""

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
                stderr=subprocess.STDOUT, env={**os.environ, "FORCE_COLOR": "0"},
                preexec_fn=(_die_with_parent(os.getpid())
                            if sys.platform == "linux" else None))
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
