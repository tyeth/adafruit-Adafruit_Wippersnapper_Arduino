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
"""

import pytest


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
# Fractions of full scale the LEDC duty scan writes, clear of 0% and 100%
# so the shortest pulse (20us at 5 kHz) stays well within the probe's
# reach.
SCAN_DUTIES = (0.10, 0.25, 0.50, 0.75, 0.90)
# How far the measured period may be from the frequency's. AO-00 measured
# the pulse probe's period error at 5 kHz under 0.05us (0.025%); the
# ESP32's LEDC divides 5 kHz at 12 bits exactly, leaving its crystal.
PERIOD_TOLERANCE = 0.005


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
