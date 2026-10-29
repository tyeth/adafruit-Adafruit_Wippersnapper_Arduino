"""Read and drive the DUT's pins from the HIL host, through an I2C GPIO
expander wired to them, for HIL tests that check what the DUT drives on a
pin, or what it reads from one.

The board entry's probes describe the wiring, one per role:

    "probes": {
      "digital_gpio": {"type": "mcp23017_gpio", "address": "0x20",
                       "pin_map": {"D14": "GPA0", "D32": "GPA1"}},
      "pwm_input": {"type": "pi_gpio", "pin_map": {"D27": "D16"}}
    }

digital_gpio is a Probe: pin_map maps a DUT pin name to the expander pin
it's wired to, GPA0-GPA7 or GPB0-GPB7.

    probe = Probe(entry["probes"]["digital_gpio"])
    probe.value("D14")                       # read it once, now
    with probe.expect_digitalout_transition("D14", False, True, timeout=2):
        pmq.publish(uid, {"digital": {"write": ...}})   # make it go high
    probe.drive("D32", True)                 # drive the DUT's input high
    probe.release("D32")                     # back to an input, not driving
    probe.release()                          # every expander pin, mapped or not

The MCP23017 keeps driving its outputs when the DUT loses power, which can
damage both, so all 16 expander pins are made inputs with no pull when a
Probe is made, by release() with no pin, and by close().

pwm_input is a PulseInProbe: it times the DUT's PWM output on a host GPIO
wired to the DUT pin, with pulseio.PulseIn (an I2C expander can't time
edges). pin_map maps a DUT pin name to the host's Blinka board pin name.

    pulse_probe = PulseInProbe(entry["probes"]["pwm_input"])
    duty, period_us = pulse_probe.measure("D27", periods=100)

Blinka and adafruit_mcp230xx are imported when a Probe or PulseInProbe is
made, so this module imports on hosts without them.
"""

import contextlib
import logging
import re
import statistics
import threading
import time

# The probe roles a board entry's probes may have, and the hardware types
# each supports.
PROBE_TYPES = {"digital_gpio": ("mcp23017_gpio",), "pwm_input": ("pi_gpio",)}
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


def _check_type(role, config):
    """The config's type, if role supports it."""
    probe_type = config.get("type")
    if probe_type not in PROBE_TYPES[role]:
        raise ProbeError(f"{role} probe 'type' must be one of "
                         f"{PROBE_TYPES[role]}, got {probe_type!r}")
    return probe_type


class Probe:
    def __init__(self, config):
        probe_type = _check_type("digital_gpio", config)
        try:
            self.address = int(str(config.get("address")), 0)
        except ValueError:
            raise ProbeError(f"digital_gpio probe 'address' must be an I2C "
                             f"address like \"0x20\", got "
                             f"{config.get('address')!r}") from None
        pin_map = config.get("pin_map")
        if not isinstance(pin_map, dict):
            raise ProbeError("digital_gpio probe 'pin_map' must be an object, "
                             "e.g. {\"D14\": \"GPA0\"}")
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


def missed_edges(lengths, period_us):
    """How many edges a capture of pulse lengths (us) of a period_us signal
    missed. Every recorded pulse ends on an edge, so the time they cover
    holds 2 pulses per period; any fewer recorded were merged by a missed
    edge."""
    return max(round(2 * sum(lengths) / period_us) - len(lengths), 0)


class PulseInProbe:
    """Time the DUT's PWM output on host GPIOs with pulseio.PulseIn. The
    host pins are only ever inputs, so they never drive the DUT."""

    # Extra PulseIn capacity over what capture() needs, so pulses that
    # arrive between its last len() and pause() don't drop the oldest and
    # lose track of which pulses are high. 10ms of a 100 kHz signal.
    SLACK = 2048
    # Times measure() recaptures after one misses edges. The OS rarely
    # stalls the capture helper for a few ms (AO-00 saw it in 1 of ~1,400
    # captures), and no edge was missed at 5 kHz otherwise.
    STALL_RETRIES = 2

    def __init__(self, config):
        _check_type("pwm_input", config)
        pin_map = config.get("pin_map")
        if not isinstance(pin_map, dict):
            raise ProbeError("pwm_input probe 'pin_map' must be an object, "
                             "e.g. {\"D27\": \"D16\"}")

        import board
        import pulseio

        self._pulseio = pulseio
        self.pin_map = {}
        for dut, host in pin_map.items():
            if not hasattr(board, host):
                raise ProbeError(f"pwm_input probe pin {host!r} for {dut!r} "
                                 f"isn't a board pin")
            self.pin_map[dut] = getattr(board, host)
        self._pulses = {}
        log.info("pulse probe: %s", pin_map)

    def _pulse_in(self, dut_pin, maxlen):
        pulses = self._pulses.get(dut_pin)
        if pulses is None or pulses.maxlen < maxlen:
            if dut_pin not in self.pin_map:
                raise ProbeError(f"DUT pin {dut_pin!r} isn't in the pulse "
                                 f"probe's pin_map {sorted(self.pin_map)}")
            if pulses is not None:
                pulses.deinit()
            pulses = self._pulseio.PulseIn(self.pin_map[dut_pin],
                                           maxlen=maxlen, idle_state=False)
            self._pulses[dut_pin] = pulses
        return pulses

    def capture(self, dut_pin, count, timeout=2):
        """Capture count pulse lengths of dut_pin, an active high signal, in
        us. The first is a high pulse, so even indexes are high pulses and
        odd ones low, as long as no edge was missed."""
        needed = count + 2  # the first pair may be partial
        pulses = self._pulse_in(dut_pin, needed + self.SLACK)
        pulses.pause()
        pulses.clear()
        # After resume() the first pulse recorded is the active (high) one
        pulses.resume()
        start = time.monotonic()
        while len(pulses) < needed:
            if time.monotonic() - start > timeout:
                got = len(pulses)
                pulses.pause()
                raise ProbeTimeout(
                    f"{dut_pin}: only {got} of {needed} pulses within "
                    f"{timeout}s, is it toggling?")
            time.sleep(0.01)
        pulses.pause()
        got = len(pulses)
        if got >= pulses.maxlen:
            raise ProbeError(f"{dut_pin}: PulseIn filled ({got}), which "
                             f"pulses are high is unknown")
        return [pulses[i] for i in range(2, needed)]

    def measure(self, dut_pin, periods=100, timeout=2):
        """Capture periods full high+low periods of dut_pin, an active high
        PWM output, and return its duty (0-1) and mean period in us.
        Recaptures up to STALL_RETRIES times if a capture misses edges."""
        for attempt in range(self.STALL_RETRIES + 1):
            lengths = self.capture(dut_pin, 2 * periods, timeout)
            pairs = [h + l for h, l in zip(lengths[0::2], lengths[1::2])]
            # The median period ignores the few pairs a miss merges, so
            # this doesn't assume the DUT's frequency is right
            missed = missed_edges(lengths, statistics.median(pairs))
            if not missed:
                break
            log.warning("%s capture %d missed %d edges%s", dut_pin,
                        attempt + 1, missed,
                        ", retrying" if attempt < self.STALL_RETRIES else "")
        else:
            raise ProbeError(f"{dut_pin}: all {self.STALL_RETRIES + 1} "
                             f"captures missed edges, last {missed}")
        high, low = lengths[0::2], lengths[1::2]
        period_lengths = [h + l for h, l in zip(high, low)]
        duty = sum(high) / sum(period_lengths)
        period_us = statistics.mean(period_lengths)
        log.info("%s duty %.2f%%, period %.1fus (min %d, max %d) over %d "
                 "periods", dut_pin, duty * 100, period_us,
                 min(period_lengths), max(period_lengths), periods)
        return duty, period_us

    def count_pulses(self, dut_pin, window):
        """How many pulses dut_pin had over window seconds: 0 if it held
        one level throughout."""
        pulses = self._pulse_in(dut_pin, 2 + self.SLACK)
        pulses.pause()
        pulses.clear()
        pulses.resume()
        time.sleep(window)
        pulses.pause()
        return len(pulses)

    def start_record(self, dut_pin, max_pulses):
        """Start recording dut_pin's pulses, at most max_pulses of them,
        and return the time.time() the recording started, the clock
        SerialMonitor stamps lines with. The first pulse recorded is the
        first high one starting after that, within one period."""
        pulses = self._pulse_in(dut_pin, max_pulses + self.SLACK)
        pulses.pause()
        pulses.clear()
        pulses.resume()
        return time.time()

    def stop_record(self, dut_pin):
        """Stop the recording start_record() began and return its pulse
        lengths, in us, high first."""
        pulses = self._pulses[dut_pin]
        # Blinka's PulseIn blocks forever on a reply from a dead helper
        if pulses._process is None or pulses._process.poll() is not None:
            raise ProbeError(f"{dut_pin}: PulseIn's helper process exited "
                             f"during the recording")
        pulses.pause()
        count = len(pulses)
        if count >= pulses.maxlen:
            raise ProbeError(f"{dut_pin}: recording filled PulseIn ({count}), "
                             f"its start is lost")
        start = time.monotonic()
        lengths = [pulses[i] for i in range(count)]
        log.info("%s recorded %d pulses, read in %.2fs", dut_pin, count,
                 time.monotonic() - start)
        return lengths

    def level(self, dut_pin):
        """dut_pin's level now, read with the host pin's pull-down on, so
        an undriven DUT pin reads low. Stops any PulseIn on the pin."""
        if dut_pin not in self.pin_map:
            raise ProbeError(f"DUT pin {dut_pin!r} isn't in the pulse "
                             f"probe's pin_map {sorted(self.pin_map)}")
        pulses = self._pulses.pop(dut_pin, None)
        if pulses is not None:
            pulses.deinit()
        # lgpio, not digitalio: Blinka's deinit() keeps the line claimed,
        # so the next PulseIn couldn't take it
        import lgpio
        chip = lgpio.gpiochip_open(0)
        line = self.pin_map[dut_pin].id
        try:
            lgpio.gpio_claim_input(chip, line, lgpio.SET_PULL_DOWN)
            try:
                time.sleep(0.01)
                return bool(lgpio.gpio_read(chip, line))
            finally:
                lgpio.gpio_free(chip, line)
        finally:
            lgpio.gpiochip_close(chip)

    def close(self):
        """Stop every PulseIn and its helper process."""
        for pulses in self._pulses.values():
            pulses.deinit()
        self._pulses.clear()
