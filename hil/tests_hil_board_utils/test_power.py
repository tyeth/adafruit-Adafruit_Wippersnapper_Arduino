"""Finding the board's serial port from its hub location, and switching the
hub port's power with uhubctl."""

import os
import re
import time

import pytest

import hil_board_utils as hbu


def wait_port_gone(entry, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        try:
            port = hbu.find_port(entry, refresh=True)
        except hbu.PortError:
            return
        assert time.monotonic() < deadline, f"{port} still present after power off"
        time.sleep(0.2)


def port_status(entry, power):
    """The status words uhubctl reports for the board's hub port, e.g.
    ['power', 'enable', 'connect'] or ['off']."""
    loc = entry["location"]
    out = hbu.run_uhubctl(["-l", loc["hub"], "-p", str(loc["port"])], **power)
    # uhubctl also lists the USB3 companion hub; keep only the board's hub.
    section = re.search(rf"^Current status for hub {re.escape(loc['hub'])} .*?(?=^Current|\Z)",
                        out, re.M | re.S)
    assert section, f"hub {loc['hub']} not in uhubctl output:\n{out}"
    m = re.search(rf"^\s*Port {loc['port']}: [0-9a-f]{{4}} (.*)$", section.group(0), re.M)
    assert m, f"port {loc['port']} not in uhubctl output:\n{out}"
    return m.group(1).split(" [", 1)[0].split()


# --- find_port (board needed, no power switching) -------------------------


@pytest.mark.hardware
def test_find_port_matches_location(board_entry):
    hbu.clear_port()
    port = hbu.find_port(board_entry)
    assert port.startswith("/dev/tty")
    loc = board_entry["location"]
    path = hbu.usb_device_path(loc["hub"], loc["port"])
    assert hbu.match_serial_ports(hbu.list_serial_ports(), path) == [port]
    assert os.access(port, os.R_OK | os.W_OK), f"no permission to open {port}"


@pytest.mark.hardware
def test_find_port_is_cached(board_entry, port):
    assert hbu.get_port(board_entry["board"]) == port
    hbu._port_cache[board_entry["board"]] = "/dev/cached"
    try:
        assert hbu.find_port(board_entry) == "/dev/cached"
        assert hbu.find_port(board_entry, refresh=True) == port
    finally:
        hbu.clear_port(board_entry["board"])
    with pytest.raises(hbu.PortError, match="not resolved"):
        hbu.get_port(board_entry["board"])


def test_find_port_empty_location(board_entry):
    entry = {**board_entry, "location": {"hub": "9-9.9", "port": 4}}
    with pytest.raises(hbu.PortError, match="no serial port"):
        hbu.find_port(entry, refresh=True)


# --- uhubctl --------------------------------------------------------------


def test_find_uhubctl(power):
    tool = hbu.find_uhubctl(power["uhubctl"]) if power["uhubctl"] else hbu.find_uhubctl()
    assert os.access(tool, os.X_OK)


@pytest.mark.hardware
def test_uhubctl_reports_board_port(board_entry, power):
    assert "power" in port_status(board_entry, power)


@pytest.mark.hardware
def test_power_off_and_on(board_entry, power, port):
    loc = board_entry["location"]
    try:
        hbu._power_off(loc["hub"], loc["port"], **power)
        assert port_status(board_entry, power)[0] == "off"
        wait_port_gone(board_entry)
    finally:
        hbu._power_on(loc["hub"], loc["port"], **power)
    assert "power" in port_status(board_entry, power)
    new_port = hbu.wait_for_port(board_entry)
    assert hbu.get_port(board_entry["board"]) == new_port


@pytest.mark.hardware
def test_power_cycle_board(board_entry, power, port):
    start = time.monotonic()
    hbu.power_cycle_board(board_entry, delay=2, **power)
    assert time.monotonic() - start >= 2  # uhubctl held the port off
    # The stale port is forgotten until the board is looked up again.
    with pytest.raises(hbu.PortError, match="not resolved"):
        hbu.get_port(board_entry["board"])
    new_port = hbu.wait_for_port(board_entry, timeout=15)
    assert os.access(new_port, os.R_OK | os.W_OK)


def test_wait_for_port_times_out(board_entry):
    entry = {**board_entry, "location": {"hub": "9-9.9", "port": 4}}
    start = time.monotonic()
    with pytest.raises(hbu.PortError, match="no serial port"):
        hbu.wait_for_port(entry, timeout=1, interval=0.2)
    assert 1 <= time.monotonic() - start < 3
