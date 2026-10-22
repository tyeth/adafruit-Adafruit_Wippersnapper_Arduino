"""
HIL tests for Adafruit Wippersnapper firmware's digitalIO component: what
the DUT does with the broker's digital.B2D messages.

Exercises:
- `src/components/digitalIO/controller.cpp::Router()`
- `src/components/digitalIO/controller.cpp::Handle_DigitalIO_Add()`
"""

import pytest

# How long runNetFSM() may take to connect to WiFi and the broker, as in
# test_mqtt.py.
WIFI_CONNECT_TIMEOUT = 150
MQTT_CONNECT_TIMEOUT = 120
# How long after connecting the DUT may take to send its checkin.Request,
# and to finish the checkin once the broker answers.
CHECKIN_TIMEOUT = 30


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_digital_add_output(board, board_definition, secrets_name,
                            flashed_board, serial, power, device_uid, protomq):
    # --- Arrange ------------------------------------------------------
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

    # A digital.Add for D14 as an output, leaving the sample mode, period,
    # initial write and inversion empty
    add = {"pin": "D14", "gpioDirection": "D_OUTPUT"}

    # --- Act ----------------------------------------------------------
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
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

    # Broker sends the digital.Add for D14, once the DUT is fully checked in
    protomq.publish(uid, {"digital": {"add": add}})
    protomq.expect_b2d_message(uid, "digital.add", match=add, timeout=5)

    # DUT adds D14 as an output (D_OUTPUT = 3)
    serial.expect(r"^\[dio\] Added new pin:$", timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^Pin Name: 14$", timeout=5)
    serial.expect(r"^Direction: 3$", timeout=5)
