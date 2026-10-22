"""
HIL tests for Adafruit Wippersnapper firmware's checkin: the DUT's
checkin.Request and what it does with the broker's checkin.Response.

Exercises:
- `src/wippersnapper.cpp::connect()`
- `src/wippersnapper.cpp::handleCheckinResponse()`
- `src/components/checkin/model.cpp`
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
def test_checkin_response_ok(board, board_definition, secrets_name,
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

    # DUT sends checkin.Request, broker should receive it
    protomq.expect_d2b_message(
        uid, "checkin.request",
        match={"hardwareUid": board_definition.board_id},
        timeout=CHECKIN_TIMEOUT)

    # Broker responds with the expected checkin.Response
    protomq.expect_b2d_message(uid, "checkin.response",
                               match=response, timeout=5)

    # DUT sends checkin.Complete, only after it decoded and accepted the
    # checkin.Response within handleCheckinResponse()
    protomq.expect_d2b_message(uid, "checkin.complete",
                               timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^Completed checkin process!$", timeout=CHECKIN_TIMEOUT)


def check_checkin_rejected(response_code, board, board_definition, serial,
                           power, device_uid, protomq):
    """The DUT rejects a checkin.Response holding response_code, a
    non-R_OK checkin.Response.Response, and never completes the checkin."""
    # --- Arrange ------------------------------------------------------
    # Create a checkin.Response that isn't R_OK
    response = {"response": response_code}
    protomq.enable_checkin_fallback(False)
    protomq.set_autorespond(
        "checkin.request", {"checkin": {"response": response}},
        match={"hardwareUid": board_definition.board_id})

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

    # DUT sends checkin.Request, broker should receive it
    protomq.expect_d2b_message(
        uid, "checkin.request",
        match={"hardwareUid": board_definition.board_id},
        timeout=CHECKIN_TIMEOUT)

    # Broker responds with the non-R_OK checkin.Response
    protomq.expect_b2d_message(uid, "checkin.response",
                               match=response, timeout=5)

    # DUT rejects the checkin.Response within CheckinModel::ProcessResponse()
    serial.expect(r"^\[checkin\] ERROR: Invalid checkin_Response!$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^ERROR: Unable to decode Checkin Response message$",
                  timeout=5)

    # DUT never sends checkin.Complete, nor finishes the checkin
    protomq.assert_no_d2b_message(uid, "checkin.complete",
                                  within=CHECKIN_TIMEOUT)
    serial.assert_none(r"^Completed checkin process!$")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_response_board_not_found(board, board_definition,
                                          secrets_name, flashed_board, serial,
                                          power, device_uid, protomq):
    check_checkin_rejected("R_BOARD_NOT_FOUND", board, board_definition,
                           serial, power, device_uid, protomq)


@pytest.mark.skip(reason="waiting to test on new firmware")
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_checkin_response_unspecified(board, board_definition, secrets_name,
                                      flashed_board, serial, power,
                                      device_uid, protomq):
    check_checkin_rejected("R_UNSPECIFIED", board, board_definition, serial,
                           power, device_uid, protomq)
