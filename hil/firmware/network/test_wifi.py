"""
HIL tests for Adafruit Wippersnapper firmware's networking functionality.

Exercises:
- `src/Wippersnapper.cpp::NetworkFSM()`
- `src/Wippersnapper.cpp::connect()`
- `src/platforms/esp32_wifi.h`
"""

import json
import re

import pytest

# How long the 5 connect attempts may take before runNetFSM() gives up.
# Associating with the AP takes an unknown time: each attempt waits up to
# 20s in multi-network mode (_wifiMulti.run()) or 5s in single-network
# mode, plus the status LED blink, so allow 5 x 20s with headroom.
WIFI_CONNECT_TIMEOUT = 150


def ssid_found_pattern(ssid):
    """The scan line when check_valid_ssid() returns
    WS_SSID_SCAN_SSID_FOUND: the SSID is printed on the scan's line."""
    return (r"^Performing a WiFi scan for SSID\.\.\."
            rf"SSID \({re.escape(ssid)}\) found! RSSI: -?\d+$")


@pytest.mark.parametrize("secrets_name", ["ssid_valid"], indirect=True)
def test_ssid_found(board, ssid_valid_secrets, network_creds, secrets_name,
                    flashed_board, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    naming the HIL WiFi network, from the $HIL_WIFI_* variables, with
    #    fake IO credentials that pass provisioning. (Compared outside the
    #    assert so a failure doesn't print the credentials.)
    matches = (json.loads(flashed_board.secrets_path.read_text())
               == ssid_valid_secrets)
    assert matches

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. Provisioning passes and runNetFSM() scans for the SSID.
    serial.expect(r"^Running Network FSM\.\.\.$", timeout=30)

    # 4. check_valid_ssid() returns WS_SSID_SCAN_SSID_FOUND, printing the
    #    SSID on the scan's line.
    serial.expect(ssid_found_pattern(network_creds.ssid), timeout=30)

    # 5. None of the scan's halts fired, and runNetFSM() goes on to
    #    connect (test_wifi_connected covers the connection).
    serial.expect(r"^Connecting to WiFi \(attempt #0\)$", timeout=10)
    serial.assert_none(r"ERROR: (No WiFi networks found|Unable to find WiFi "
                       r"network|No network interface defined|Unknown WiFi "
                       r"scan result)")


@pytest.mark.parametrize("secrets_name", ["ssid_valid"], indirect=True)
def test_wifi_connected(board, ssid_valid_secrets, network_creds, secrets_name,
                        flashed_board, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    naming the HIL WiFi network, from the $HIL_WIFI_* variables, with
    #    fake IO credentials that pass provisioning. (Compared outside the
    #    assert so a failure doesn't print the credentials.)
    matches = (json.loads(flashed_board.secrets_path.read_text())
               == ssid_valid_secrets)
    assert matches

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the DUT's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. Provisioning passes and the scan finds the SSID.
    serial.expect(r"^Running Network FSM\.\.\.$", timeout=30)
    serial.expect(ssid_found_pattern(network_creds.ssid), timeout=30)

    # 4. Leave the DUT alone through its connect attempts until it
    #    either connects (FSM_NET_CHECK_NETWORK sees WS_NET_CONNECTED) or
    #    gives up after the 5th attempt.
    m = serial.expect(r"^(Connected to WiFi!|ERROR: Unable to connect to "
                      r"WiFi!)$", timeout=WIFI_CONNECT_TIMEOUT)

    # 5. It connected.
    assert m.group(1) == "Connected to WiFi!", m.string


@pytest.mark.parametrize("secrets_name", ["invalid_password"], indirect=True)
def test_wifi_invalid_password(board, invalid_password_secrets, network_creds,
                               secrets_name, flashed_board, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    naming the HIL WiFi network with a wrong password, and fake IO
    #    credentials that pass provisioning. (Compared outside the assert
    #    so a failure doesn't print the credentials.)
    matches = (json.loads(flashed_board.secrets_path.read_text())
               == invalid_password_secrets)
    assert matches

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the DUT's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. Provisioning passes and the scan finds the SSID: the password
    #    isn't checked until the connect.
    serial.expect(r"^Running Network FSM\.\.\.$", timeout=30)
    serial.expect(ssid_found_pattern(network_creds.ssid), timeout=30)

    # 4. All 5 connect attempts fail, so runNetFSM() gives up.
    serial.expect(r"^Connecting to WiFi \(attempt #4\)$",
                  timeout=WIFI_CONNECT_TIMEOUT)
    serial.expect(r"^ERROR: Unable to connect to WiFi!$",
                  timeout=WIFI_CONNECT_TIMEOUT)

    # 5. DUT halts until the WDT reboots the board.
    serial.expect(r"ERROR: Unable to connect to WiFi, rebooting soon\.\.\.$",
                  timeout=10)

    # 6. DUT never connected.
    serial.assert_none(r"^Connected to WiFi!$")


@pytest.mark.parametrize("secrets_name", ["ssid_not_found"], indirect=True)
def test_ssid_not_found(board, generated_secrets, secrets_name, flashed_board,
                        serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    that pass provisioning but name a WiFi network that doesn't exist.
    assert (json.loads(flashed_board.secrets_path.read_text())
            == generated_secrets[secrets_name])

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. Provisioning passes and runNetFSM() scans for the SSID.
    serial.expect(r"^Running Network FSM\.\.\.$", timeout=30)
    serial.expect(r"^Performing a WiFi scan for SSID\.\.\.", timeout=5)

    # 4. check_valid_ssid() returns false, so runNetFSM() halts until the
    #    WDT reboots the board.
    serial.expect(r"ERROR: Unable to find WiFi network, rebooting soon\.\.\.$",
                  timeout=30)

    # 5. It halted before trying to connect.
    serial.assert_none(r"^Connecting to WiFi \(attempt")


@pytest.mark.parametrize("secrets_name", ["ssid_not_found"], indirect=True)
def test_hil_hides_ssid_scan_results(board, generated_secrets, secrets_name,
                                     flashed_board, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the HIL firmware (built with
    #    WS_HIL_BUILD) and secrets that pass provisioning but name a WiFi
    #    network that doesn't exist.
    assert (json.loads(flashed_board.secrets_path.read_text())
            == generated_secrets[secrets_name])

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port, checking that it really reset
    #    (its serial port went away and came back), and capture the boot.
    serial.port_reset(board.entry, timeout=15, confirm_off=True, **power)

    # --- Assert -------------------------------------------------------
    # 3. Wait for runNetFSM()'s halt: check_valid_ssid() has returned from
    #    the not-found branch, where non-HIL builds dump every nearby
    #    network, so the dump would already be in the log.
    serial.expect(r"ERROR: Unable to find WiFi network, rebooting soon\.\.\.$",
                  timeout=60)

    # 4. The scan results weren't printed: HIL serial logs are public, so
    #    nearby (neighbors') SSIDs must stay out of them. Match only the
    #    list's heading so a regression's failure message doesn't itself
    #    print an SSID.
    serial.assert_none(r"WipperSnapper found these WiFi networks")
