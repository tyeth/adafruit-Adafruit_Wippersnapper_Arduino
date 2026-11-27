"""
HIL tests for the ws.config.Settings each I2C sensor's component exposes:
one test per setting in the sensor's Wippersnapper_Components
definition.json, which tries every value the definition offers.

How Does This Work?
The tests are generated from the definitions, so a setting added to a
component's JSON gets a test without touching this file. For each sensor
in hil_sensors.SENSORS, each key in its definition's "settings" is a
test, test_i2c_setting[<sensor>-<key>] (with the good secrets, the
secrets_name default). After one checkin, for each
of the setting's values (every index of "options", or a float setting's
min, default and max) the broker sends an Add reading every type with
settings {key: value}, and the test expects:
- no error.component for the device (drvBase::configure()'s "Failed to
  apply setting: <key>" or "Unknown setting key: <key>"),
- an i2c.Event holding every type in range (collect_full_events()), or,
  for the values in SILENT, no event at all (modes that stop measuring),
- for sea_level_pressure, the altitude getEventAltitude() gives for the
  event's pressure at that sea-level pressure,
then sends a Remove before the next value. Every value is tried whichever
fail; the test fails listing the ones that did.

Coverage: a module fixture writes every value's result to
i2c_settings_coverage.md in the --serial-log-dir (and the log), one row
per (sensor, setting, value), and the settings in no test (a sensor not
in this run) are listed too.

Exercises:
- `src/components/i2c/drivers/drvBase.cpp::configure()`
- each sensor's `src/components/i2c/drivers/drv*.h::set*()` setters
- `src/components/i2c/controller.cpp::Handle_Add()` (settings)
"""

import json
import logging
import os
import time
from pathlib import Path

import pytest

import i2c_helpers as i2c
from hil_sensors import SENSORS, SLOW
from i2c_helpers import REMOVED_BUFFER


log = logging.getLogger("hil.i2c")

# The Adds' period, in seconds
SETTING_PERIOD = 3
# How long after a Remove an event the DUT published before handling it
# may still arrive
REMOVE_SETTLE = 1.0
# Values after which the sensor stops measuring, so publishes nothing:
# {(sensor, key, value): why}
SILENT = {
    ("mlx90632d_med", "mode", 3): "Halt stops measuring",
    ("ens160", "mode", 0): "Deep Sleep stops measuring",
    ("ens160", "mode", 1): "Idle stops measuring",
}
# sea_level_pressure: the altitude getEventAltitude() gives from the
# event's pressure must be within this many metres of the formula's
ALTITUDE_TOLERANCE = 1.0
DEFAULT_WS_COMPONENTS_DIR = "~/Wippersnapper_Components"


def _components_dir():
    value = os.environ.get("HIL_WS_COMPONENTS_DIR")
    return Path(value if value and value.strip()
                else DEFAULT_WS_COMPONENTS_DIR).expanduser()


def _settings(name):
    """The "settings" of the sensor's definition.json, {} if none (or the
    checkout isn't there: the component_definition fixture fails the test
    with why)."""
    path = _components_dir() / "components" / "i2c" / name / "definition.json"
    try:
        return json.loads(path.read_text()).get("settings") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _values(spec):
    """[(label, ws.config.Value)] for every value a setting offers: each
    option's index, or a float setting's min, default and max."""
    if "options" in spec:
        return [(f"{i}={option}", {"intValue": i})
                for i, option in enumerate(spec["options"])]
    values = []
    for key in ("min", "default", "max"):
        if key in spec and spec[key] not in [v for _, v in values]:
            values.append((f"{key} {spec[key]}", spec[key]))
    return [(label, {"floatValue": float(v)}) for label, v in values]


# Every (sensor, setting key, its spec) the definitions expose
CASES = [(name, key, spec) for name in SENSORS
         for key, spec in _settings(name).items()]


def _params():
    params = []
    for name, key, spec in CASES:
        marks = [pytest.mark.slow] if SENSORS[name][3] == SLOW else []
        params.append(pytest.param(name, key, spec, id=f"{name}-{key}",
                                   marks=marks))
    return params or [pytest.param(None, None, None, id="none")]


@pytest.fixture(scope="module")
def coverage(pytestconfig, tmp_path_factory):
    """Rows of (sensor, key, value label, expected, result, detail),
    written to i2c_settings_coverage.md once the module's tests are done."""
    rows = []
    yield rows
    log_dir = Path(pytestconfig.getoption("serial_log_dir")
                   or tmp_path_factory.mktemp("settings"))
    tested = {(r[0], r[1]) for r in rows}
    lines = ["# I2C sensor settings coverage", "",
             f"Wippersnapper_Components: `{_components_dir()}`", "",
             "| Sensor | Setting | Value | Expected | Result | Detail |",
             "|---|---|---|---|---|---|"]
    lines += [f"| {' | '.join(str(c).replace('|', '/') for c in r)} |"
              for r in rows]
    untested = [f"{n}.{k}" for n, k, _ in CASES if (n, k) not in tested]
    passed = sum(r[4] == "PASS" for r in rows)
    lines += ["", f"{passed}/{len(rows)} values passed, "
              f"{len(tested)}/{len(CASES)} settings tested"
              + (f"; not run: {', '.join(untested)}" if untested else "")]
    path = log_dir / "i2c_settings_coverage.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    log.info("settings coverage: %s\n%s", path, "\n".join(lines))


def _component_errors(protomq, uid, mark, address):
    return [err.get("message") for err in
            protomq.d2b_since(uid, "error.component", mark)
            if err.get("i2c", {}).get("address") == address]


def _altitude(pressure, sea_level):
    """getEventAltitude()'s altitude, in m, for pressure and sea_level in
    hPa."""
    return 44330.0 * (1.0 - (pressure / sea_level) ** 0.1903)


@pytest.mark.parametrize("name,key,spec", _params())
def test_i2c_setting(board, secrets_name, flashed_board, serial, protomq,
                     checkin, i2c_address_space, i2c_devices,
                     component_definition, coverage, name, key, spec):
    # --- Arrange ------------------------------------------------------
    if name is None:
        pytest.fail(f"no settings found in {_components_dir()} for "
                    f"{list(SENSORS)}", pytrace=False)
    address, value_range, first_event, _ = SENSORS[name]
    dev = i2c.device(i2c_devices, component_definition, name, address)
    types = dev.types()
    if key == "sea_level_pressure":
        # Checked against the formula instead: far from the default
        # sea-level pressure, a real altitude is outside the bench's range
        value_range = {k: v for k, v in value_range.items()
                       if k != "T_ALTITUDE"}
    uid = checkin()
    failures = {}

    for label, value in _values(spec):
        silent = SILENT.get((name, key, value.get("intValue")))
        expected = f"silent ({silent})" if silent else "full event"
        mark = protomq.d2b_mark(uid)

        # --- Act ------------------------------------------------------
        try:
            i2c.add(serial, protomq, uid, dev, i2c_address_space, types,
                    period=SETTING_PERIOD, settings={key: value})
        except AssertionError as e:
            errors = _component_errors(protomq, uid, mark, address)
            failures[label] = f"Add failed: {errors or e}"
            coverage.append((name, key, label, expected, "FAIL",
                             failures[label]))
            continue

        # --- Assert ---------------------------------------------------
        detail = ""
        if silent:
            time.sleep(SETTING_PERIOD + REMOVED_BUFFER)
            events = protomq.d2b_since(uid, "i2c.event", mark)
            if events:
                failures[label] = f"expected no event, got {events[0]}"
            else:
                detail = f"no event in {SETTING_PERIOD + REMOVED_BUFFER}s"
        else:
            found, failed = i2c.collect_full_events(
                protomq, uid, [(dev, types, value_range)], i2c_address_space,
                mark, first_event)
            if address in failed:
                failures[label] = failed[address]
            else:
                values = found[address]
                detail = ", ".join(f"{k.removeprefix('T_')} {v:.4g}"
                                   for k, v in values.items())
                if key == "sea_level_pressure":
                    sea_level = value["floatValue"]
                    want = _altitude(values["T_PRESSURE"], sea_level)
                    got = values["T_ALTITUDE"]
                    if abs(got - want) > ALTITUDE_TOLERANCE:
                        failures[label] = (
                            f"altitude {got:.2f} m, expected {want:.2f} m "
                            f"for {values['T_PRESSURE']} hPa at {sea_level}")
        errors = _component_errors(protomq, uid, mark, address)
        if errors:
            failures.setdefault(label, f"error.component: {errors}")
        coverage.append((name, key, label, expected,
                         "FAIL" if label in failures else "PASS",
                         failures.get(label, detail)))
        log.info("%s %s=%s: %s", name, key, label,
                 failures.get(label, f"PASS {detail}"))

        # --- Act ------------------------------------------------------
        i2c.remove(protomq, uid, dev, i2c_address_space)
        time.sleep(REMOVE_SETTLE)

    assert not failures, f"{name}.{key}: " + "; ".join(
        f"{label}: {why}" for label, why in failures.items())
