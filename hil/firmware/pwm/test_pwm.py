"""
HIL tests for Adafruit Wippersnapper firmware's pwm component: what the DUT
does with the broker's pwm.B2D messages.

Exercises:
- `src/components/pwm/controller.cpp::Router()`
- `src/components/pwm/controller.cpp::Handle_PWM_Add()`
- `src/components/pwm/controller.cpp::Handle_PWM_Write()`
- `src/components/pwm/hardware.cpp::attach()`
- `src/components/pwm/hardware.cpp::writeDutyCycle()`
- `src/components/pwm/hardware.cpp::analogWrite()` (ESP32's ledcWrite())
- `src/components/pwm/controller.cpp::Handle_PWM_Remove()`
- `src/components/pwm/hardware.cpp::detach()`
"""

import logging
import re
import time

import pytest


log = logging.getLogger("hil.pwm")

# How long after checkin the DUT may take to handle a message, as the
# checkin fixture allows for the checkin itself.
CHECKIN_TIMEOUT = 30
# The broker's default fixed PWM frequency, in Hz, and resolution, in bits.
DEFAULT_FREQUENCY = 5000
DEFAULT_RESOLUTION = 12
# How long to watch for the DUT to report an error after it handles a
# message.
ERROR_WINDOW = 3
# How many PWM periods the pulse probe measures the duty over.
DUTY_PERIODS = 100
# How far the pulse probe's measured duty may be off. AO-00 (2026-10-28)
# measured its duty error at 5 kHz over 100 periods: bias under 0.02%,
# stdev 0.02-0.05%, worst 0.237% in 240 captures.
PROBE_DUTY_ERROR = 0.003
# How far the measured period may be from the frequency's. AO-00 measured
# the pulse probe's period error at 5 kHz under 0.05us (0.025%); the
# ESP32's LEDC divides 5 kHz at 12 bits exactly, leaving its crystal.
PERIOD_TOLERANCE = 0.005
# Fractions of full scale the LEDC duty scan writes, clear of 0% and 100%
# so the shortest pulse (20us at 5 kHz) stays well within the probe's
# reach.
SCAN_DUTIES = (0.10, 0.25, 0.50, 0.75, 0.90)
# How long a pin must show no edge to count as stopped.
QUIET_WINDOW = 0.5

# AO-05: Handle_PWM_Write() logs each write on one line (pwm.Write's
# duty_cycle is payload tag 2).
WRITE_LOG = (r"\[pwm\] Wrote to pin: {pin}\[pwm\] Message type: 2"
             r"\[pwm\] Payload value: {value}\b")
# How far the first period at a new duty may be from the write's log line,
# either way: the DUT may log before or after ledcWrite(), and the line
# reaches the host over USB serial. Provisional until measured on a
# known-good build.
WRITE_LATENCY_BOUND = 0.050
# How long to keep recording after the write's log line, and the most
# pulses a recording holds (5 kHz, 2 a period, for 3s).
RECORD_AFTER_WRITE = 0.3
RECORD_MAX_PULSES = 30000
# How far a single period's duty and length may be off and still count as
# at the old or new duty: PulseIn rounds each pulse to 1us, 0.5% of 200us.
PERIOD_DUTY_TOLERANCE = 0.03
PERIOD_LENGTH_TOLERANCE = 0.05
# Periods between the last at the old duty and the first at the new one
# that may be at neither: LEDC applies a new duty at a period boundary, so
# none, plus one for PulseIn's 1us rounding at the boundary.
MAX_TRANSITION_PERIODS = 1

# AO-11: a free board pin to move the PWM output to; it isn't wired to
# the pulse probe, so only the old pin going quiet is measured.
MODIFY_NEW_PIN = "D26"

# AO-14: a 12-bit duty above the 2 ** 12 maximum, which ledcWrite() clamps
# to fully on, and a frequency no 12-bit LEDC timer reaches (80 MHz / 2 **
# 12 is about 19.5 kHz).
DUTY_ABOVE_MAX = 5000
FREQUENCY_TOO_HIGH = 1_000_000
# Lines the DUT prints when it crashes or reboots.
CRASH_RE = re.compile(r"Guru Meditation|Backtrace:|rst:0x|ESP Reset Reason")


@pytest.fixture
def pwm_pin(pulse_probe):
    """The DUT's PWM output pin under test: the first pin in the board
    entry's pwm_input probe's pin_map, which the pulse probe measures."""
    return next(iter(pulse_probe.pin_map))


def _add(serial, protomq, uid, pin, frequency=DEFAULT_FREQUENCY,
         resolution=DEFAULT_RESOLUTION):
    """Send a pwm.Add for pin at frequency and resolution, leaving the
    inversion and initial write empty, and expect the DUT to attach it."""
    add = {"pin": pin, "frequency": frequency, "resolution": resolution}
    protomq.publish(uid, {"pwm": {"add": add}})
    protomq.expect_b2d_message(uid, "pwm.add", match=add, timeout=5)

    # Handle_PWM_Add() logs the pin without a newline, so the next log
    # text may follow it on the same line
    serial.expect(rf"^\[pwm\] Attached pin: {pin}", timeout=CHECKIN_TIMEOUT)
    # It logs that even when attach() fails, publishing a component error
    # instead, so the absence of one is what shows the pin was attached
    protomq.assert_no_d2b_message(uid, "error.component", match={"pin": pin},
                                  within=ERROR_WINDOW)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pwm_add(board, board_definition, secrets_name, flashed_board,
                 serial, power, device_uid, protomq, pwm_pin, checkin):
    # --- Arrange ------------------------------------------------------
    # DUT checks in
    uid = checkin()

    # --- Act / Assert -------------------------------------------------
    # The broker adds the PWM pin at 5 kHz, 12-bit and the DUT attaches it
    _add(serial, protomq, uid, pwm_pin)


def _write_duty(protomq, uid, pin, duty_cycle):
    """Send a pwm.Write of duty_cycle to pin and expect the DUT to report
    no error for it."""
    write = {"pin": pin, "dutyCycle": duty_cycle}
    protomq.publish(uid, {"pwm": {"write": write}})
    protomq.expect_b2d_message(uid, "pwm.write", match=write, timeout=5)
    # Handle_PWM_Write() logs nothing on success, but publishes a component
    # error on failure. This also gives the DUT time to apply the write.
    protomq.assert_no_d2b_message(uid, "error.component", match={"pin": pin},
                                  within=ERROR_WINDOW)


def _assert_pwm(pulse_probe, pin, duty_cycle, resolution, frequency):
    """Measure pin over DUTY_PERIODS periods and check it runs at
    frequency with duty_cycle's duty: pwm.proto's duty_cycle counts from
    0 to 2 ** resolution, so the duty may be off by 1 LSB of that plus the
    probe's error."""
    duty, period_us = pulse_probe.measure(pin, periods=DUTY_PERIODS)

    full_scale = 2 ** resolution
    expected = duty_cycle / full_scale
    tolerance = 1 / full_scale + PROBE_DUTY_ERROR
    assert abs(duty - expected) <= tolerance, (
        f"{pin} dutyCycle {duty_cycle}/{full_scale}: measured {duty:.3%}, "
        f"expected {expected:.3%} +/- {tolerance:.3%} "
        f"(period {period_us:.2f}us)")
    expected_period = 1e6 / frequency
    assert abs(period_us - expected_period) <= (
        PERIOD_TOLERANCE * expected_period), (
        f"{pin} dutyCycle {duty_cycle}/{full_scale}: measured period "
        f"{period_us:.2f}us, expected {expected_period:.0f}us "
        f"+/- {PERIOD_TOLERANCE:.1%}")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pwm_duty_cycle_sweep(board, board_definition, secrets_name,
                              flashed_board, serial, power, device_uid,
                              protomq, pulse_probe, pwm_pin, checkin):
    # --- Arrange ------------------------------------------------------
    # DUT checks in and adds the PWM pin at 5 kHz, 12-bit
    uid = checkin()
    _add(serial, protomq, uid, pwm_pin)

    # --- Act / Assert -------------------------------------------------
    # The broker sweeps the PWM pin through 25%, 50% and 75% duty, and the
    # pulse probe measures each over DUTY_PERIODS periods
    for duty_cycle in (1024, 2048, 3072):
        _write_duty(protomq, uid, pwm_pin, duty_cycle)
        _assert_pwm(pulse_probe, pwm_pin, duty_cycle, DEFAULT_RESOLUTION,
                    DEFAULT_FREQUENCY)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
@pytest.mark.parametrize("resolution", [10, 12])
def test_pwm_ledc_duty_scan(board, board_definition, secrets_name,
                            flashed_board, serial, power, device_uid,
                            protomq, pulse_probe, pwm_pin, checkin,
                            resolution):
    # --- Arrange ------------------------------------------------------
    # DUT checks in and adds the PWM pin at 5 kHz and resolution, which
    # ledcAttach() sets the LEDC timer to: 2 ** resolution steps a period
    uid = checkin()
    _add(serial, protomq, uid, pwm_pin, resolution=resolution)

    # --- Act / Assert -------------------------------------------------
    # The broker scans the duty across SCAN_DUTIES as counts at
    # resolution, e.g. 512/1024 or 2048/4096 for 50%, which must reach
    # ledcWrite() unscaled for the pulse probe to measure that duty
    for fraction in SCAN_DUTIES:
        duty_cycle = round(fraction * 2 ** resolution)
        _write_duty(protomq, uid, pwm_pin, duty_cycle)
        _assert_pwm(pulse_probe, pwm_pin, duty_cycle, resolution,
                    DEFAULT_FREQUENCY)


def _remove(serial, protomq, uid, pin):
    """Send a pwm.Remove for pin and expect the DUT to remove it."""
    protomq.publish(uid, {"pwm": {"remove": {"pin": pin}}})
    # Handle_PWM_Remove() logs the pin without a newline
    serial.expect(rf"^\[pwm\] Removed pin: {pin}", timeout=CHECKIN_TIMEOUT)
    protomq.assert_no_d2b_message(uid, "error.component", match={"pin": pin},
                                  within=ERROR_WINDOW)


def _assert_quiet(pulse_probe, pin, level):
    """Check pin shows no edge for QUIET_WINDOW and reads level."""
    count = pulse_probe.count_pulses(pin, QUIET_WINDOW)
    assert count == 0, (
        f"{pin} should be still but had {count} pulses in {QUIET_WINDOW}s")
    assert pulse_probe.level(pin) == level, (
        f"{pin} should read {'HIGH' if level else 'LOW'}")


def _periods(lengths):
    """(start in us from the first pulse, duty, length in us) of each full
    high+low period in a recording, dropping the first, maybe partial,
    pair."""
    periods, start = [], lengths[0] + lengths[1]
    for high, low in zip(lengths[2::2], lengths[3::2]):
        periods.append((start, high / (high + low), high + low))
        start += high + low
    return periods


def _at(period, duty, length):
    """Whether period is at duty and length, within one period's
    tolerance."""
    _, measured, measured_length = period
    return (abs(measured - duty) <= PERIOD_DUTY_TOLERANCE
            and abs(measured_length - length)
            <= PERIOD_LENGTH_TOLERANCE * length)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_ao05_duty_update_timing(board, board_definition, secrets_name,
                                 flashed_board, serial, power, device_uid,
                                 protomq, pulse_probe, pwm_pin, checkin,
                                 record_property):
    """AO-05: a duty change shows up within WRITE_LATENCY_BOUND of the
    write's log line, with no stuck or garbled periods in between."""
    # --- Arrange ------------------------------------------------------
    # The PWM pin runs at 25%, 12-bit, 5 kHz
    old, new = 1024, 3072
    full_scale = 2 ** DEFAULT_RESOLUTION
    period_us = 1e6 / DEFAULT_FREQUENCY
    uid = checkin()
    _add(serial, protomq, uid, pwm_pin)
    _write_duty(protomq, uid, pwm_pin, old)
    _assert_pwm(pulse_probe, pwm_pin, old, DEFAULT_RESOLUTION,
                DEFAULT_FREQUENCY)

    # --- Act ----------------------------------------------------------
    # The pulse probe records the pin while the broker changes it to 75%,
    # until RECORD_AFTER_WRITE after the DUT logs the write
    since = serial.mark()
    started = pulse_probe.start_record(pwm_pin, RECORD_MAX_PULSES)
    write = {"pin": pwm_pin, "dutyCycle": new}
    protomq.publish(uid, {"pwm": {"write": write}})
    m = serial.expect(WRITE_LOG.format(pin=pwm_pin, value=new), timeout=5)
    written = next(l.t for l in serial.lines(since) if l.text == m.string)
    time.sleep(RECORD_AFTER_WRITE)
    lengths = pulse_probe.stop_record(pwm_pin)
    protomq.assert_no_d2b_message(uid, "error.component",
                                  match={"pin": pwm_pin}, within=0.1)

    # --- Assert -------------------------------------------------------
    # Every period is at the old duty, then at most MAX_TRANSITION_PERIODS
    # at neither, then every one at the new duty
    periods = _periods(lengths)
    is_old = [_at(p, old / full_scale, period_us) for p in periods]
    is_new = [_at(p, new / full_scale, period_us) for p in periods]
    assert is_old and is_old[0], (
        f"{pwm_pin} wasn't at {old}/{full_scale} when the recording began")
    assert is_new[-1], (
        f"{pwm_pin} isn't at {new}/{full_scale} {RECORD_AFTER_WRITE}s after "
        f"the write was logged: last period {periods[-1]}")
    last_old = max(i for i, o in enumerate(is_old) if o)
    first_new = min(i for i, n in enumerate(is_new) if n)
    stray_old = [i for i in range(last_old) if not is_old[i]]
    stray_new = [i for i in range(first_new, len(periods)) if not is_new[i]]
    assert not stray_old and not stray_new and first_new > last_old, (
        f"{pwm_pin} had garbled periods: before the change "
        f"{[periods[i] for i in stray_old[:5]]}, after it "
        f"{[periods[i] for i in stray_new[:5]]}")
    between = first_new - last_old - 1
    assert between <= MAX_TRANSITION_PERIODS, (
        f"{pwm_pin} had {between} periods at neither duty between "
        f"{old} and {new}: {periods[last_old + 1:first_new][:5]}")

    # The first period at the new duty starts within WRITE_LATENCY_BOUND
    # of the write's log line
    changed = started + periods[first_new][0] / 1e6
    latency = changed - written
    record_property("ao05_latency_ms", round(latency * 1000, 3))
    log.info("%s duty change %+.2fms from the write's log line, %d periods "
             "between", pwm_pin, latency * 1000, between)
    assert abs(latency) <= WRITE_LATENCY_BOUND, (
        f"{pwm_pin} reached {new}/{full_scale} {latency * 1000:+.1f}ms from "
        f"the write's log line, over +/-{WRITE_LATENCY_BOUND * 1000:.0f}ms")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_ao09_remove(board, board_definition, secrets_name, flashed_board,
                     serial, power, device_uid, protomq, pulse_probe, pwm_pin,
                     checkin):
    """AO-09: a removed pin stops toggling and is left LOW."""
    # --- Arrange ------------------------------------------------------
    # The PWM pin runs at 50%, 12-bit, 5 kHz
    uid = checkin()
    _add(serial, protomq, uid, pwm_pin)
    _write_duty(protomq, uid, pwm_pin, 2048)
    _assert_pwm(pulse_probe, pwm_pin, 2048, DEFAULT_RESOLUTION,
                DEFAULT_FREQUENCY)

    # --- Act ----------------------------------------------------------
    _remove(serial, protomq, uid, pwm_pin)

    # --- Assert -------------------------------------------------------
    # No edges afterward, and the line reads LOW with the probe's
    # pull-down
    _assert_quiet(pulse_probe, pwm_pin, level=False)


def _modify(serial, protomq, uid, old_pin, pin, frequency, duty_cycle):
    """Send a pwm.Modify removing old_pin and adding pin at frequency,
    12-bit, with an initial write of duty_cycle, and expect the DUT to do
    both with no error."""
    add = {"pin": pin, "frequency": frequency,
           "resolution": DEFAULT_RESOLUTION,
           "write": {"pin": pin, "dutyCycle": duty_cycle}}
    modify = {"remove": {"pin": old_pin}, "add": add}
    protomq.publish(uid, {"pwm": {"modify": modify}})
    protomq.expect_b2d_message(uid, "pwm.modify", match=modify, timeout=5)
    # The remove and add each log their pin without a newline, so they
    # share a line
    serial.expect(rf"\[pwm\] Removed pin: {old_pin}\[pwm\] Attached pin: "
                  rf"{pin}", timeout=CHECKIN_TIMEOUT)
    for p in {old_pin, pin}:
        protomq.assert_no_d2b_message(uid, "error.component", match={"pin": p},
                                      within=ERROR_WINDOW)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_ao11_modify(board, board_definition, secrets_name, flashed_board,
                     serial, power, device_uid, protomq, pulse_probe,
                     pwm_pin, checkin):
    """AO-11: a pwm.Modify's new duty, frequency and pin take effect, and
    a pin change stops the old pin."""
    # --- Arrange ------------------------------------------------------
    # The PWM pin runs at 25%, 12-bit, 5 kHz
    uid = checkin()
    _add(serial, protomq, uid, pwm_pin)
    _write_duty(protomq, uid, pwm_pin, 1024)
    _assert_pwm(pulse_probe, pwm_pin, 1024, DEFAULT_RESOLUTION,
                DEFAULT_FREQUENCY)

    # --- Act / Assert -------------------------------------------------
    # Duty: Modify to 75%, given as the Add's initial write
    _modify(serial, protomq, uid, pwm_pin, pwm_pin, DEFAULT_FREQUENCY, 3072)
    _assert_pwm(pulse_probe, pwm_pin, 3072, DEFAULT_RESOLUTION,
                DEFAULT_FREQUENCY)

    # Frequency: Modify to 2 kHz at 50%
    _modify(serial, protomq, uid, pwm_pin, pwm_pin, 2000, 2048)
    _assert_pwm(pulse_probe, pwm_pin, 2048, DEFAULT_RESOLUTION, 2000)

    # Pin: Modify to MODIFY_NEW_PIN, so the old pin stops toggling and is
    # left LOW
    _modify(serial, protomq, uid, pwm_pin, MODIFY_NEW_PIN, 2000, 2048)
    _assert_quiet(pulse_probe, pwm_pin, level=False)


def _expect_add_rejected(serial, protomq, uid, pin, frequency):
    """Send a pwm.Add for pin at frequency, 12-bit, and expect the DUT to
    reject it: attach() fails and it publishes a component error."""
    add = {"pin": pin, "frequency": frequency,
           "resolution": DEFAULT_RESOLUTION}
    protomq.publish(uid, {"pwm": {"add": add}})
    protomq.expect_d2b_message(
        uid, "error.component",
        match={"pin": pin, "message": "Failed to attach pin"},
        timeout=CHECKIN_TIMEOUT)


def _assert_valid_config(serial, protomq, uid, pulse_probe, pin):
    """Add pin at 5 kHz, 12-bit, write 50% and measure it, showing the DUT
    still takes a valid configuration."""
    _add(serial, protomq, uid, pin)
    _write_duty(protomq, uid, pin, 2048)
    _assert_pwm(pulse_probe, pin, 2048, DEFAULT_RESOLUTION, DEFAULT_FREQUENCY)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_ao14_invalid_values(board, board_definition, secrets_name,
                             flashed_board, serial, power, device_uid,
                             protomq, pulse_probe, pwm_pin, checkin):
    """AO-14: a duty above maximum is clamped to fully on, an Add with no
    frequency attaches the pin silent, a frequency beyond the hardware is
    rejected, the DUT doesn't crash, and the next valid configuration
    works after each."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    since = serial.mark()
    _add(serial, protomq, uid, pwm_pin)

    # --- Act / Assert -------------------------------------------------
    # Duty above 2 ** 12: ledcWrite() clamps it to fully on, so the pin
    # holds HIGH with no error
    _write_duty(protomq, uid, pwm_pin, DUTY_ABOVE_MAX)
    _assert_quiet(pulse_probe, pwm_pin, level=True)
    _write_duty(protomq, uid, pwm_pin, 2048)
    _assert_pwm(pulse_probe, pwm_pin, 2048, DEFAULT_RESOLUTION,
                DEFAULT_FREQUENCY)

    # ISSUE: fails on ESP32 until the firmware handles frequency 0. The
    # broker leaves out frequency (null, so 0) on a variable-frequency
    # (piezo) Add, meaning "attached, silent until a tone is written".
    # Handle_PWM_Add() passes it to ledcAttach(), whose
    # ledcAttachChannel() rejects freq == 0 ("frequency can't be zero"),
    # so the DUT publishes "Failed to attach pin" and later Writes find no
    # pin. Fix: attach at a non-zero frequency with duty 0 when frequency
    # is 0; ledcWriteTone() then sets each note, and 0 silences it again.
    # Frequency 0: the broker's piezo Add, 10-bit and no frequency. The
    # DUT attaches the pin with no error and it stays silent and LOW
    add = {"pin": pwm_pin, "resolution": 10}
    protomq.publish(uid, {"pwm": {"add": add}})
    protomq.expect_b2d_message(uid, "pwm.add", match=add, timeout=5)
    serial.expect(rf"^\[pwm\] Attached pin: {pwm_pin}",
                  timeout=CHECKIN_TIMEOUT)
    protomq.assert_no_d2b_message(uid, "error.component",
                                  match={"pin": pwm_pin}, within=ERROR_WINDOW)
    _assert_quiet(pulse_probe, pwm_pin, level=False)
    _assert_valid_config(serial, protomq, uid, pulse_probe, pwm_pin)

    # Frequency beyond what a 12-bit LEDC timer reaches: rejected
    _expect_add_rejected(serial, protomq, uid, pwm_pin, FREQUENCY_TOO_HIGH)
    _assert_valid_config(serial, protomq, uid, pulse_probe, pwm_pin)

    # The DUT didn't crash or reboot throughout
    serial.assert_none(CRASH_RE, since=since)
