"""
Shared helpers for the digitalIO HIL tests (test_digital_out.py,
test_digital_in.py).
"""

# How long runNetFSM() may take to connect to WiFi and the broker, as in
# test_mqtt.py.
WIFI_CONNECT_TIMEOUT = 150
MQTT_CONNECT_TIMEOUT = 120
# How long after connecting the DUT may take to send its checkin.Request,
# and to finish the checkin once the broker answers.
CHECKIN_TIMEOUT = 30


def checkin(board, board_definition, serial, power, device_uid, protomq):
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

    # Capture the DUT's UID and tell the broker to track its messages.
    # Not asserting that it isn't on the broker yet: if the DUT reconnected
    # to this test's broker before the port reset cut its power, the broker
    # keeps that dead session until the DUT reconnects and takes it over.
    # The checkin.Request below shows the connection is fresh. If this
    # race causes trouble again, power the DUT off before ProtoMQ starts.
    uid = device_uid(timeout=60)
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
