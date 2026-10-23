"""
HIL tests for Adafruit Wippersnapper firmware's digitalIO component: what
the DUT does with the broker's digital.B2D messages.

Exercises:
- `src/components/digitalIO/controller.cpp::Router()`
- `src/components/digitalIO/controller.cpp::Handle_DigitalIO_Add()`
- `src/components/digitalIO/controller.cpp::Handle_DigitalIO_Write()`
- `src/components/digitalIO/controller.cpp::Handle_DigitalIO_Remove()`
"""

import pytest

# How long runNetFSM() may take to connect to WiFi and the broker, as in
# test_mqtt.py.
WIFI_CONNECT_TIMEOUT = 150
MQTT_CONNECT_TIMEOUT = 120
# How long after connecting the DUT may take to send its checkin.Request,
# and to finish the checkin once the broker answers.
CHECKIN_TIMEOUT = 30
# How long the DUT's pin may take to reach a written value, read by the
# probe, from entering expect_digitalout_transition().
PIN_TRANSITION_TIMEOUT = 2


def _checkin(board, board_definition, serial, power, device_uid, protomq):
    """Reset the DUT and see it through the checkin to its app loop.
    Returns its UID."""
    # Create the expected checkin.Response based on the board definition
    definition = board_definition.definition
    response = {
        "response": "R_OK",
        "totalGpioPins": len(definition["components"]["digitalPins"]),
        "totalAnalogPins": len(definition["components"]["analogPins"]),
        "referenceVoltage": definition["mcuRefVoltage"],
    }
    protomq.enable_checkin_fallback(False)
    protomq.set_autorespond(
        "checkin.request", {"checkin": {"response": response}},
        match={"hardwareUid": board_definition.board_id})

    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # Capture the DUT's UID and tell the broker to track its messages
    uid = device_uid(timeout=60)
    assert not protomq.is_connected(uid), f"{uid} is already on the broker"
    protomq.track(uid)

    # Ensure runNetFSM() connects to WiFi and the broker
    serial.expect(r"^Connected to WiFi!$", timeout=WIFI_CONNECT_TIMEOUT)
    serial.expect(r"^Connected to Adafruit IO$", timeout=MQTT_CONNECT_TIMEOUT)
    protomq.expect_client(uid, timeout=5)

    # DUT checks in with the broker
    protomq.expect_d2b_message(
        uid, "checkin.request",
        match={"hardwareUid": board_definition.board_id},
        timeout=CHECKIN_TIMEOUT)
    protomq.expect_b2d_message(uid, "checkin.response",
                               match=response, timeout=5)
    protomq.expect_d2b_message(uid, "checkin.complete",
                               timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^Completed checkin process!$", timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^Running app loop\.\.\.$", timeout=CHECKIN_TIMEOUT)
    return uid


def _add_output(serial, protomq, uid, pin):
    """Send a digital.Add for pin as an output, leaving the sample mode,
    period, initial write and inversion empty, and expect the DUT to add
    it."""
    add = {"pin": pin, "gpioDirection": "D_OUTPUT"}
    protomq.publish(uid, {"digital": {"add": add}})
    protomq.expect_b2d_message(uid, "digital.add", match=add, timeout=5)

    # DUT adds the pin as an output (D_OUTPUT = 3)
    serial.expect(r"^\[dio\] Added new pin:$", timeout=CHECKIN_TIMEOUT)
    serial.expect(rf"^Pin Name: {pin.lstrip('D')}$", timeout=5)
    serial.expect(r"^Direction: 3$", timeout=5)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_digital_add_output(board, board_definition, secrets_name,
                            flashed_board, serial, power, device_uid, protomq):
    # DUT checks in, then the broker sends the digital.Add for D14 once
    # the DUT is fully checked in
    uid = _checkin(board, board_definition, serial, power, device_uid, protomq)
    _add_output(serial, protomq, uid, "D14")


def _write(serial, protomq, probe, uid, pin, value):
    """Send a digital.Write of value to pin, an output, and expect the probe
    to see it go from the other value to value."""
    write = {"pin": pin, "value": {"boolValue": value}}
    # The probe first checks that pin is at the other value, then samples
    # it while the broker sends the digital.Write, and finally asserts it
    # reached value
    with probe.expect_digitalout_transition(
            pin, not value, value, timeout=PIN_TRANSITION_TIMEOUT):
        protomq.publish(uid, {"digital": {"write": write}})
        serial.expect(rf"^\[dio\] Writing: {int(value)} to Pin {pin.lstrip('D')}$",
                      timeout=5)


def _remove(serial, protomq, uid, pin):
    """Send a digital.Remove for pin and expect the DUT to remove it."""
    protomq.publish(uid, {"digital": {"remove": {"pin": pin}}})
    serial.expect(rf"^\[dio\] Pin removed: {pin.lstrip('D')}$", timeout=5)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_digital_write_output_low_high(board, board_definition, secrets_name,
                                       flashed_board, serial, power, device_uid,
                                       protomq, probe):
    # --- Arrange ------------------------------------------------------
    # DUT checks in and adds D14 as an output, which starts low
    uid = _checkin(board, board_definition, serial, power, device_uid, protomq)
    _add_output(serial, protomq, uid, "D14")

    # --- Act / Assert -------------------------------------------------
    # D14 goes low -> high
    _write(serial, protomq, probe, uid, "D14", True)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_digital_write_output_high_low(board, board_definition, secrets_name,
                                       flashed_board, serial, power, device_uid,
                                       protomq, probe):
    # --- Arrange ------------------------------------------------------
    # DUT checks in and adds D14 as an output, which starts low, then the
    # broker drives it high
    uid = _checkin(board, board_definition, serial, power, device_uid, protomq)
    _add_output(serial, protomq, uid, "D14")
    _write(serial, protomq, probe, uid, "D14", True)

    # --- Act / Assert -------------------------------------------------
    # D14 goes high -> low
    _write(serial, protomq, probe, uid, "D14", False)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_digital_add_write_remove(board, board_definition, secrets_name,
                                  flashed_board, serial, power, device_uid,
                                  protomq, probe):
    # --- Arrange ------------------------------------------------------
    # DUT checks in, adds D14 as an output and the broker drives it high
    uid = _checkin(board, board_definition, serial, power, device_uid, protomq)
    _add_output(serial, protomq, uid, "D14")
    _write(serial, protomq, probe, uid, "D14", True)

    # --- Act / Assert -------------------------------------------------
    # DUT removes D14
    _remove(serial, protomq, uid, "D14")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_digital_add_write_remove_write(board, board_definition, secrets_name,
                                        flashed_board, serial, power,
                                        device_uid, protomq, probe):
    # --- Arrange ------------------------------------------------------
    # DUT checks in, adds D14 as an output, the broker drives it high and
    # then removes it
    uid = _checkin(board, board_definition, serial, power, device_uid, protomq)
    _add_output(serial, protomq, uid, "D14")
    _write(serial, protomq, probe, uid, "D14", True)
    _remove(serial, protomq, uid, "D14")

    # --- Act ----------------------------------------------------------
    # The broker writes to the removed D14
    write = {"pin": "D14", "value": {"boolValue": True}}
    protomq.publish(uid, {"digital": {"write": write}})

    # --- Assert -------------------------------------------------------
    # D14's DigitalIOHardware was deleted by the remove, so GetPin() finds
    # nothing and Handle_DigitalIO_Write() rejects the write
    protomq.expect_d2b_message(
        uid, "error.component",
        match={"pin": "D14", "message": "Failed to find pin"}, timeout=5)
