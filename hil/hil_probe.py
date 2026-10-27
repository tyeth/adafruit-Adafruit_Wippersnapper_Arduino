"""Read and drive the DUT's pins from the HIL host, through an I2C GPIO
expander wired to them, for HIL tests that check what the DUT drives on a
pin, or what it reads from one.

The board entry describes the wiring:

    "probe": {"type": "mcp23017", "address": "0x20",
              "pin_map": {"D14": "GPA0", "D32": "GPA1"}}

pin_map maps a DUT pin name to the expander pin it's wired to, GPA0-GPA7
or GPB0-GPB7.

    probe = Probe(entry["probe"])
    probe.value("D14")                       # read it once, now
    with probe.expect_digitalout_transition("D14", False, True, timeout=2):
        pmq.publish(uid, {"digital": {"write": ...}})   # make it go high
    probe.drive("D32", True)                 # drive the DUT's input high
    probe.release("D32")                     # back to an input, not driving
    probe.release()                          # every expander pin, mapped or not

The MCP23017 keeps driving its outputs when the DUT loses power, which can
damage both, so all 16 expander pins are made inputs with no pull when a
Probe is made, by release() with no pin, and by close().

Blinka and adafruit_mcp230xx are imported when a Probe is made, so this
module imports on hosts without them.
"""

import contextlib
import logging
import re
import threading
import time

PROBE_TYPES = ("mcp23017",)
EXPANDER_PINS = 16
EXPANDER_PIN_RE = re.compile(r"^GP([AB])([0-7])$")
TRACE_SAMPLES = 10  # samples shown when a transition times out

log = logging.getLogger("hil.probe")


class ProbeError(Exception):
    """The probe's config is invalid, or a pin isn't mapped."""


class ProbeTimeout(ProbeError, AssertionError):
    """A pin didn't reach the expected value in time."""


def resolve_expander_pin(name):
    """The MCP23017 pin number for name: GPA0-GPA7 -> 0-7, GPB0-GPB7 -> 8-15."""
    m = EXPANDER_PIN_RE.match(name) if isinstance(name, str) else None
    if not m:
        raise ValueError(f"expander pin must be GPA0-GPA7 or GPB0-GPB7, got {name!r}")
    bank, pin = m.groups()
    return (8 if bank == "B" else 0) + int(pin)


class Probe:
    def __init__(self, config):
        probe_type = config.get("type")
        if probe_type not in PROBE_TYPES:
            raise ProbeError(f"probe 'type' must be one of {PROBE_TYPES}, "
                             f"got {probe_type!r}")
        try:
            self.address = int(str(config.get("address")), 0)
        except ValueError:
            raise ProbeError(f"probe 'address' must be an I2C address like "
                             f"\"0x20\", got {config.get('address')!r}") from None
        pin_map = config.get("pin_map")
        if not isinstance(pin_map, dict):
            raise ProbeError("probe 'pin_map' must be an object, e.g. "
                             "{\"D14\": \"GPA0\"}")
        # Resolve now, so a typo fails before the test runs
        self.pin_map = {dut: resolve_expander_pin(exp) for dut, exp in pin_map.items()}

        import board
        import busio
        import digitalio
        from adafruit_mcp230xx.mcp23017 import MCP23017

        self._direction_input = digitalio.Direction.INPUT
        self._direction_output = digitalio.Direction.OUTPUT
        self._i2c = busio.I2C(board.SCL, board.SDA)
        self._mcp = MCP23017(self._i2c, address=self.address)
        self._pins = {}
        log.info("probe %s at 0x%02x: %s", probe_type, self.address, pin_map)
        self.release()  # an earlier run may have left pins driving

    def _pin(self, dut_pin):
        if dut_pin not in self._pins:
            if dut_pin not in self.pin_map:
                raise ProbeError(f"DUT pin {dut_pin!r} isn't in the probe's "
                                 f"pin_map {sorted(self.pin_map)}")
            pin = self._mcp.get_pin(self.pin_map[dut_pin])
            pin.direction = self._direction_input
            pin.pull = None
            self._pins[dut_pin] = pin
        return self._pins[dut_pin]

    def drive(self, dut_pin, value):
        """Drive the DUT pin to value. Sets the output latch before making
        the expander pin an output, so it doesn't glitch to the old latch."""
        pin = self._pin(dut_pin)
        pin.value = value
        pin.direction = self._direction_output
        log.info("probe driving %s %d", dut_pin, value)

    def release(self, dut_pin=None):
        """Stop driving the DUT pin: make the expander pin an input with no
        pull. With no pin, do that to all 16 expander pins."""
        if dut_pin is not None:
            pins = [self._pin(dut_pin)]
        else:
            pins = [self._mcp.get_pin(n) for n in range(EXPANDER_PINS)]
        for pin in pins:
            pin.direction = self._direction_input
            pin.pull = None
        if dut_pin is None:
            log.info("released all probe pins")

    def value(self, dut_pin):
        """The DUT pin's level right now."""
        return bool(self._pin(dut_pin).value)

    @contextlib.contextmanager
    def expect_digitalout_transition(self, dut_pin, from_value, to_value, *,
                                     frequency=200, timeout=2):
        """Check dut_pin reads from_value now, sample it at frequency Hz while
        the block runs (the block sends the command), then wait until it
        reads to_value, at most timeout seconds after entering."""
        start = time.monotonic()
        initial = self.value(dut_pin)
        assert initial == from_value, (
            f"{dut_pin} should start at {int(from_value)} but reads {int(initial)}")

        samples = [(0.0, initial)]
        reached = threading.Event()
        stop = threading.Event()

        def sample():
            interval = 1 / frequency
            while not stop.is_set():
                value = self.value(dut_pin)
                samples.append((time.monotonic() - start, value))
                if value == to_value:
                    reached.set()
                    return
                stop.wait(interval)

        sampler = threading.Thread(target=sample, name=f"probe-{dut_pin}", daemon=True)
        sampler.start()
        try:
            yield samples
            remaining = timeout - (time.monotonic() - start)
            if not reached.wait(max(remaining, 0)):
                trace = ", ".join(f"{t:.3f}s={int(v)}" for t, v in samples[-TRACE_SAMPLES:])
                raise ProbeTimeout(
                    f"{dut_pin} didn't go {int(from_value)} -> {int(to_value)} "
                    f"within {timeout}s: last read {int(samples[-1][1])} "
                    f"({len(samples)} samples; last: {trace})")
            log.info("%s went %d -> %d at %.3fs", dut_pin, from_value, to_value,
                     samples[-1][0])
        finally:
            stop.set()
            sampler.join()

    def close(self):
        """Release all 16 expander pins, so it drives nothing into the DUT
        after the test, then free the I2C bus."""
        try:
            self.release()
        finally:
            self._i2c.deinit()
