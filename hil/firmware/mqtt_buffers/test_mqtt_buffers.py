"""
HIL tests for the firmware's MQTT dynamic buffers (WS_MQTT_BUFFER_SZ =
2048): the boot allocation, B2D payloads between 512 B and the buffer's
limit, an oversized B2D packet the MQTT library drains without losing
sync, and the D2B publish size bound.

The large B2D messages are checkin.Responses padded with repeated
componentAdds.digitalAdds for one output pin: a re-Add replaces the pin,
so any count is safe, and each one logs "[digital] Added new pin:". Their
exact encoded size comes from ProtoMQ's own encoder (encoded_sizes()),
which is what the broker sends.

Needs firmware with the [mqtt] buffer logs (WS_DEBUG) and the
Adafruit_MQTT_Library fork with MQTT_ERROR enabled (see the HIL
verification doc). Not covered here: D2B too large (no component on the
HIL boards publishes over ~2000 B) and the forced allocation failure
(needs a special build with a huge WS_MQTT_BUFFER_SZ).

Exercises:
- `src/wippersnapper.cpp` MQTT buffer allocation, cbBrokerToDevice(),
  PublishD2b()
- Adafruit_MQTT_Library oversized-packet drain
"""

import json
import logging
import re
import time

import pytest

import hil_protomq

log = logging.getLogger("hil.mqtt_buffers")

# --- Constants (from the verification doc) --------------------------------
BUF = 2048                 # WS_MQTT_BUFFER_SZ
CAP = BUF - 1              # largest subscription payload (1 B for NUL)
B2D_MIN = 512              # Case 2's lower bound (exclusive)
B2D_DOC_MAX = 1990         # Case 2's upper bound
OVERSIZED = 2100           # Case 3's minimum payload size
# Headroom under BUF - 8 - len(topic) for the near-max payload, since
# the device UID's length is only known after the boot
NEAR_MAX_MARGIN = 8
# The UID's MAC-derived suffix is at most this many characters
UID_SUFFIX_MAX = 12

# Heap delta for the boot allocation (heap_before - heap_after), bytes
HEAP_NO_PSRAM = (2048, 3000)   # [min, max)
HEAP_PSRAM_MAX = 1000
PSRAM_CHIPS = ("esp32s2", "esp32s3")

# The output pin the padding Adds and the follow-up write use
PAD_PIN = "D14"
PIN_TIMEOUT = 2
# How long the DUT may take to log a B2D message once it's published
RX_TIMEOUT = 10
# Case 3: wait between the oversized and the follow-up message
OVERSIZED_GAP = 2
# The library's drop line reports the space left for the packet after its
# fixed header (1 type byte + 1..4 remaining-length bytes), so it varies
# per packet: conservatively BUF - 5 .. BUF
MQTT_FIXED_HEADER_MAX = 5

# --- Serial patterns -------------------------------------------------------
BUF_ALLOC_RE = re.compile(r"^\[mqtt\] buf alloc: requested=(\d+) got=(\d+) "
                          r"heap_before=(\d+) heap_after=(\d+)$")
SUB_BUF_RE = re.compile(r"^\[mqtt\] b2d sub buf: lastread_max=(\d+) "
                        r"mqtt_buf=(\d+)$")
B2D_RX_RE = re.compile(rf"^\[mqtt\] B2D rx len=(\d+) cap={CAP}$")
B2D_OK_RE = re.compile(r"^\[mqtt\] B2D decode ok$")
DROP_RE = re.compile(r"^Incoming packet \((\d+) bytes\) larger than buffer "
                     r"size \((\d+) bytes\), dropping$")
DRAINED_RE = re.compile(r"^Oversized packet drained OK$")
PUBLISHING_RE = re.compile(r"^Publishing signal message to broker\.\.\.$")
PUBLISHED_RE = re.compile(r"^Published!$")
D2B_OK_RE = re.compile(r"^\[mqtt\] D2B tx ok tag=(\d+) size=(\d+)$")
DIGITAL_ADDED_RE = re.compile(r"^\[digital\] Added new pin:$")
RECONNECT_RE = re.compile(r"^Connecting to AIO MQTT|^Connected to Adafruit IO$")

# Lines that fail any test here (Cases 4 and the global failure list)
FAILURE_RES = [
    re.compile(r"^\[mqtt\] (TX |RX )?ERROR:"),
    re.compile(r"ERROR \[RESET\]:"),
    re.compile(r"Subscription packet \(\d+ bytes\) larger than buffer size "
               r"\(\d+ bytes\), truncated"),
    re.compile(r"Publish packet \(\d+ bytes\) larger than buffer size "
               r"\(\d+ bytes\), truncated"),
    re.compile(r"Short packet read"),
    re.compile(r"Timed out reading packet length"),
    re.compile(r"Timed out draining oversized packet"),
    re.compile(r"Malformed packet len"),
]


# --- Helpers ---------------------------------------------------------------


def encoded_sizes(messages):
    """The BrokerToDevice-encoded size of each message, as the broker
    sends it (see hil_protomq.encoded_sizes())."""
    return hil_protomq.encoded_sizes(messages)


def checkin_response(board_definition, component_adds=None):
    """The checkin.Response the checkin fixture sends, as its B2D
    message."""
    definition = board_definition.definition
    response = {
        "response": "R_OK",
        "totalGpioPins": len(definition["components"]["digitalPins"]),
        "totalAnalogPins": len(definition["components"]["analogPins"]),
        "referenceVoltage": definition["mcuRefVoltage"],
    }
    if component_adds is not None:
        response["componentAdds"] = component_adds
    return {"checkin": {"response": response}}


def pad_add(pin=PAD_PIN):
    """A digital.Add of pin as an output written high."""
    return {"pin": pin, "gpioDirection": "D_OUTPUT",
            "write": {"pin": pin, "value": {"boolValue": True}}}


def padded_adds(board_definition, lo, hi, max_count=200):
    """(componentAdds, size): the fewest repeated pad_add()s whose checkin
    response encodes to a size in (lo, hi]."""
    candidates = [{"digitalAdds": [pad_add()] * k} for k in range(max_count)]
    sizes = encoded_sizes([checkin_response(board_definition, c)
                           for c in candidates])
    for adds, size in zip(candidates, sizes):
        if lo < size <= hi:
            return adds, size
    pytest.fail(f"no pad count gives a checkin response in ({lo}, {hi}]: "
                f"sizes {sizes[0]}..{sizes[-1]}", pytrace=False)


def find_all(serial, regex, since):
    """[(line index, match)] for every line from since that matches."""
    return [(l.index, m) for l in serial.lines(since)
            if (m := regex.search(l.text))]


def find_first(serial, regex, since, what):
    found = find_all(serial, regex, since)
    assert found, f"no {what} line ({regex.pattern!r}) since line {since}"
    return found[0]


def assert_clean(serial, since=None):
    """Fail on any Case 4 or global failure line since the boot."""
    for regex in FAILURE_RES:
        serial.assert_none(regex, since=since)


def io_username(flashed_board):
    return json.loads(flashed_board.secrets_path.read_text())["io_username"]


def b2d_topic(protomq, uid):
    topics = protomq.clients().get(uid, [])
    b2d = [t for t in topics if "/ws-b2d/" in t]
    assert b2d, f"{uid} isn't subscribed to a ws-b2d topic: {topics}"
    return b2d[0]


def d2b_max(b2d):
    """The largest D2B payload the firmware sends: BUF - (9 + len(d2b))."""
    return BUF - (9 + len(b2d.replace("/ws-b2d/", "/ws-d2b/")))


# --- Case 1 + Case 5: boot allocation and D2B publishes --------------------


@pytest.mark.flaky(reruns=1)
def test_mqtt_buffer_boot_allocation(board, serial, checkin, protomq,
                                     record_property):
    # --- Act ----------------------------------------------------------
    # Reset the DUT and see it through the checkin
    uid = checkin()
    boot = serial.boot

    # --- Assert -------------------------------------------------------
    # The packet buffer allocation, then the subscription buffer, in order
    alloc_at, alloc = find_first(serial, BUF_ALLOC_RE, boot, "buf alloc")
    sub_at, sub = find_first(serial, SUB_BUF_RE, alloc_at, "b2d sub buf")
    requested, got, heap_before, heap_after = map(int, alloc.groups())
    lastread_max, mqtt_buf = map(int, sub.groups())
    assert requested == BUF, f"requested={requested}, expected {BUF}"
    assert got == BUF, f"got={got}, expected {BUF}"
    assert lastread_max == BUF, f"lastread_max={lastread_max}, expected {BUF}"
    assert mqtt_buf == BUF, f"mqtt_buf={mqtt_buf}, expected {BUF}"

    # The checkin completed after both
    done_at, _ = find_first(serial, re.compile(r"^Completed checkin process!$"),
                            boot, "checkin complete")
    assert done_at > sub_at, "checkin completed before the buffer logs"

    # The heap delta covers the network client, the MQTT client and the
    # packet buffer (in PSRAM on boards that have it). Recorded as the
    # board's regression baseline.
    if heap_before == 0 and heap_after == 0:
        log.info("no free-heap API on this arch: skipping the delta check")
    else:
        delta = heap_before - heap_after
        record_property("mqtt_buf_heap_delta", delta)
        log.info("%s: mqtt buffer heap delta %d B (before %d, after %d)",
                 board.name, delta, heap_before, heap_after)
        psram = board.entry.get("psram", board.chip in PSRAM_CHIPS)
        if psram:
            assert delta < HEAP_PSRAM_MAX, (
                f"heap delta {delta} B, expected < {HEAP_PSRAM_MAX} B with "
                "the packet buffer in PSRAM")
        else:
            lo, hi = HEAP_NO_PSRAM
            assert lo <= delta < hi, (
                f"heap delta {delta} B, expected {lo}..{hi - 1} B")

    # Case 5: each D2B publish logs Publishing..., Published!, then tx ok,
    # with a size within the D2B max
    limit = d2b_max(b2d_topic(protomq, uid))
    oks = find_all(serial, D2B_OK_RE, boot)
    assert len(oks) >= 2, (
        f"expected D2B tx ok for checkin.request and checkin.complete, got "
        f"{len(oks)}")
    publishing = [i for i, _ in find_all(serial, PUBLISHING_RE, boot)]
    published = [i for i, _ in find_all(serial, PUBLISHED_RE, boot)]
    for at, ok in oks:
        tag, size = map(int, ok.groups())
        pub = [i for i in published if i < at]
        assert pub, f"D2B tx ok tag={tag} at line {at} has no Published!"
        assert any(i < pub[-1] for i in publishing), (
            f"D2B tx ok tag={tag} at line {at} has no Publishing... before "
            "its Published!")
        assert size <= limit, f"D2B tag={tag} size={size} > max {limit}"

    assert_clean(serial)


# --- Case 2: B2D payloads between 512 B and the buffer's limit -------------


def _target_range(board_definition, flashed_board, which):
    """(lo, hi] for the checkin response's encoded size."""
    if which == "mid":
        return B2D_MIN, 1100
    # near the limit, BUF - 8 - len(topic), for the longest UID the board
    # can have
    topic_max = (len(io_username(flashed_board)) + len("/ws-b2d/")
                 + len(board_definition.board_id) + UID_SUFFIX_MAX)
    hi = min(B2D_DOC_MAX, BUF - 8 - topic_max - NEAR_MAX_MARGIN)
    return hi - 40, hi


@pytest.mark.flaky(reruns=1)
@pytest.mark.parametrize("which", ["mid", "near_max"])
def test_mqtt_b2d_large_payload(which, board_definition, flashed_board,
                                serial, checkin, protomq, probe):
    # --- Arrange ------------------------------------------------------
    # A checkin.Response padded with repeated Adds of PAD_PIN to a size
    # between 512 B and the buffer's limit
    lo, hi = _target_range(board_definition, flashed_board, which)
    adds, size = padded_adds(board_definition, lo, hi)
    count = len(adds["digitalAdds"])
    log.info("checkin response: %d B with %d digitalAdds", size, count)

    # --- Act ----------------------------------------------------------
    # The broker answers the checkin with it
    uid = checkin(component_adds=adds)
    boot = serial.boot

    # --- Assert -------------------------------------------------------
    # The near-max case really fits the buffer, now the topic is known
    topic = b2d_topic(protomq, uid)
    assert size <= BUF - 8 - len(topic), (
        f"test bug: {size} B doesn't fit with topic {topic!r}")

    # The DUT received the whole response and decoded it
    rxs = find_all(serial, B2D_RX_RE, boot)
    hits = [i for i, m in rxs if int(m.group(1)) == size]
    assert hits, (f"no B2D rx len={size}; got lens "
                  f"{[int(m.group(1)) for _, m in rxs]}")
    rx_at = hits[0]
    ok_at, _ = find_first(serial, B2D_OK_RE, rx_at, "B2D decode ok")

    # It acted on every Add, and PAD_PIN is high from their writes
    added = [i for i, _ in find_all(serial, DIGITAL_ADDED_RE, rx_at)
             if i < ok_at]
    assert len(added) == count, (
        f"expected {count} digital Adds between B2D rx and decode ok, got "
        f"{len(added)}")
    assert probe.value(PAD_PIN), f"{PAD_PIN} should be high from the Adds"

    serial.assert_none(r"larger than buffer size", since=boot)
    assert_clean(serial)


# --- Case 3: oversized B2D packet is drained, the stream stays in sync ----


@pytest.mark.flaky(reruns=1)
def test_mqtt_b2d_oversized_dropped(board_definition, serial, checkin,
                                    protomq, probe):
    # --- Arrange ------------------------------------------------------
    # PAD_PIN starts high from the checkin's Add
    uid = checkin(component_adds={"digitalAdds": [pad_add()]})
    assert probe.value(PAD_PIN), f"{PAD_PIN} should be high from the Add"

    # A B2D message over the buffer: a checkin response padded past
    # OVERSIZED. The DUT never decodes it, so its content doesn't matter.
    big_adds, big_size = padded_adds(board_definition, OVERSIZED,
                                     OVERSIZED + 100)
    big = checkin_response(board_definition, big_adds)
    small = {"digital": {"write": {"pin": PAD_PIN,
                                   "value": {"boolValue": False}}}}
    (small_size,) = encoded_sizes([small])

    # --- Act ----------------------------------------------------------
    mark = serial.mark()
    topic = protomq.publish(uid, big)

    # --- Assert -------------------------------------------------------
    # The library drops it: remaining length ~ payload + topic + header
    m = serial.expect(DROP_RE, timeout=RX_TIMEOUT)
    remaining, limit = map(int, m.groups())
    base = big_size + len(topic) + 2
    assert base <= remaining <= base + 2, (
        f"dropped packet is {remaining} B, expected {base}..{base + 2} "
        f"(payload {big_size} B, topic {len(topic)} B)")
    assert BUF - MQTT_FIXED_HEADER_MAX <= limit <= BUF, (
        f"library buffer size {limit}, expected "
        f"{BUF - MQTT_FIXED_HEADER_MAX}..{BUF}")
    assert remaining > limit, f"dropped {remaining} B fits in {limit} B"
    serial.expect(DRAINED_RE, timeout=RX_TIMEOUT)
    serial.assert_none(B2D_RX_RE, since=mark)

    # --- Act ----------------------------------------------------------
    # A small valid message after it
    time.sleep(OVERSIZED_GAP)
    with probe.expect_digitalout_transition(PAD_PIN, True, False,
                                            timeout=PIN_TIMEOUT + RX_TIMEOUT):
        protomq.publish(uid, small)
        serial.expect(rf"^\[mqtt\] B2D rx len={small_size} cap={CAP}$",
                      timeout=RX_TIMEOUT)
        serial.expect(B2D_OK_RE, timeout=RX_TIMEOUT)

    # --- Assert -------------------------------------------------------
    # No reconnect between them: the stream stayed in sync
    serial.assert_none(RECONNECT_RE, since=mark)
    assert protomq.is_connected(uid), f"{uid} dropped off the broker"
    assert_clean(serial)
