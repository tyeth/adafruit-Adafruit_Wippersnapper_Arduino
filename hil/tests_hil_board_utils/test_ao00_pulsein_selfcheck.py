"""AO-00: rig self-check of the pulse probe (pulseio.PulseIn on GPIO16).

Drives GPIO16 from one of the Pi's own hardware PWM channels, which runs
from its crystal, so any error PulseIn reports comes from capture alone.
At each frequency, it measures:

- missed edges: pulses the capture should have seen over the time it
  covered (2 per PWM period), minus the pulses it recorded
- duty and period error after averaging over PERIODS periods: the bias
  (mean) and spread (max |error|) across RUNS captures

The highest frequency up to which every capture missed no edges and read
no duty far off is the probe's ceiling.

Wiring: GPIO12 (header pin 32) -> 1 kOhm -> GPIO16 (header pin 36). The
DUT's D27 may stay on GPIO16 as long as it isn't driving it; the
resistor limits the current if it is. GPIO12 needs the RP1 PWM0
overlay, in /boot/firmware/config.txt, then a reboot:

    dtoverlay=pwm,pin=12,func=4

Run from hil/:

    venv/bin/python -m pytest tests_hil_board_utils/test_ao00_pulsein_selfcheck.py \\
        --log-cli-level=INFO
"""

import json
import logging
import os
import statistics
import time
from pathlib import Path

import pytest

import hil_probe

log = logging.getLogger("hil.ao00")

# The pulse probe's host pin, and the hardware PWM GPIO wired to it.
PULSE_PIN = "D16"
PWM_GPIO = 12
# RP1 PWM0's sysfs device, and its channel for each PWM GPIO.
RP1_PWM0 = "1f00098000.pwm"
RP1_PWM0_CHANNELS = {12: 0, 13: 1, 18: 2, 19: 3}

# Frequencies (Hz) and duties to capture at. 5 kHz is the broker's fixed
# PWM frequency, which the pwm/ tests use.
FREQUENCIES = (1000, 2000, 5000, 10000, 20000, 50000)
DUTIES = (0.25, 0.5, 0.75)
PLANNED_FREQUENCY = 5000
# Captures per frequency and duty, and PWM periods per capture, as many as
# the pwm/ tests average over.
RUNS = 80
PERIODS = 100
# How long one capture may take, and how long the PWM gets to settle after
# a change.
CAPTURE_TIMEOUT = 2
SETTLE = 0.05
# Failed captures after which a frequency and duty stops early.
MAX_FAILED_CAPTURES = 3
# A duty error this large means the capture mixed up high and low pulses,
# e.g. 25% read as 75%, which the edge count can miss.
MAX_CLEAN_DUTY_ERROR = 0.01


class HostPWM:
    """One channel of the Pi's hardware PWM, through sysfs."""

    def __init__(self, gpio):
        if gpio not in RP1_PWM0_CHANNELS:
            raise ValueError(f"GPIO{gpio} has no RP1 PWM0 channel, use one "
                             f"of {sorted(RP1_PWM0_CHANNELS)}")
        self.gpio = gpio
        self.chip = self._find_chip()
        self.channel = RP1_PWM0_CHANNELS[gpio]
        self.path = self.chip / f"pwm{self.channel}"
        if not self.path.exists():
            (self.chip / "export").write_text(str(self.channel))
        # udev makes the channel's files writable by the gpio group
        # shortly after the export
        enable = self.path / "enable"
        deadline = time.monotonic() + 2
        while not (enable.exists() and os.access(enable, os.W_OK)):
            if time.monotonic() > deadline:
                raise RuntimeError(f"{enable} isn't writable, is this user "
                                   f"in the gpio group?")
            time.sleep(0.05)
        self.disable()

    @staticmethod
    def _find_chip():
        for chip in sorted(Path("/sys/class/pwm").glob("pwmchip*")):
            if (chip / "device").resolve().name == RP1_PWM0:
                return chip
        pytest.skip(f"no RP1 PWM0 pwmchip: add 'dtoverlay=pwm,pin="
                    f"{PWM_GPIO},func=4' to /boot/firmware/config.txt "
                    f"and reboot")

    def _write(self, name, value):
        (self.path / name).write_text(str(value))

    def set(self, frequency, duty):
        """Output frequency Hz at duty (0-1). Returns the programmed period
        and high time, in ns."""
        period_ns = round(1e9 / frequency)
        duty_ns = round(period_ns * duty)
        # The high time may never exceed the period, old or new
        self._write("duty_cycle", 0)
        self._write("period", period_ns)
        self._write("duty_cycle", duty_ns)
        self._write("enable", 1)
        return period_ns, duty_ns

    def disable(self):
        self._write("enable", 0)

    def close(self):
        try:
            self.disable()
        finally:
            (self.chip / "unexport").write_text(str(self.channel))


@pytest.fixture
def pulse_probe():
    probe = hil_probe.PulseInProbe({"type": "pi_gpio",
                                    "pin_map": {PULSE_PIN: PULSE_PIN}})
    try:
        yield probe
    finally:
        probe.close()


@pytest.fixture
def host_pwm():
    pwm = HostPWM(PWM_GPIO)
    try:
        yield pwm
    finally:
        pwm.close()


def _capture_stats(probe, period_ns, duty_ns):
    """RUNS captures of PERIODS periods: missed edges, and the duty and
    period error of those that missed none."""
    period_us = period_ns / 1000
    true_duty = duty_ns / period_ns
    missed = expected = failed = 0
    duty_errors, period_errors = [], []
    for _ in range(RUNS):
        try:
            lengths = probe.capture(PULSE_PIN, 2 * PERIODS,
                                    timeout=CAPTURE_TIMEOUT)
        except hil_probe.ProbeError as e:
            log.warning("capture failed: %s", e)
            failed += 1
            if failed >= MAX_FAILED_CAPTURES:
                break  # beyond what PulseIn can capture, don't wait out RUNS
            continue
        run_missed = hil_probe.missed_edges(lengths, period_us)
        expected += len(lengths) + run_missed
        missed += run_missed
        if run_missed:
            continue  # high and low pulses are mixed up from the miss on
        high, low = lengths[0::2], lengths[1::2]
        total = sum(high) + sum(low)
        duty_errors.append(sum(high) / total - true_duty)
        period_errors.append(total / PERIODS - period_us)
    return {
        "missed_edges": missed,
        "expected_edges": expected,
        "failed_captures": failed,
        "clean_captures": len(duty_errors),
        "duty_bias": statistics.mean(duty_errors) if duty_errors else None,
        "duty_max_abs": max(map(abs, duty_errors)) if duty_errors else None,
        "duty_stdev": (statistics.stdev(duty_errors)
                       if len(duty_errors) > 1 else None),
        "period_bias_us": (statistics.mean(period_errors)
                           if period_errors else None),
        "period_max_abs_us": (max(map(abs, period_errors))
                              if period_errors else None),
    }


def _pct(value):
    return "-" if value is None else f"{value:+.3%}"


def test_ao00_pulsein_capture_ceiling(pulse_probe, host_pwm, tmp_path,
                                      record_property):
    # --- Arrange ------------------------------------------------------
    # Nothing else may be driving GPIO16, e.g. the DUT left PWMing D27
    with pytest.raises(hil_probe.ProbeTimeout):
        pulse_probe.capture(PULSE_PIN, 4, timeout=0.2)
    # GPIO12's PWM reaches GPIO16, or every capture below would time out
    host_pwm.set(FREQUENCIES[0], 0.5)
    time.sleep(SETTLE)
    try:
        pulse_probe.capture(PULSE_PIN, 4, timeout=0.5)
    except hil_probe.ProbeTimeout:
        pytest.fail(f"no pulses on {PULSE_PIN} from GPIO{PWM_GPIO}'s PWM: "
                    f"wire GPIO{PWM_GPIO} (header pin 32) -> 1 kOhm -> "
                    f"GPIO16 (header pin 36), with nothing else driving "
                    f"GPIO16", pytrace=False)

    # --- Act ----------------------------------------------------------
    results = []
    for frequency in FREQUENCIES:
        for duty in DUTIES:
            period_ns, duty_ns = host_pwm.set(frequency, duty)
            time.sleep(SETTLE)
            stats = _capture_stats(pulse_probe, period_ns, duty_ns)
            stats.update(frequency=frequency, duty=duty)
            results.append(stats)
            log.info("%6d Hz %3.0f%%: missed %d/%d edges, %d failed, duty "
                     "bias %s max %s, period bias %.3f us",
                     frequency, duty * 100, stats["missed_edges"],
                     stats["expected_edges"], stats["failed_captures"],
                     _pct(stats["duty_bias"]), _pct(stats["duty_max_abs"]),
                     stats["period_bias_us"] or 0.0)
    host_pwm.disable()

    # Per frequency, over all its duties
    summary = []
    for frequency in FREQUENCIES:
        rows = [r for r in results if r["frequency"] == frequency]
        maxes = [r["duty_max_abs"] for r in rows
                 if r["duty_max_abs"] is not None]
        summary.append({
            "frequency": frequency,
            "missed_edges": sum(r["missed_edges"] for r in rows),
            "expected_edges": sum(r["expected_edges"] for r in rows),
            "failed_captures": sum(r["failed_captures"] for r in rows),
            "duty_max_abs": max(maxes) if len(maxes) == len(rows) else None,
        })
    ceiling = None
    for s in summary:
        if (s["missed_edges"] or s["failed_captures"]
                or s["duty_max_abs"] is None
                or s["duty_max_abs"] > MAX_CLEAN_DUTY_ERROR):
            break
        ceiling = s["frequency"]

    lines = [f"AO-00 PulseIn on {PULSE_PIN} vs GPIO{PWM_GPIO} hardware PWM, "
             f"{RUNS} x {PERIODS} periods per duty",
             f"{'Hz':>7} {'missed':>13} {'failed':>6} {'max |duty err|':>15}"]
    for s in summary:
        lines.append(f"{s['frequency']:>7} "
                     f"{s['missed_edges']:>6}/{s['expected_edges']:<6} "
                     f"{s['failed_captures']:>6} "
                     f"{_pct(s['duty_max_abs']):>15}")
    lines.append(f"ceiling (no missed edges up to): {ceiling} Hz")
    log.info("\n%s", "\n".join(lines))

    report = tmp_path / "ao00_pulsein_selfcheck.json"
    report.write_text(json.dumps({"ceiling": ceiling, "summary": summary,
                                  "results": results}, indent=2))
    log.info("AO-00 report: %s", report)
    record_property("ao00_ceiling_hz", ceiling)
    for s in summary:
        record_property(f"ao00_{s['frequency']}hz_duty_max_abs",
                        s["duty_max_abs"])

    # --- Assert -------------------------------------------------------
    # The pwm/ tests' frequency is within the probe's ceiling
    assert ceiling is not None and ceiling >= PLANNED_FREQUENCY, (
        f"PulseIn misses edges at or below {PLANNED_FREQUENCY} Hz "
        f"(ceiling {ceiling} Hz):\n" + "\n".join(lines))
