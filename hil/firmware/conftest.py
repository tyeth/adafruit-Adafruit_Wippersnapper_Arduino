"""Fixtures for the firmware HIL tests, which run against the board in the
board file (hil-boards.json by default).

    pytest firmware/provisioning/
    pytest firmware -k io_user
    pytest firmware --secrets-dir ~/hil-secrets --log-cli-level=INFO

Fixture chain:

    board (session) -> flashed_board (function) -> serial (function)
                                                -> device_uid (function)

Every test starts from a clean chip: flashed_board erases the whole flash
(firmware, NVS, filesystem), then flashes the firmware and a LittleFS image
holding the test's secrets scenario, before the test body runs.

Secrets come from a directory of <scenario>.secrets.json files (--secrets-dir,
else $HIL_SECRETS_DIR, else ~/hil-secrets). good.secrets.json is the default
scenario and the source of broker_creds and network_creds. Other scenarios
may be invalid on purpose, so they're packed byte for byte and never
validated. The scenarios in generated_secrets (e.g. "default", the firmware's
placeholder secrets) aren't read from the dir: they're written to a temp
file. Two scenarios have no secrets file at all: "no_secrets_file" flashes
an empty LittleFS image, and "no_fs_image" flashes no image, leaving the fs
partition erased. A module picks a scenario by overriding secrets_name:

    @pytest.fixture
    def secrets_name():
        return "bad_wifi"

and a single test by parametrizing it indirectly:

    @pytest.mark.parametrize("secrets_name", ["default"], indirect=True)

The board entry picks which tests run on it, with selectors relative to
this dir: a folder ("provisioning/"), a file ("provisioning/test_secrets.py")
or a test ("provisioning/test_secrets.py::test_default_secrets"):

    "run_tests": ["provisioning/"],   # only these run; omit to run all
    "skip_tests": []                  # these are reported as skipped

Tests outside run_tests are deselected. Selection on the command line
(paths, -k) narrows this further; it can't widen it.
"""

import copy
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

FIRMWARE_DIR = Path(__file__).resolve().parent
HIL_DIR = FIRMWARE_DIR.parent
sys.path.insert(0, str(HIL_DIR))
import hil_board_utils  # noqa: E402

DEFAULT_BOARDS = HIL_DIR / "hil-boards.json"
DEFAULT_SECRETS_DIR = Path.home() / "hil-secrets"
SECRETS_DIR_ENV = "HIL_SECRETS_DIR"
SECRETS_SUFFIX = ".secrets.json"
GOOD_SECRETS = "good"
DEFAULT_SECRETS = "default"
NO_SECRETS_FILE = "no_secrets_file"  # an empty LittleFS image
NO_FS_IMAGE = "no_fs_image"  # no image: the fs partition stays erased
DEVICE_UID_RE = re.compile(r"^Device UID: (io-wipper-\S+)$")

log = logging.getLogger("hil.firmware")


def pytest_addoption(parser):
    group = parser.getgroup("hil")
    group.addoption("--boards", type=Path, default=DEFAULT_BOARDS,
                    help=f"board file (default: {DEFAULT_BOARDS.name})")
    group.addoption("--board", help="board name, if the board file lists several")
    group.addoption("--firmware-dir", type=Path,
                    help="dir with *.combined.bin and *.partitions.bin "
                         "(default: the board entry's firmware_dir)")
    group.addoption("--secrets-dir", type=Path,
                    help=f"dir of <scenario>{SECRETS_SUFFIX} files "
                         f"(default: ${SECRETS_DIR_ENV}, else {DEFAULT_SECRETS_DIR})")
    group.addoption("--uhubctl", help="path to uhubctl, used if not found on PATH")
    group.addoption("--sudo", action="store_true", help="run uhubctl under 'sudo -n'")
    group.addoption("--serial-log-dir", type=Path,
                    help="write each test's serial capture here (default: tmp_path)")


# --- Board and firmware ---------------------------------------------------


@dataclass(frozen=True)
class Board:
    name: str
    entry: dict
    chip: str
    port: str
    flash_baud: int  # esptool baud rate for flashing
    read_baud: int   # SerialMonitor baud rate for the firmware's output


@dataclass(frozen=True)
class Firmware:
    combined: Path
    partitions: Path


def load_board_entry(path, board=None):
    """Read a board entry from path, which holds one entry or a list of them."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        pytest.fail(f"cannot read board file {path}: {e}", pytrace=False)
    entries = data if isinstance(data, list) else [data]
    if board is None:
        if len(entries) != 1:
            pytest.fail(f"{path} has {len(entries)} boards; pick one with --board",
                        pytrace=False)
        return entries[0]
    matches = [e for e in entries if isinstance(e, dict) and e.get("board") == board]
    if not matches:
        pytest.fail(f"board '{board}' not found in {path}", pytrace=False)
    return matches[0]


def _test_selectors(entry, key):
    """The board entry's run_tests/skip_tests list, with trailing '/'
    stripped, or None if it isn't set."""
    selectors = entry.get(key)
    if selectors is None:
        return None
    if (not isinstance(selectors, list)
            or not all(isinstance(s, str) and s.strip("/") for s in selectors)):
        raise pytest.UsageError(
            f"board '{entry.get('board')}': '{key}' must be a list of test paths, "
            f"e.g. [\"provisioning/\"], got {selectors!r}")
    return [s.rstrip("/") for s in selectors]


def _matches(test_id, selectors):
    """Whether test_id (relative to FIRMWARE_DIR) is in, or is, one of
    selectors: a folder, a file, or a test with or without its params."""
    return any(test_id == s or test_id.startswith((s + "/", s + "::", s + "["))
               for s in selectors)


def pytest_collection_modifyitems(config, items):
    """Apply the board entry's run_tests and skip_tests to the tests under
    FIRMWARE_DIR."""
    try:
        entry = load_board_entry(config.getoption("boards"),
                                 config.getoption("board"))
    except pytest.fail.Exception as e:
        raise pytest.UsageError(str(e)) from None
    run = _test_selectors(entry, "run_tests")
    skip = _test_selectors(entry, "skip_tests")
    selected, deselected = [], []
    for item in items:
        path = Path(item.path)
        if not path.is_relative_to(FIRMWARE_DIR):
            selected.append(item)
            continue
        test_id = path.relative_to(FIRMWARE_DIR).as_posix()
        if "::" in item.nodeid:
            test_id += "::" + item.nodeid.split("::", 1)[1]
        if run is not None and not _matches(test_id, run):
            deselected.append(item)
            continue
        if skip and _matches(test_id, skip):
            item.add_marker(pytest.mark.skip(
                reason=f"in skip_tests for board '{entry['board']}'"))
        selected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture(scope="session")
def board(pytestconfig):
    entry = load_board_entry(pytestconfig.getoption("boards"),
                             pytestconfig.getoption("board"))
    try:
        hil_board_utils.validate_board_entry(entry)
        chip = entry.get("chip")
        if not isinstance(chip, str):
            raise hil_board_utils.PortError(
                f"board '{entry['board']}': 'chip' must be set, e.g. \"esp32\"")
        hil_board_utils.esp_bootloader_offset(chip)  # rejects unknown chips
        bauds = {key: _require_baud(entry, key) for key in ("flash_baud", "read_baud")}
        port = hil_board_utils.find_port(entry, refresh=True)
    except hil_board_utils.HilBoardUtilsError as e:
        pytest.fail(str(e), pytrace=False)
    return Board(entry["board"], entry, chip, port, **bauds)


def _require_baud(entry, key):
    baud = entry.get(key)
    if isinstance(baud, bool) or not isinstance(baud, int) or baud <= 0:
        raise hil_board_utils.PortError(
            f"board '{entry['board']}': '{key}' must be a positive integer, "
            f"got {baud!r}")
    return baud


@pytest.fixture(scope="session")
def power(pytestconfig):
    """uhubctl/sudo keyword arguments for port_reset() and the power functions."""
    return {"uhubctl": pytestconfig.getoption("uhubctl"),
            "sudo": pytestconfig.getoption("sudo")}


def _find_one(directory, pattern):
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        pytest.fail(f"expected one {pattern} in {directory}, found {len(matches)}",
                    pytrace=False)
    return matches[0]


@pytest.fixture(scope="session")
def firmware(pytestconfig, board):
    directory = pytestconfig.getoption("firmware_dir") or board.entry.get("firmware_dir")
    if not directory:
        pytest.fail("no firmware dir: pass --firmware-dir or set firmware_dir "
                    "in the board entry", pytrace=False)
    directory = Path(directory)
    return Firmware(_find_one(directory, "*.combined.bin"),
                    _find_one(directory, "*.partitions.bin"))


# --- Secrets and credentials ----------------------------------------------


@dataclass(frozen=True)
class BrokerCreds:
    host: str
    port: int
    username: str
    key: str


@dataclass(frozen=True)
class NetworkCreds:
    ssid: str
    password: str


@pytest.fixture(scope="session")
def secrets_dir(pytestconfig):
    directory = (pytestconfig.getoption("secrets_dir")
                 or os.environ.get(SECRETS_DIR_ENV)
                 or DEFAULT_SECRETS_DIR)
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        pytest.fail(f"secrets dir {directory} not found: pass --secrets-dir or set "
                    f"${SECRETS_DIR_ENV}", pytrace=False)
    return directory


@pytest.fixture(scope="session")
def secrets_file(secrets_dir):
    """secrets_file(name) -> the path of <secrets_dir>/<name>.secrets.json."""
    def find(name):
        path = secrets_dir / f"{name}{SECRETS_SUFFIX}"
        if not path.is_file():
            pytest.fail(f"secrets scenario '{name}' not found: {path}", pytrace=False)
        return path
    return find


@pytest.fixture(scope="session")
def good_secrets(secrets_file):
    """The known-good secrets, validated. The only scenario that is."""
    try:
        return hil_board_utils.load_secrets(secrets_file(GOOD_SECRETS))
    except hil_board_utils.SecretsError as e:
        pytest.fail(str(e), pytrace=False)


@pytest.fixture(scope="session")
def default_secrets():
    """The placeholder secrets the firmware rejects, as in
    examples/secrets-examples/secrets-wifi.json."""
    return {
        "io_username": "YOUR_IO_USERNAME_HERE",
        "io_key": "YOUR_IO_KEY_HERE",
        "network_type_wifi": {
            "network_ssid": "YOUR_WIFI_SSID_HERE",
            "network_password": "YOUR_WIFI_PASS_HERE",
        },
    }


def _with_wifi(secrets, **changes):
    """A copy of secrets with network_type_wifi updated with changes."""
    secrets = copy.deepcopy(secrets)
    secrets["network_type_wifi"].update(changes)
    return secrets


@pytest.fixture(scope="session")
def generated_secrets(default_secrets):
    """Scenarios built here instead of read from the secrets dir, by name.
    The structure scenarios keep the placeholder credentials: the firmware
    checks structure first, so it must halt before it gets to them."""
    no_wifi = {k: v for k, v in default_secrets.items() if k != "network_type_wifi"}
    return {
        DEFAULT_SECRETS: default_secrets,
        "no_network_type_wifi": no_wifi,
        "empty_alternative_networks":
            _with_wifi(default_secrets, alternative_networks=[]),
        "string_alternative_networks":
            _with_wifi(default_secrets, alternative_networks="YOUR_WIFI_SSID_HERE"),
        "object_alternative_networks":
            _with_wifi(default_secrets, alternative_networks={
                "network_ssid": "YOUR_WIFI_SSID_HERE",
                "network_password": "YOUR_WIFI_PASS_HERE",
            }),
    }


@pytest.fixture(scope="session")
def broker_creds(good_secrets):
    return BrokerCreds(
        host=good_secrets.get("io_url", "io.adafruit.com"),
        port=good_secrets.get("io_port", 8883),
        username=good_secrets["io_username"],
        key=good_secrets["io_key"],
    )


@pytest.fixture(scope="session")
def network_creds(good_secrets):
    wifi = good_secrets["network_type_wifi"]
    return NetworkCreds(ssid=wifi["network_ssid"], password=wifi["network_password"])


# --- Flashing -------------------------------------------------------------


@pytest.fixture
def secrets_name(request):
    """The secrets scenario the test boots with: the test's indirect
    parameter, else the good secrets."""
    return getattr(request, "param", GOOD_SECRETS)


@pytest.fixture
def secrets_path(request, secrets_name, secrets_file, tmp_path):
    """The file for secrets_name: None for no_secrets_file and no_fs_image,
    written to a temp file if it's one of generated_secrets, else
    <secrets_dir>/<secrets_name>.secrets.json."""
    if secrets_name in (NO_SECRETS_FILE, NO_FS_IMAGE):
        return None
    generated = request.getfixturevalue("generated_secrets")
    if secrets_name not in generated:
        return secrets_file(secrets_name)
    path = tmp_path / f"{secrets_name}{SECRETS_SUFFIX}"
    path.write_text(json.dumps(generated[secrets_name], indent=4))
    return path


@dataclass(frozen=True)
class FlashState:
    """What flashed_board did to the board before the test."""
    secrets_path: Path  # None if no secrets file was packed
    erase: hil_board_utils.EraseResult
    build: hil_board_utils.BuildResult  # None if no fs image was flashed
    flash: hil_board_utils.FlashResult
    erase_s: float  # seconds to erase the chip
    build_s: float  # seconds to build the LittleFS image
    flash_s: float  # seconds to flash the firmware and image


@pytest.fixture
def flashed_board(board, firmware, secrets_name, secrets_path, tmp_path):
    """Erase the whole chip, then flash the firmware and a LittleFS image
    holding secrets_path. The file is packed as-is, valid or not. For
    no_fs_image only the firmware is flashed."""
    image = None if secrets_name == NO_FS_IMAGE else tmp_path / "littlefs.bin"
    build = None
    try:
        start = time.perf_counter()
        erase = hil_board_utils.esp_erase_flash(board.port, board.chip,
                                                baud=board.flash_baud)
        erased = time.perf_counter()
        if image is not None:
            build = hil_board_utils.esp_make_littlefs(firmware.partitions, image,
                                                      secrets_path)
        built = time.perf_counter()
        flash = hil_board_utils.esp_flash(board.port, board.chip, firmware.partitions,
                                          firmware.combined, image,
                                          baud=board.flash_baud)
        flashed = time.perf_counter()
    except hil_board_utils.HilBoardUtilsError as e:
        pytest.fail(str(e), pytrace=False)
    log.info("erased %s, flashed %s with scenario %s: erase %.2fs, build %.2fs, "
             "flash %.2fs", board.port, firmware.combined.name, secrets_name,
             erased - start, built - erased, flashed - built)
    return FlashState(secrets_path, erase, build, flash, erase_s=erased - start,
                      build_s=built - erased, flash_s=flashed - built)


# --- Serial ---------------------------------------------------------------


@pytest.fixture
def serial(board, flashed_board, request, pytestconfig, tmp_path):
    """A SerialMonitor on the board's port. It doesn't reset the board; the
    test does (board_reset() or port_reset()), as its Act step. The capture
    is dumped when the test ends."""
    mon = hil_board_utils.SerialMonitor(board.port, board.read_baud)
    yield mon
    mon.close()
    log_dir = pytestconfig.getoption("serial_log_dir") or tmp_path
    path = mon.dump(Path(log_dir) / f"{request.node.name}.serial.log")
    log.info("serial log: %s", path)


@pytest.fixture
def device_uid(serial):
    """device_uid(timeout) -> the Device UID the firmware prints at the start
    of connect(), the client ID it uses on the broker. Call it after the
    test's Act step, and after expecting any earlier boot lines: it moves the
    serial cursor past the UID line. Scenarios that halt in provision() never
    print it."""
    def read(timeout=30):
        uid = serial.expect(DEVICE_UID_RE, timeout=timeout).group(1)
        log.info("device uid: %s", uid)
        return uid
    return read
