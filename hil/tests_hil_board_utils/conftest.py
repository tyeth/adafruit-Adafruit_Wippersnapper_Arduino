"""Fixtures for the hil_board_utils tests, which run against the board in the
board file (hil-boards.json by default), like hil_power_cycle_monitor.py.

    pytest tests_hil_board_utils                    # everything
    pytest tests_hil_board_utils -m "not hardware"  # no board needed
    pytest tests_hil_board_utils --secrets ~/secrets.json -s

Tests marked 'hardware' flash, reset, and power-cycle the board.
"""

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

HIL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HIL_DIR))
import hil_board_utils as hbu  # noqa: E402
import hil_power_cycle_monitor as hpcm  # noqa: E402


def pytest_addoption(parser):
    group = parser.getgroup("hil")
    group.addoption("--boards", type=Path, default=hpcm.DEFAULT_BOARDS,
                    help=f"board file (default: {hpcm.DEFAULT_BOARDS.name})")
    group.addoption("--board", help="board name, if the board file lists several")
    group.addoption("--firmware-dir", type=Path,
                    help="dir with *.combined.bin and *.partitions.bin "
                         "(default: the board entry's firmware_dir)")
    group.addoption("--chip", default="esp32", help="esptool chip (default: esp32)")
    group.addoption("--secrets", type=Path,
                    help="secrets.json for the end-to-end test (default: generated)")
    group.addoption("--uhubctl", help="path to uhubctl, used if not found on PATH")
    group.addoption("--sudo", action="store_true", help="run uhubctl under 'sudo -n'")
    group.addoption("--serial-log-dir", type=Path,
                    help="write each test's serial capture here (default: tmp_path)")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "hardware: needs the board from the board file attached"
    )


@dataclass(frozen=True)
class Firmware:
    combined: Path
    partitions: Path


@pytest.fixture(scope="session")
def board_entry(pytestconfig):
    try:
        return hpcm.load_board_entry(pytestconfig.getoption("boards"),
                                     pytestconfig.getoption("board"))
    except hbu.HilBoardUtilsError as e:
        pytest.fail(str(e), pytrace=False)


def _find_one(directory, pattern):
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        pytest.fail(f"expected one {pattern} in {directory}, found {len(matches)}",
                    pytrace=False)
    return matches[0]


@pytest.fixture(scope="session")
def firmware(pytestconfig, board_entry):
    directory = pytestconfig.getoption("firmware_dir") or board_entry.get("firmware_dir")
    if not directory:
        pytest.fail("no firmware dir: pass --firmware-dir or set firmware_dir "
                    "in the board entry", pytrace=False)
    directory = Path(directory)
    return Firmware(_find_one(directory, "*.combined.bin"),
                    _find_one(directory, "*.partitions.bin"))


@pytest.fixture(scope="session")
def chip(pytestconfig):
    return pytestconfig.getoption("chip")


@pytest.fixture(scope="session")
def power(pytestconfig):
    """uhubctl/sudo keyword arguments for the hbu power functions."""
    return {"uhubctl": pytestconfig.getoption("uhubctl"),
            "sudo": pytestconfig.getoption("sudo")}


@pytest.fixture
def port(board_entry):
    """The board's tty, looked up fresh since other tests power-cycle it."""
    return hbu.find_port(board_entry, refresh=True)


@pytest.fixture
def make_secrets(tmp_path):
    """Write a valid secrets.json with a unique username and SSID, which the
    firmware echoes at boot. Returns (path, secrets)."""
    def make(**overrides):
        token = uuid.uuid4().hex[:8]
        secrets = {
            "io_username": f"hil-user-{token}",
            "io_key": "aio_hil_test_key",
            "network_type_wifi": {"network_ssid": f"hil-ssid-{token}",
                                  "network_password": "hil-password"},
            "status_pixel_brightness": 0.2,
        }
        secrets.update(overrides)
        path = tmp_path / f"secrets-{token}.json"
        path.write_text(json.dumps(secrets, indent=2))
        return path, secrets
    return make


@pytest.fixture
def open_monitor(request, pytestconfig, tmp_path):
    """Open SerialMonitors; each is closed and dumped when the test ends."""
    monitors = []

    def open_(port):
        mon = hbu.SerialMonitor(port)
        monitors.append(mon)
        return mon

    yield open_
    log_dir = pytestconfig.getoption("serial_log_dir") or tmp_path
    for i, mon in enumerate(monitors):
        mon.close()
        suffix = f"-{i}" if len(monitors) > 1 else ""
        mon.dump(Path(log_dir) / f"{request.node.name}{suffix}.serial.log")
