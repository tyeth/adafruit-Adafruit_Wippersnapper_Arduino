"""
HIL tests for the componentAdds of Adafruit Wippersnapper firmware's
checkin: the components the broker's checkin.Response tells the DUT to
set up, so they survive between boots. The DUT applies each Add while it
decodes the response, before it sends checkin.Complete; no component
message comes from the broker after checkin.

One test per component type, then one adding everything wired to the DUT
(the board entry's probes in hil-boards.json) in a single response. Each
checks the DUT logs the Add during checkin, and that the component then
works: the probes read the outputs, and the DUT publishes events for the
inputs and sensors.

Exercises:
- `src/components/checkin/model.cpp::cbSetupResponse()`
- `src/components/checkin/model.cpp::cbDigitalAdds()`, `cbAnalogAdds()`,
  `cbServoAdds()`, `cbPWMAdds()`, `cbDs18x20Adds()`, `cbI2cAdds()`
- `src/wippersnapper.cpp::handleCheckinResponse()`
"""

import json
import logging
import re
import time

import pytest

import hil_board_utils
import hil_protomq
import i2c_helpers as i2c
from hil_sensors import FAST, MUX_SENSORS, SENSORS
from i2c_helpers import INTERVAL_LATE, PERIOD as I2C_PERIOD

log = logging.getLogger("hil.checkin")

# The digital output and input: D14 starts high from the Add's write, D32
# is a pulled-up input
DIGITAL_OUT = "D14"
DIGITAL_IN = "D32"
# digital.Direction values, as Handle_Digital_Add() prints them
D_INPUT_PULL_UP, D_OUTPUT = 2, 3
# How long a digital pin may take to reach a written or driven value
PIN_TIMEOUT = 2
# Analog: the DAC's voltage, the Add's period (s), and how far a T_RAW
# value may be from it as a fraction of full scale (as test_analog_in.py)
ANALOG_VOLTS = 1.65
ANALOG_PERIOD = 5
RAW_MAX = 2 ** 16
RAW_TOLERANCE = 0.08
# T_RAW, as Handle_Analog_Add() prints it
T_RAW = 18
# Servo: the broker's defaults (as test_servo.py)
SERVO_FREQ = 50
SERVO_MIN_US = 500
SERVO_MAX_US = 2500
SERVO_PERIODS = 25
PULSE_WIDTH_TOLERANCE = 7
# PWM: the broker's defaults, and the Add's initial write, 50% duty
PWM_FREQ = 5000
PWM_RESOLUTION = 12
PWM_DUTY = 2048
PWM_PERIODS = 100
PROBE_DUTY_ERROR = 0.006  # Pi GPIO probe jitter at 5 kHz: seen up to +/-0.53%
PERIOD_TOLERANCE = 0.005
# DS18x20: as test_ds18x20.py
DS_RESOLUTION = 12
DS_PERIOD = 10
CELSIUS = "T_OBJECT_TEMPERATURE"
TEMP_RANGE = (5.0, 50.0)
# How long after checkin the first event of an input or sensor may take
FIRST_EVENT_TIMEOUT = 15
# The sensor the I2C test adds
I2C_DEVICE = "bme688"
# An address in I2C_DEVICE's i2cAddresses with nothing wired to it (the
# BME688's other address): a sensor saved on Adafruit IO, then unplugged
# between boots
UNPLUGGED_ADDRESS = 0x76
UNPLUGGED_ERROR = "Failed to initialize driver, check wiring + address!"


def _power_on(board, power):
    """Power the DUT on, if it isn't, and wait for its serial port."""
    loc = board.entry["location"]
    hil_board_utils._power_on(loc["hub"], loc["port"], **power)
    port = str(hil_board_utils.wait_for_port(board.entry, timeout=15))
    if port != board.port:
        pytest.fail(f"board '{board.name}' came back on {port}, not "
                    f"{board.port}", pytrace=False)


@pytest.fixture(scope="module", autouse=True)
def powered_after_module(board, power):
    """Leave the DUT powered on for the tests after this module's."""
    yield
    _power_on(board, power)


@pytest.fixture(autouse=True)
def powered_between_tests(board, power, powered_after_module):
    """Power the DUT on before the test and off after it, so the inputs
    and sensors it added don't keep publishing into the next test."""
    _power_on(board, power)
    loc = board.entry["location"]
    yield
    hil_board_utils._power_off(loc["hub"], loc["port"], **power)
    hil_board_utils.wait_for_port_gone(board.entry, timeout=10)
    hil_board_utils.clear_port(board.name)


@pytest.fixture
def analog_pin(dac_probe):
    return next(iter(dac_probe.pin_map))


@pytest.fixture
def pulse_pin(pulse_probe):
    return next(iter(pulse_probe.pin_map))


# --- Adds, and what the DUT logs for each during checkin ----------------


def _digital_out_add(pin=DIGITAL_OUT):
    return {"pin": pin, "gpioDirection": "D_OUTPUT",
            "write": {"pin": pin, "value": {"boolValue": True}}}


def _digital_in_add(pin=DIGITAL_IN):
    return {"pin": pin, "gpioDirection": "D_INPUT_PULL_UP",
            "sampleMode": "SM_EVENT"}


def _expect_digital(serial, pin, direction):
    serial.expect(r"^\[digital\] Added new pin:$", timeout=5)
    serial.expect(rf"^Pin Name: {pin.lstrip('D')}$", timeout=5)
    serial.expect(rf"^Direction: {direction}$", timeout=5)


def _analog_add(pin):
    return {"pin": pin, "readMode": "T_RAW", "sampleMode": "SM_TIMER",
            "period": ANALOG_PERIOD}


def _expect_analog(serial, pin):
    serial.expect(r"^\[analog\] Added new pin:$", timeout=5)
    serial.expect(rf"^Pin Name: {pin}$", timeout=5)
    serial.expect(rf"^Read Mode: {T_RAW}$", timeout=5)


def _servo_add(pin):
    return {"servoPin": pin, "freq": SERVO_FREQ,
            "minPulseWidth": SERVO_MIN_US, "maxPulseWidth": SERVO_MAX_US}


def _expect_servo(serial, pin):
    # writeMicroseconds() logs without a newline, so it shares the line
    serial.expect(rf"\[servo\] Set Pulse Width: {SERVO_MIN_US} uS on pin: "
                  rf"{pin[1:]}\[servo\] Servo attached to pin: {pin}",
                  timeout=5)


def _pwm_add(pin):
    return {"pin": pin, "frequency": PWM_FREQ, "resolution": PWM_RESOLUTION,
            "write": {"pin": pin, "dutyCycle": PWM_DUTY}}


def _expect_pwm(serial, pin):
    # "Attached pin: <pin>" has no newline, so the write's log follows it
    serial.expect(rf"\[pwm\] Attached pin: {pin}\[pwm\] Processing initial "
                  r"write\.\.\.", timeout=5)


def _ds18x20_add(pin):
    return {"onewirePin": pin, "sensorResolution": DS_RESOLUTION,
            "period": DS_PERIOD, "types": [CELSIUS]}


def _expect_ds18x20(serial):
    serial.expect(r"^\[ds18x20\] Handle_Ds18x20Add MESSAGE\.\.\.$", timeout=5)
    serial.expect(r"^\[ds18x\] New Sensor Added!$", timeout=5)


def _expect_mux(serial):
    # InitMux() prints no newline on success
    serial.expect(r"\[i2c\] Initializing MUX driver\.\.\.", timeout=5,
                  partial=True)


# --- What each component does after checkin -------------------------------


def _assert_no_component_errors(protomq, uid):
    errors = protomq.d2b_since(uid, "error.component", 0)
    assert not errors, f"expected no error.component, got {errors}"


def _assert_digital_out(serial, protomq, probe, uid, pin):
    """pin is high from the Add's write; a digital.Write drives it low."""
    assert probe.value(pin), f"{pin} should be high from the Add's write"
    with probe.expect_digitalout_transition(pin, True, False,
                                            timeout=PIN_TIMEOUT):
        protomq.publish(uid, {"digital": {"write": {
            "pin": pin, "value": {"boolValue": False}}}})


def _assert_digital_in(protomq, probe, uid, pin):
    """pin publishes its pulled-up 1, then 0 once the probe drives it low."""
    first = protomq.expect_d2b_message(uid, "digital.event",
                                       match={"pin": pin},
                                       timeout=FIRST_EVENT_TIMEOUT)
    assert first["value"].get("boolValue", False) is True, (
        f"expected {pin}'s pull-up to read 1 first: {first}")
    probe.drive(pin, False)
    try:
        event = protomq.expect_d2b_message(uid, "digital.event",
                                           match={"pin": pin},
                                           timeout=PIN_TIMEOUT)
        assert event["value"].get("boolValue", False) is False, (
            f"expected {pin} to read 0 once driven low: {event}")
    finally:
        probe.release(pin)


def _assert_analog(protomq, dac_probe, uid, pin, ref_voltage):
    """With the DAC at ANALOG_VOLTS, pin's next timer event reads near it.
    Events from before the DAC was set are skipped."""
    volts = dac_probe.set_voltage(pin, ANALOG_VOLTS)
    set_at = time.time()
    expected = volts / ref_voltage * RAW_MAX
    deadline = time.monotonic() + FIRST_EVENT_TIMEOUT + ANALOG_PERIOD
    while True:
        event = protomq.expect_d2b_message(
            uid, "analog.event", match={"pin": pin},
            timeout=max(deadline - time.monotonic(), 0.1))
        if protomq.last_d2b_time(uid) > set_at + 0.2:
            break
    value = event["value"].get("floatValue", 0)
    log.info("%s read %.0f, expected %.0f", pin, value, expected)
    assert abs(value - expected) <= RAW_TOLERANCE * RAW_MAX, (
        f"{pin} read {value:.0f}, expected {expected:.0f} +/- "
        f"{RAW_TOLERANCE * RAW_MAX:.0f} for the DAC's {volts:.3f}V")


def _assert_pulse(pulse_probe, pin, periods, frequency, duty=None,
                  width_us=None):
    """pin runs at frequency, with duty (a fraction) or a width_us pulse."""
    measured, period_us = pulse_probe.measure(pin, periods=periods)
    log.info("%s: duty %.3f%%, period %.2fus", pin, measured * 100,
             period_us)
    expected_period = 1e6 / frequency
    assert abs(period_us - expected_period) <= (
        PERIOD_TOLERANCE * expected_period), (
        f"{pin}: period {period_us:.2f}us, expected {expected_period:.0f}us")
    if duty is not None:
        tolerance = 1 / 2 ** PWM_RESOLUTION + PROBE_DUTY_ERROR
        assert abs(measured - duty) <= tolerance, (
            f"{pin}: duty {measured:.3%}, expected {duty:.3%} +/- "
            f"{tolerance:.3%}")
    if width_us is not None:
        width = measured * period_us
        assert abs(width - width_us) <= PULSE_WIDTH_TOLERANCE, (
            f"{pin}: pulse {width:.1f}us, expected {width_us}us +/- "
            f"{PULSE_WIDTH_TOLERANCE}us")


def _assert_ds18x20(protomq, uid, pin):
    event = protomq.expect_d2b_message(uid, "ds18x20.event",
                                       match={"onewirePin": pin},
                                       timeout=FIRST_EVENT_TIMEOUT)
    events = event.get("events", [])
    assert [e.get("type") for e in events] == [CELSIUS], event
    value = events[0].get("floatValue")
    assert value is not None and TEMP_RANGE[0] <= value <= TEMP_RANGE[1], (
        f"expected {CELSIUS} {TEMP_RANGE}, got {value}: {event}")


def _assert_i2c_events(protomq, uid, sensors):
    """A full i2c.Event from each (dev, types, value_range, space)."""
    for dev, types, value_range, space in sensors:
        i2c.full_events_on(protomq, uid, dev, space, types, 0, 1,
                           FIRST_EVENT_TIMEOUT + I2C_PERIOD + INTERVAL_LATE,
                           value_range)


# --- One test per component type -----------------------------------------


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_digital(board, secrets_name, flashed_board, serial,
                              protomq, probe, checkin):
    # --- Arrange / Act ------------------------------------------------
    # The checkin.Response adds D14 as an output, written high
    probe.release(DIGITAL_OUT)
    uid = checkin(
        {"digitalAdds": [_digital_out_add()]},
        lambda: _expect_digital(serial, DIGITAL_OUT, D_OUTPUT))

    # --- Assert -------------------------------------------------------
    # D14 is high, and the controller has it: a digital.Write drives it low
    _assert_digital_out(serial, protomq, probe, uid, DIGITAL_OUT)
    _assert_no_component_errors(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_analog(board, board_definition, secrets_name,
                             flashed_board, serial, protomq, dac_probe,
                             analog_pin, checkin):
    # --- Arrange / Act ------------------------------------------------
    # The checkin.Response adds the DAC's pin as a T_RAW SM_TIMER input
    uid = checkin({"analogAdds": [_analog_add(analog_pin)]},
                  lambda: _expect_analog(serial, analog_pin))

    # --- Assert -------------------------------------------------------
    _assert_analog(protomq, dac_probe, uid, analog_pin,
                   float(board_definition.definition["mcuRefVoltage"]))
    _assert_no_component_errors(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_servo(board, secrets_name, flashed_board, serial,
                            protomq, pulse_probe, pulse_pin, checkin):
    # --- Arrange / Act ------------------------------------------------
    uid = checkin({"servoAdds": [_servo_add(pulse_pin)]},
                  lambda: _expect_servo(serial, pulse_pin))

    # --- Assert -------------------------------------------------------
    # 50 Hz at the minimum, 500us, pulse
    _assert_pulse(pulse_probe, pulse_pin, SERVO_PERIODS, SERVO_FREQ,
                  width_us=SERVO_MIN_US)
    _assert_no_component_errors(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_pwm(board, secrets_name, flashed_board, serial,
                          protomq, pulse_probe, pulse_pin, checkin):
    # --- Arrange / Act ------------------------------------------------
    uid = checkin({"pwmAdds": [_pwm_add(pulse_pin)]},
                  lambda: _expect_pwm(serial, pulse_pin))

    # --- Assert -------------------------------------------------------
    # 5 kHz at the initial write's 50% duty
    _assert_pulse(pulse_probe, pulse_pin, PWM_PERIODS, PWM_FREQ,
                  duty=PWM_DUTY / 2 ** PWM_RESOLUTION)
    _assert_no_component_errors(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_ds18x20(board, secrets_name, flashed_board, serial,
                              protomq, onewire_pin, checkin):
    # --- Arrange / Act ------------------------------------------------
    uid = checkin({"ds18x20Adds": [_ds18x20_add(onewire_pin)]},
                  lambda: _expect_ds18x20(serial))

    # --- Assert -------------------------------------------------------
    _assert_ds18x20(protomq, uid, onewire_pin)
    _assert_no_component_errors(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_i2c(board, secrets_name, flashed_board, serial,
                          protomq, i2c_devices, i2c_address_space,
                          component_definition, checkin):
    # --- Arrange ------------------------------------------------------
    address, value_range, _, _ = SENSORS[I2C_DEVICE]
    dev = i2c.device(i2c_devices, component_definition, I2C_DEVICE, address)
    types = dev.types()

    # --- Act ----------------------------------------------------------
    uid = checkin(
        {"i2cAdds": [i2c.add_msg(dev, i2c_address_space, types)]},
        lambda: i2c.expect_driver_initialized(serial, dev))

    # --- Assert -------------------------------------------------------
    _assert_i2c_events(protomq, uid,
                       [(dev, types, value_range, i2c_address_space)])
    _assert_no_component_errors(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_i2c_unplugged(board, secrets_name, flashed_board,
                                    serial, protomq, i2c_devices,
                                    i2c_address_space, component_definition,
                                    checkin):
    # --- Arrange ------------------------------------------------------
    # The checkin.Response adds I2C_DEVICE at an address nothing answers
    # (unplugged since the last boot), then the one that's wired
    address, value_range, _, _ = SENSORS[I2C_DEVICE]
    if UNPLUGGED_ADDRESS in i2c_devices.addresses:
        pytest.fail(f"i2c_bus probe lists 0x{UNPLUGGED_ADDRESS:02x}, which "
                    "this test needs empty", pytrace=False)
    dev = i2c.device(i2c_devices, component_definition, I2C_DEVICE, address)
    unplugged = i2c.I2cDevice(dev.name, UNPLUGGED_ADDRESS, dev.component)
    types = dev.types()

    def expect_adds():
        # The unplugged sensor's driver fails to begin(), the wired one's
        # initializes
        serial.expect(r"^\[i2c\] Creating sensor driver\.\.\.$", timeout=5)
        serial.expect(r"^\[i2c\] ERROR: Failed to initialize I2C driver!$",
                      timeout=5)
        i2c.expect_driver_initialized(serial, dev)

    # --- Act ----------------------------------------------------------
    uid = checkin(
        {"i2cAdds": [i2c.add_msg(unplugged, i2c_address_space, types),
                     i2c.add_msg(dev, i2c_address_space, types)]},
        expect_adds)

    # --- Assert -------------------------------------------------------
    # The DUT completed checkin (checkin() expects it) and reports the
    # unplugged sensor as a component error, not by rebooting
    errors = protomq.d2b_since(uid, "error.component", 0)
    assert any(e.get("message") == UNPLUGGED_ERROR
               and e.get("i2c", {}).get("address") == UNPLUGGED_ADDRESS
               for e in errors), (
        f"expected an error.component for 0x{UNPLUGGED_ADDRESS:02x}: "
        f"{errors}")
    serial.assert_none(r"Unable to decode checkin response|ERROR \[RESET\]")
    # The wired sensor works, and nothing is read from the unplugged one
    _assert_i2c_events(protomq, uid,
                       [(dev, types, value_range, i2c_address_space)])
    events = [e for e in protomq.d2b_since(uid, "i2c.event", 0)
              if e.get("descriptor", {}).get("address") == UNPLUGGED_ADDRESS]
    assert not events, f"expected no i2c.event from 0x76: {events}"


# --- Everything wired to the DUT in one checkin.Response ------------------


def _all_i2c(i2c_devices, i2c_address_space, i2c_mux, component_definition):
    """[(I2cDevice, types, value_range, address space, fast)] for every
    sensor in SENSORS the i2c_bus probe lists on the bus, then every one in
    MUX_SENSORS on each mux channel the probe lists it on."""
    sensors = []
    for name, (address, value_range, _, speed) in SENSORS.items():
        if address in i2c_devices.addresses:
            dev = i2c.device(i2c_devices, component_definition, name, address)
            sensors.append((dev, dev.types(), value_range, i2c_address_space,
                            speed == FAST))
    for name, (address, value_range, _, speed) in MUX_SENSORS.items():
        dev, channels = i2c.mux_device(i2c_mux, component_definition, name,
                                       address)
        for channel in channels:
            space = i2c.mux_address_space(i2c_address_space, i2c_mux, channel)
            sensors.append((dev, dev.types(), value_range, space,
                            speed == FAST))
    return sensors


def _run_adds_all(board_definition, serial, protomq, probe, pulse_probe,
                  pulse_pin, onewire_pin, i2c_devices, i2c_address_space,
                  i2c_mux, component_definition, checkin, dac_probe=None,
                  analog_pin=None):
    """Check in with an Add for everything the board entry's probes say is
    wired to the DUT, and check each component works. Without dac_probe
    and analog_pin, the analog Add and its check are left out."""
    # --- Arrange ------------------------------------------------------
    # The pwm_input pin is a PWM output here, not a servo: it's the only
    # pin the pulse probe measures. The mux's Add comes before the sensors
    # on its channels. Slow sensors (minutes of warm-up) are only checked
    # to initialize.
    with_analog = analog_pin is not None
    sensors = _all_i2c(i2c_devices, i2c_address_space, i2c_mux,
                       component_definition)
    mux_add = {"descriptor": i2c.mux_descriptor(i2c_address_space, i2c_mux),
               "name": i2c_mux.type}
    adds = {"digitalAdds": [_digital_out_add(), _digital_in_add()]}
    if with_analog:
        adds["analogAdds"] = [_analog_add(analog_pin)]
    adds.update({
        "pwmAdds": [_pwm_add(pulse_pin)],
        "ds18x20Adds": [_ds18x20_add(onewire_pin)],
        "i2cAdds": [mux_add] + [i2c.add_msg(dev, space, types)
                                for dev, types, _, space, _ in sensors],
    })
    log.info("componentAdds: %s", adds)

    def expect_adds():
        # The DUT decodes ComponentAdds' fields in field-number order,
        # and each repeated field's Adds in order
        _expect_digital(serial, DIGITAL_OUT, D_OUTPUT)
        _expect_digital(serial, DIGITAL_IN, D_INPUT_PULL_UP)
        if with_analog:
            _expect_analog(serial, analog_pin)
        _expect_pwm(serial, pulse_pin)
        _expect_ds18x20(serial)
        _expect_mux(serial)
        for dev, _, _, _, _ in sensors:
            i2c.expect_driver_initialized(serial, dev)

    # --- Act ----------------------------------------------------------
    probe.release()
    uid = checkin(adds, expect_adds)

    # --- Assert -------------------------------------------------------
    _assert_digital_in(protomq, probe, uid, DIGITAL_IN)
    _assert_digital_out(serial, protomq, probe, uid, DIGITAL_OUT)
    _assert_pulse(pulse_probe, pulse_pin, PWM_PERIODS, PWM_FREQ,
                  duty=PWM_DUTY / 2 ** PWM_RESOLUTION)
    _assert_ds18x20(protomq, uid, onewire_pin)
    if with_analog:
        _assert_analog(protomq, dac_probe, uid, analog_pin,
                       float(board_definition.definition["mcuRefVoltage"]))
    _assert_i2c_events(protomq, uid, [(dev, types, value_range, space)
                                      for dev, types, value_range, space, fast
                                      in sensors if fast])
    _assert_no_component_errors(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_all(board, board_definition, secrets_name,
                          flashed_board, serial, protomq, probe, dac_probe,
                          analog_pin, pulse_probe, pulse_pin, onewire_pin,
                          i2c_devices, i2c_address_space, i2c_mux,
                          component_definition, checkin):
    _run_adds_all(board_definition, serial, protomq, probe, pulse_probe,
                  pulse_pin, onewire_pin, i2c_devices, i2c_address_space,
                  i2c_mux, component_definition, checkin,
                  dac_probe=dac_probe, analog_pin=analog_pin)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_all_without_dac(board, board_definition, secrets_name,
                                      flashed_board, serial, protomq, probe,
                                      pulse_probe, pulse_pin, onewire_pin,
                                      i2c_devices, i2c_address_space, i2c_mux,
                                      component_definition, checkin):
    """As test_checkin_adds_all, without the analog input, for a bench
    whose DAC isn't wired to the DUT."""
    _run_adds_all(board_definition, serial, protomq, probe, pulse_probe,
                  pulse_pin, onewire_pin, i2c_devices, i2c_address_space,
                  i2c_mux, component_definition, checkin)


# --- Stress: as many Adds as one checkin.Response can carry ---------------

# The MQTT buffer (WS_MQTT_BUFFER_SZ): the checkin.Response, its topic and
# MQTT's header must fit, so the largest payload is about BUF - 8 -
# len(topic). The DUT's UID is only known after the boot, so the topic is
# estimated with the longest UID suffix, and a margin left under it.
STRESS_MQTT_BUF = 2048
STRESS_UID_SUFFIX_MAX = 12
STRESS_MARGIN = 16
# The digital inputs' timer period (s), and how long every pin may take
# to report once after checkin
STRESS_DIGITAL_PERIOD = 10
STRESS_EVENT_TIMEOUT = 2 * STRESS_DIGITAL_PERIOD + 10
D_INPUT = 1


def _stress_digital_add(pin):
    """A digital.Add of pin as a plain input reporting on a timer: it
    drives nothing, so it's safe on every pin, wired or not."""
    return {"pin": pin, "gpioDirection": "D_INPUT", "sampleMode": "SM_TIMER",
            "period": STRESS_DIGITAL_PERIOD}


def _stress_response(board_definition, component_adds):
    """The checkin.Response the checkin fixture sends, as a B2D message."""
    definition = board_definition.definition
    return {"checkin": {"response": {
        "response": "R_OK",
        "totalGpioPins": len(definition["components"]["digitalPins"]),
        "totalAnalogPins": len(definition["components"]["analogPins"]),
        "referenceVoltage": definition["mcuRefVoltage"],
        "componentAdds": component_adds}}}


def _stress_max_size(board_definition, flashed_board):
    """The largest checkin.Response payload sure to fit the MQTT buffer."""
    user = json.loads(flashed_board.secrets_path.read_text())["io_username"]
    topic_max = (len(user) + len("/ws-b2d/") + len(board_definition.board_id)
                 + STRESS_UID_SUFFIX_MAX)
    return STRESS_MQTT_BUF - 8 - topic_max - STRESS_MARGIN


def _wait_digital_pins(protomq, uid, pins, mark, timeout):
    """Wait until every pin in pins has published a digital.event since
    mark; the pins that didn't."""
    deadline = time.monotonic() + timeout
    while True:
        seen = {e.get("pin") for e in protomq.d2b_since(uid, "digital.event",
                                                        mark)}
        missing = [p for p in pins if p not in seen]
        if not missing or time.monotonic() > deadline:
            return missing
        time.sleep(0.5)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_adds_stress(board, board_definition, secrets_name,
                             flashed_board, serial, protomq, probe,
                             onewire_pin, i2c_devices, i2c_address_space,
                             i2c_mux, component_definition, checkin):
    """One checkin.Response with as many Adds as fit the MQTT buffer: a
    digital input on every board pin (but the 1-Wire pin), the DS18x20,
    the mux and every I2C sensor wired to the DUT, then re-Adds of one
    digital input (a re-Add replaces the pin) up to the size limit. The
    DUT must apply every Add, finish checkin, and run them all."""
    # --- Arrange ------------------------------------------------------
    pins = [p["name"] for p in
            board_definition.definition["components"]["digitalPins"]
            if p["name"] != onewire_pin]
    sensors = _all_i2c(i2c_devices, i2c_address_space, i2c_mux,
                       component_definition)
    mux_add = {"descriptor": i2c.mux_descriptor(i2c_address_space, i2c_mux),
               "name": i2c_mux.type}
    base = {
        "digitalAdds": [_stress_digital_add(p) for p in pins],
        "ds18x20Adds": [_ds18x20_add(onewire_pin)],
        "i2cAdds": [mux_add] + [i2c.add_msg(dev, space, types)
                                for dev, types, _, space, _ in sensors],
    }

    # Pad with re-Adds of the last pin to the largest count that fits
    limit = _stress_max_size(board_definition, flashed_board)
    pad_pin = pins[-1]
    candidates = []
    for k in range(200):
        adds = dict(base, digitalAdds=base["digitalAdds"]
                    + [_stress_digital_add(pad_pin)] * k)
        candidates.append(adds)
    sizes = hil_protomq.encoded_sizes(
        [_stress_response(board_definition, a) for a in candidates])
    fitting = [i for i, n in enumerate(sizes) if n <= limit]
    assert fitting, (f"the distinct Adds alone are {sizes[0]} B, over the "
                     f"{limit} B limit")
    pad = fitting[-1]
    adds, size = candidates[pad], sizes[pad]
    digital = [a["pin"] for a in adds["digitalAdds"]]
    total = (len(digital) + len(adds["ds18x20Adds"]) + len(adds["i2cAdds"]))
    log.info("stress: %d Adds (%d digital incl. %d re-Adds of %s, 1 "
             "ds18x20, %d i2c) in %d B of %d B", total, len(digital), pad,
             pad_pin, len(adds["i2cAdds"]), size, limit)

    def expect_adds():
        # In field-number order: digital, ds18x20, then i2c
        for pin in digital:
            _expect_digital(serial, pin, D_INPUT)
        _expect_ds18x20(serial)
        _expect_mux(serial)
        for dev, _, _, _, _ in sensors:
            i2c.expect_driver_initialized(serial, dev)

    # --- Act ----------------------------------------------------------
    probe.release()
    boot = serial.mark()
    uid = checkin(adds, expect_adds)
    mark = 0

    # --- Assert -------------------------------------------------------
    # The whole response arrived in one piece, and no Add failed
    rx = [int(m.group(1)) for l in serial.lines(boot)
          if (m := re.search(r"^\[mqtt\] B2D rx len=(\d+) ", l.text))]
    assert size in rx, f"no B2D rx len={size}; got {rx}"
    added = sum(1 for l in serial.lines(boot)
                if l.text == "[digital] Added new pin:")
    assert added == len(digital), (f"{added} digital Adds logged, expected "
                                   f"{len(digital)}")
    serial.assert_none(r"component adds failed|ERROR \[RESET\]|"
                       r"larger than buffer size", since=boot)
    _assert_no_component_errors(protomq, uid)

    # Every component runs: each digital input reports on its timer, the
    # DS18x20 and the fast I2C sensors publish events
    missing = _wait_digital_pins(protomq, uid, pins, mark,
                                 STRESS_EVENT_TIMEOUT)
    assert not missing, (f"no digital.event within {STRESS_EVENT_TIMEOUT}s "
                         f"for {missing}")
    _assert_ds18x20(protomq, uid, onewire_pin)
    _assert_i2c_events(protomq, uid, [(dev, types, value_range, space)
                                      for dev, types, value_range, space, fast
                                      in sensors if fast])
    _assert_no_component_errors(protomq, uid)
