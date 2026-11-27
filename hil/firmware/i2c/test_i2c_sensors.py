"""
HIL tests for Adafruit Wippersnapper firmware's I2C sensor drivers running
together: sensors wired to the DUT are added after one checkin, each
reading every type in its component definition; an i2c.Event holding all
of a sensor's types arrives at the broker from each, at its own period;
and once they're removed no more do. The I2C API itself (Modify,
rejected Adds, ...) is test_i2c_api.py's.

- test_all_i2c_sensor_types_add_read_remove: the sensors with no
  warm-up, together.
- test_four_i2c_sensors_add_read_remove: FOUR_SENSORS, a mix of driver
  kinds (a heater conversion, a ticking driver, plain reads), for
  problems that only show up with several drivers active.
- test_eight_i2c_sensors_add_read_remove: every sensor in SENSORS, so
  waits out the warm-ups (slow).
- test_i2c_sensors_independent_periods: each sensor at its own period in
  PERIODS; every sensor's events arrive at its own interval, within
  INTERVAL_EARLY/INTERVAL_LATE.
- test_i2c_sensors_same_period_drift: every sensor at DRIFT_PERIOD, all
  due at once, timed over DRIFT_INTERVALS intervals as above. A benchmark
  too: logs (and records as junit properties) each sensor's mean, min and
  max interval, jitter (standard deviation), drift per interval (mean
  less the period) and drift (how far the last event is behind an ideal
  clock started at the first). Writes drift_report.py's HTML charts of
  it to the --serial-log-dir (i2c_sensor_drift_<run>.html), once with
  every sensor and once per DRIFT_RUNS exclusion (e.g. without the
  BME688), noting what was left out and why.

How Does This Work?
The sensors in SENSORS are wired to the DUT's I2C port at their Adafruit
breakouts' default addresses, each listed in the board entry's i2c_bus
probe (the i2c_devices and i2c_address_space fixtures). A sensor's
component definition in Wippersnapper_Components
(components/i2c/<name>/definition.json) lists its subcomponents; the
Add's types map keys each one's ws.sensor.Type by its 0-based position
in that list (i2c_helpers.py). After one checkin the broker sends an Add
per sensor, the test waits a period and checks the readings in the
i2c.Events the DUT publishes after it, then sends a Remove per sensor.
Every sensor is checked, whichever fail; the log ends with a PASS/FAIL
line per sensor, and the failure lists each one's.

checkin() power-cycles the DUT and the sensors with it, so sensors with a
warm-up start it: the SEN66 publishes some types before the rest
(collect_full_events() waits for an event with all of them) for a
minute, the ENS160 nothing for 3 minutes, so they're only in the slow
test.

Exercises:
- `src/components/i2c/controller.cpp::Handle_Add()`
- `src/components/i2c/controller.cpp::Handle_Remove()`
- `src/components/i2c/controller.cpp::update()` (several drivers, periods)
- `src/components/i2c/drivers/drvBase.h::ReadDue()`
- each sensor's `src/components/i2c/drivers/drv*.h::begin()` and
  `getEvent*()`
"""

import logging
import statistics
import time

import pytest

import drift_report
import i2c_helpers as i2c
from hil_sensors import FAST_SENSORS, SENSORS
from i2c_helpers import (FIRST_EVENT_TIMEOUT, INTERVAL_EARLY, INTERVAL_LATE,
                         PERIOD, REMOVED_BUFFER)


log = logging.getLogger("hil.i2c")


# The Adds' period, in seconds: short, so the check that the removed
# sensors publish nothing (a period plus REMOVED_BUFFER) is short too
SENSOR_PERIOD = 3
# How long after the last Remove reaches the DUT an event it published
# before handling it may still arrive
REMOVE_SETTLE = 1.0

# Four drivers of different kinds: the BME688's blocking gas-heater
# conversion, the MLX90632's fastTick(), and two plain reads
FOUR_SENSORS = ["bme688", "mlx90632d_med", "max44009", "ina219"]
# Each sensor's own period, in seconds, for the independent periods test:
# all different, none a multiple of another's below the longest
PERIODS = {
    "bme688": 2,
    "max44009": 3,
    "ina219": 5,
    "max17048": 7,
    "mlx90632d_med": 4,
    "vcnl4040": 6,
}
# How many intervals between events to time per sensor
PERIOD_INTERVALS = 4
# The drift benchmark: every sensor at DRIFT_PERIOD seconds, timed over
# DRIFT_INTERVALS intervals (about 30 s)
DRIFT_PERIOD = 3
DRIFT_INTERVALS = 10
# The drift benchmark's runs: {id: sensors left out, {name: why}}. Without
# the BME688 shows the drift the other drivers add on their own.
DRIFT_RUNS = {
    "all": {},
    "without_bme688": {
        "bme688": "its gas heater's blocking ~0.4 s conversion holds up "
                  "every sensor read in the same pass",
    },
}


# --- Steps ----------------------------------------------------------------


def _devices(names, i2c_devices, component_definition):
    """[(I2cDevice, types, value_range)] for the sensors named, reading
    every type in their definitions, and the longest of their first-event
    timeouts."""
    sensors = []
    timeout = 0
    for name in names:
        address, value_range, first_event, _ = SENSORS[name]
        dev = i2c.device(i2c_devices, component_definition, name, address)
        sensors.append((dev, dev.types(), value_range))
        timeout = max(timeout, first_event)
    return sensors, timeout


def _add_all(serial, protomq, uid, address_space, sensors, periods, mark,
             failures):
    """Add every sensor, at periods[name] seconds. Returns the ones the
    DUT added; the rest go in failures with the error.component it sent,
    so one sensor failing doesn't stop the others."""
    added = []
    for dev, types, value_range in sensors:
        try:
            i2c.add(serial, protomq, uid, dev, address_space, types,
                    period=periods[dev.name])
        except AssertionError as e:
            errors = [err.get("message") for err in protomq.d2b_since(
                uid, "error.component", mark)
                if err.get("i2c", {}).get("address") == dev.address]
            failures[dev.name] = f"Add failed: {errors or e}"
            continue
        added.append((dev, types, value_range))
    return added


def _remove_all(protomq, uid, address_space, added, period, mark, failures):
    """Remove every added sensor, then expect no event from any of them
    for period plus REMOVED_BUFFER, and no error.component from them
    since mark."""
    for dev, _, _ in added:
        i2c.remove(protomq, uid, dev, address_space)
    time.sleep(REMOVE_SETTLE)
    removed = protomq.d2b_mark(uid)
    time.sleep(period + REMOVED_BUFFER)
    names = {dev.address: dev.name for dev, _, _ in added}
    for event in protomq.d2b_since(uid, "i2c.event", removed):
        address = event.get("descriptor", {}).get("address")
        failures.setdefault(names.get(address, f"0x{address:02x}"),
                            f"Remove: event after it: {event}")
    for err in protomq.d2b_since(uid, "error.component", mark):
        name = names.get(err.get("i2c", {}).get("address"))
        if name:
            failures.setdefault(name, f"error.component: {err}")


def _report(sensors, results, failures):
    """Log a PASS/FAIL line per sensor, then fail listing the failures."""
    for dev, _, _ in sensors:
        if dev.name in failures:
            log.error("FAIL %-14s 0x%02x  %s", dev.name, dev.address,
                      failures[dev.name])
        else:
            log.info("PASS %-14s 0x%02x  %s", dev.name, dev.address,
                     results.get(dev.address))
    assert not failures, "\n".join(
        f"{name}: {why}" for name, why in failures.items())


def _add_read_remove(serial, protomq, checkin, address_space, i2c_devices,
                     component_definition, names):
    """Add the sensors named at SENSOR_PERIOD, expect an event holding
    every type from each after a period, remove them, and report."""
    # --- Arrange ------------------------------------------------------
    sensors, timeout = _devices(names, i2c_devices, component_definition)
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    failures = {}

    # --- Act ----------------------------------------------------------
    added = _add_all(serial, protomq, uid, address_space, sensors,
                     {dev.name: SENSOR_PERIOD for dev, _, _ in sensors},
                     mark, failures)
    # The events read at the period, not the first read straight after
    # the Add
    time.sleep(SENSOR_PERIOD)
    period_mark = protomq.d2b_mark(uid)

    # --- Assert -------------------------------------------------------
    # An event from every sensor holding every type in its definition,
    # each in range...
    found, failed = i2c.collect_full_events(protomq, uid, added,
                                            address_space, period_mark,
                                            timeout)
    for dev, _, _ in added:
        if dev.address in failed:
            failures[dev.name] = f"Event: {failed[dev.address]}"
    # ...and none once they're removed
    _remove_all(protomq, uid, address_space, added, SENSOR_PERIOD, mark,
                failures)
    _report(sensors, found, failures)


# --- Tests ----------------------------------------------------------------


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_all_i2c_sensor_types_add_read_remove(board, secrets_name,
                                              flashed_board, serial, protomq,
                                              checkin, i2c_address_space,
                                              i2c_devices,
                                              component_definition):
    _add_read_remove(serial, protomq, checkin, i2c_address_space,
                     i2c_devices, component_definition, FAST_SENSORS)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_four_i2c_sensors_add_read_remove(board, secrets_name,
                                          flashed_board, serial, protomq,
                                          checkin, i2c_address_space,
                                          i2c_devices, component_definition):
    _add_read_remove(serial, protomq, checkin, i2c_address_space,
                     i2c_devices, component_definition, FOUR_SENSORS)


@pytest.mark.slow
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_eight_i2c_sensors_add_read_remove(board, secrets_name,
                                           flashed_board, serial, protomq,
                                           checkin, i2c_address_space,
                                           i2c_devices,
                                           component_definition):
    _add_read_remove(serial, protomq, checkin, i2c_address_space,
                     i2c_devices, component_definition, list(SENSORS))


def _timing(protomq, uid, mark, added, periods, count, failures,
            record_property):
    """Time each added sensor's i2c.Events since mark against its period
    in periods: its first count intervals, each within -INTERVAL_EARLY/
    +INTERVAL_LATE of the period (update() restarts the period after the
    read and the publish, so the conversion and the latency only add).
    Logs a benchmark line per sensor, records it as junit properties, and
    returns ({address: summary}, {address: [event times]}); failures get
    the sensors out of bounds."""
    events = protomq.d2b_timed_since(uid, "i2c.event", mark)
    results = {}
    timed = {}
    log.info("%-14s %6s %4s %8s %8s %8s %8s %9s %10s", "sensor", "period",
             "n", "mean", "min", "max", "jitter", "drift/int", "drift")
    for dev, _, _ in added:
        period = periods[dev.name]
        times = [t for t, event in events
                 if event.get("descriptor", {}).get("address") == dev.address]
        if None in times:
            pytest.fail("ProtoMQ doesn't timestamp deliveries, can't time "
                        "the events", pytrace=False)
        times = times[:count + 1]
        timed[dev.address] = times
        intervals = [b - a for a, b in zip(times, times[1:])]
        if len(intervals) < count:
            failures[dev.name] = (
                f"Period {period}s: only {len(times)} events, intervals "
                f"{[round(i, 2) for i in intervals]}")
            continue
        mean = statistics.mean(intervals)
        jitter = statistics.pstdev(intervals)
        # How far the last event is behind an ideal clock started at the
        # first: the drift accumulated over the run
        drift = (times[-1] - times[0]) - count * period
        summary = {"period": period, "intervals": count,
                   "mean": round(mean, 3), "min": round(min(intervals), 3),
                   "max": round(max(intervals), 3),
                   "jitter": round(jitter, 3),
                   "drift_per_interval": round(mean - period, 3),
                   "drift": round(drift, 3)}
        results[dev.address] = summary
        log.info("%-14s %5ss %4d %7.3fs %7.3fs %7.3fs %7.3fs %+8.3fs "
                 "%+9.3fs", dev.name, period, count, mean, min(intervals),
                 max(intervals), jitter, mean - period, drift)
        for key, value in summary.items():
            record_property(f"{dev.name}.{key}", value)
        late = [round(i, 2) for i in intervals
                if not period - INTERVAL_EARLY <= i <= period + INTERVAL_LATE]
        if late:
            failures[dev.name] = (
                f"Period {period}s: intervals {late} outside "
                f"{period - INTERVAL_EARLY}-{period + INTERVAL_LATE}s")
    return results, timed


def _period_run(serial, protomq, checkin, address_space, i2c_devices,
                component_definition, periods, count, record_property,
                report=None, excluded=None):
    """Add the sensors in periods, each at its period there, time their
    first count intervals (_timing()), remove them, and report. If report
    is (path, title), also write drift_report's HTML there, before
    failing."""
    # --- Arrange ------------------------------------------------------
    sensors, _ = _devices(periods, i2c_devices, component_definition)
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    failures = {}
    # Long enough for count intervals of the longest period, each up to
    # INTERVAL_LATE late, after the first event
    watch = ((max(periods.values()) + INTERVAL_LATE) * count
             + FIRST_EVENT_TIMEOUT)

    # --- Act ----------------------------------------------------------
    added = _add_all(serial, protomq, uid, address_space, sensors, periods,
                     mark, failures)
    time.sleep(watch)

    # --- Assert -------------------------------------------------------
    # Each sensor's events at its period...
    results, timed = _timing(protomq, uid, mark, added, periods, count,
                             failures, record_property)
    # ...and none once they're removed
    _remove_all(protomq, uid, address_space, added, max(periods.values()),
                mark, failures)
    if report:
        path, title = report
        written = drift_report.write_report(
            path, title,
            f"{len(sensors)} sensors, {count} intervals each, "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}",
            [{"name": dev.name, "address": dev.address,
              "period": periods[dev.name], "slot": list(SENSORS).index(
                  dev.name),
              "times": timed.get(dev.address, []),
              "summary": results.get(dev.address),
              "failure": failures.get(dev.name)}
             for dev, _, _ in sensors],
            INTERVAL_EARLY, INTERVAL_LATE, excluded=excluded)
        log.info("drift report: %s", written)
    _report(sensors, results, failures)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_sensors_independent_periods(board, secrets_name, flashed_board,
                                         serial, protomq, checkin,
                                         i2c_address_space, i2c_devices,
                                         component_definition,
                                         record_property):
    _period_run(serial, protomq, checkin, i2c_address_space, i2c_devices,
                component_definition, PERIODS, PERIOD_INTERVALS,
                record_property)


@pytest.mark.parametrize("excluded", DRIFT_RUNS.values(),
                         ids=DRIFT_RUNS.keys())
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_sensors_same_period_drift(board, secrets_name, flashed_board,
                                       serial, protomq, checkin,
                                       i2c_address_space, i2c_devices,
                                       component_definition,
                                       record_property, pytestconfig,
                                       tmp_path, request, excluded):
    # The report goes with the serial logs (build-hil.yml's artifact)
    log_dir = pytestconfig.getoption("serial_log_dir") or tmp_path
    run = request.node.callspec.id.split("-")[-1]
    title = f"I2C sensor drift at a {DRIFT_PERIOD} s period"
    if excluded:
        title += f", without {', '.join(excluded)}"
    _period_run(serial, protomq, checkin, i2c_address_space, i2c_devices,
                component_definition,
                {name: DRIFT_PERIOD for name in FAST_SENSORS
                 if name not in excluded},
                DRIFT_INTERVALS, record_property,
                report=(log_dir / f"i2c_sensor_drift_{run}.html", title),
                excluded=excluded)
