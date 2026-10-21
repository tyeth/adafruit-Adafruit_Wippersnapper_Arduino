"""
HIL tests for Adafruit Wippersnapper firmware's MQTT functionality.

Exercises:
- `src/Wippersnapper.cpp::NetworkFSM()`
- `src/Wippersnapper.cpp::connect()`
- `src/platforms/esp32_wifi.h`
"""

import json
import re

import pytest

# How long runNetFSM() may take to connect to WiFi, as in test_wifi.py.
WIFI_CONNECT_TIMEOUT = 150
# How long the 5 MQTT connect attempts may take before runNetFSM() gives
# up: each blinks the status LED, waits for the TLS handshake and CONNACK,
# and on failure waits 3s before retrying.
MQTT_CONNECT_TIMEOUT = 120
# The firmware's WDT timeout (WS_TIMEOUT_WDT): after a halt, the WDT
# reboots the board within this.
WS_TIMEOUT_WDT = 60


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_mqtt_connected(board, valid_secrets, broker_creds, secrets_name,
                        flashed_board, serial, power, protomq):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    holding the HIL WiFi network and IO credentials, from the $HIL_*
    #    variables, and the io_url and io_port of the ProtoMQ broker the
    #    protomq fixture started on this host. (Compared outside the
    #    assert so a failure doesn't print the credentials.)
    matches = (json.loads(flashed_board.secrets_path.read_text())
               == valid_secrets)
    assert matches

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the DUT's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. PrintDeviceInfo() shows the ProtoMQ broker
    serial.expect(rf"^Adafruit\.io URL: {re.escape(broker_creds.host)}$",
                  timeout=30)
    serial.expect(rf"^Adafruit\.io Port: {broker_creds.port}$", timeout=5)

    # 4. Provisioning passes and runNetFSM() connects to WiFi.
    serial.expect(r"^Running Network FSM\.\.\.$", timeout=30)
    serial.expect(r"^Connected to WiFi!$", timeout=WIFI_CONNECT_TIMEOUT)

    # 5. runNetFSM() connects to the broker, and connect() reports it.
    serial.expect(r"^Connecting to AIO MQTT \(attempt #0\)$", timeout=30)
    serial.expect(r"^Connected to Adafruit IO$",
                  timeout=MQTT_CONNECT_TIMEOUT)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_mqtt_client_connected_to_protomq(board, secrets_name, flashed_board,
                                          serial, power, device_uid, protomq):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and the good
    #    secrets, whose io_url and io_port are those of the ProtoMQ broker
    #    the protomq fixture started on this host, with no clients yet.

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the DUT's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. connect() prints "Device UID: io-wipper-<uid>", its MQTT client ID.
    uid = device_uid(timeout=60)

    # 4. It's printed before the WiFi connect, so the broker doesn't list
    #    the DUT yet: a session listed now would be from the boot after
    #    flashing, not this one.
    assert not protomq.is_connected(uid), f"{uid} is already on the broker"

    # 5. runNetFSM() connects to WiFi and the broker.
    serial.expect(r"^Connected to WiFi!$", timeout=WIFI_CONNECT_TIMEOUT)
    serial.expect(r"^Connected to Adafruit IO$",
                  timeout=MQTT_CONNECT_TIMEOUT)

    # 6. ProtoMQ lists the DUT as a connected client.
    protomq.expect_client(uid, timeout=60)


def expect_mqtt_gives_up(serial):
    """All 5 of runNetFSM()'s MQTT connect attempts fail, so it halts
    until the WDT reboots the board, before "Connected to Adafruit IO"."""
    serial.expect(r"^Connecting to AIO MQTT \(attempt #0\)$", timeout=30)
    serial.expect(r"^MQTT Connection Error: -?\d+$",
                  timeout=MQTT_CONNECT_TIMEOUT)
    serial.expect(r"^Connecting to AIO MQTT \(attempt #4\)$",
                  timeout=MQTT_CONNECT_TIMEOUT)
    serial.expect(r"ERROR: Unable to connect to Adafruit\.IO MQTT, rebooting "
                  r"soon\.\.\.$", timeout=MQTT_CONNECT_TIMEOUT)
    serial.assert_none(r"^Connected to Adafruit IO$")


@pytest.mark.parametrize("secrets_name", ["invalid_broker_url"], indirect=True)
def test_mqtt_invalid_broker_url(board, invalid_broker_url_secrets,
                                 broker_creds, secrets_name, flashed_board,
                                 serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and the good
    #    secrets, but with an io_url that doesn't resolve. (Compared
    #    outside the assert so a failure doesn't print the credentials.)
    secrets = json.loads(flashed_board.secrets_path.read_text())
    matches = secrets == invalid_broker_url_secrets
    assert matches

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the DUT's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. PrintDeviceInfo() shows the bad host, with the broker's port.
    serial.expect(rf"^Adafruit\.io URL: {re.escape(secrets['io_url'])}$",
                  timeout=30)
    serial.expect(rf"^Adafruit\.io Port: {broker_creds.port}$", timeout=5)

    # 4. Provisioning passes and runNetFSM() connects to WiFi.
    serial.expect(r"^Running Network FSM\.\.\.$", timeout=30)
    serial.expect(r"^Connected to WiFi!$", timeout=WIFI_CONNECT_TIMEOUT)

    # 5. The host never resolves, so every MQTT connect fails and the
    #    DUT halts before the check-in.
    expect_mqtt_gives_up(serial)


@pytest.mark.parametrize("secrets_name", ["invalid_broker_port"], indirect=True)
def test_mqtt_invalid_broker_port(board, invalid_broker_port_secrets,
                                  broker_creds, secrets_name, flashed_board,
                                  serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and the good
    #    secrets, but with an io_port nothing listens on at the broker's
    #    host. (Compared outside the assert so a failure doesn't print
    #    the credentials.)
    secrets = json.loads(flashed_board.secrets_path.read_text())
    matches = secrets == invalid_broker_port_secrets
    assert matches

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the DUT's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. PrintDeviceInfo() shows the broker's host, with the bad port.
    serial.expect(rf"^Adafruit\.io URL: {re.escape(broker_creds.host)}$",
                  timeout=30)
    serial.expect(rf"^Adafruit\.io Port: {secrets['io_port']}$", timeout=5)

    # 4. Provisioning passes and runNetFSM() connects to WiFi.
    serial.expect(r"^Running Network FSM\.\.\.$", timeout=30)
    serial.expect(r"^Connected to WiFi!$", timeout=WIFI_CONNECT_TIMEOUT)

    # 5. Nothing accepts the connection, so every MQTT connect fails and
    #    the DUT halts before the check-in.
    expect_mqtt_gives_up(serial)


@pytest.mark.slow
@pytest.mark.parametrize("secrets_name", ["invalid_broker_url"], indirect=True)
def test_mqtt_unreachable_wdt_reboot(board, invalid_broker_url_secrets,
                                     secrets_name, flashed_board, serial,
                                     power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and the good
    #    secrets, but with an io_url that doesn't resolve. (Compared
    #    outside the assert so a failure doesn't print the credentials.)
    matches = (json.loads(flashed_board.secrets_path.read_text())
               == invalid_broker_url_secrets)
    assert matches

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the DUT's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. The first boot came from the power cycle.
    serial.expect(r"^ESP Reset Reason: PowerOn$", timeout=30)

    # 4. Provisioning passes, runNetFSM() connects to WiFi, then gives up
    #    on the broker and halts, no longer feeding the WDT.
    serial.expect(r"^Running Network FSM\.\.\.$", timeout=30)
    serial.expect(r"^Connected to WiFi!$", timeout=WIFI_CONNECT_TIMEOUT)
    expect_mqtt_gives_up(serial)

    # 5. DUT should reboot within the WS_TIMEOUT_WDT (+15 seconds buffer) timeframe
    serial.expect(r"^rst:0x[0-9a-f]+ ", timeout=WS_TIMEOUT_WDT + 15)

    # 6. The firmware reports the task WDT as the reset reason.
    serial.expect(r"^ESP Reset Reason: WDT_Task$", timeout=30)
