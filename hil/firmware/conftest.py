"""Fixtures for the firmware HIL tests, which run against the board in the
board file (hil-boards.json by default).

    pytest firmware/provisioning/
    pytest firmware -k io_user
    pytest firmware --secrets-dir ~/hil-secrets --log-cli-level=INFO

Fixture chain:

    board (session) -> flashed_firmware (session) -> flashed_fs (module) -> serial (function)

Secrets come from a directory of <scenario>.secrets.json files (--secrets-dir,
else $HIL_SECRETS_DIR, else ~/hil-secrets). good.secrets.json is required: it
is flashed with the firmware and is the source of broker_creds and
network_creds. Other scenarios may be invalid on purpose, so they're packed
byte for byte and never validated. The scenarios in generated_secrets (e.g.
"default", the firmware's placeholder secrets) aren't read from the dir:
they're written to a temp file. A module picks a scenario by overriding
secrets_name:

    @pytest.fixture(scope="module")
    def secrets_name():
        return "bad_wifi"

and a single test by parametrizing it indirectly:

    @pytest.mark.parametrize("secrets_name", ["default"], indirect=True)
"""

import copy
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

HIL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HIL_DIR))
import hil_board_utils  # noqa: E402

DEFAULT_BOARDS = HIL_DIR / "hil-boards.json"
DEFAULT_SECRETS_DIR = Path.home() / "hil-secrets"
SECRETS_DIR_ENV = "HIL_SECRETS_DIR"
SECRETS_SUFFIX = ".secrets.json"
GOOD_SECRETS = "good"
DEFAULT_SECRETS = "default"

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


@dataclass
class FlashState:
    """What the session flashed. fs_digest tracks the filesystem's current
    contents (None if unknown, e.g. after a failed flash)."""
    board: Board
    firmware: Firmware
    build: hil_board_utils.BuildResult
    flash: hil_board_utils.FlashResult
    fs_digest: str
    build_s: float  # seconds to build the LittleFS image
    flash_s: float  # seconds to flash the firmware and image


@dataclass(frozen=True)
class FsState:
    secrets_path: Path
    digest: str
    flashed: bool  # False if the filesystem already held these secrets
    build_s: float = 0.0  # seconds to build the image (0 if not flashed)
    flash_s: float = 0.0  # seconds to flash it (0 if not flashed)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="session")
def flashed_firmware(board, firmware, secrets_file, good_secrets, tmp_path_factory):
    """Flash the firmware and a LittleFS image with the good secrets, once."""
    secrets_path = secrets_file(GOOD_SECRETS)
    image = tmp_path_factory.mktemp("fs") / "littlefs.bin"
    try:
        start = time.perf_counter()
        build = hil_board_utils.esp_make_littlefs(firmware.partitions, image,
                                                  secrets_path)
        built = time.perf_counter()
        flash = hil_board_utils.esp_flash(board.port, board.chip, firmware.partitions,
                                          firmware.combined, image,
                                          baud=board.flash_baud)
        flashed = time.perf_counter()
    except hil_board_utils.HilBoardUtilsError as e:
        pytest.fail(str(e), pytrace=False)
    log.info("flashed %s and %s (%s) to %s: build %.2fs, flash %.2fs",
             firmware.combined.name, secrets_path.name, build.partition.label,
             board.port, built - start, flashed - built)
    return FlashState(board, firmware, build, flash, _digest(secrets_path),
                      build_s=built - start, flash_s=flashed - built)


@pytest.fixture(scope="module")
def secrets_name(request):
    """The secrets scenario this module's tests boot with, or a test's
    indirect parameter."""
    return getattr(request, "param", GOOD_SECRETS)


@pytest.fixture(scope="module")
def secrets_path(request, secrets_name, secrets_file, tmp_path_factory):
    """The file for secrets_name: written to a temp file if it's one of
    generated_secrets, else <secrets_dir>/<secrets_name>.secrets.json."""
    generated = request.getfixturevalue("generated_secrets")
    if secrets_name not in generated:
        return secrets_file(secrets_name)
    path = tmp_path_factory.mktemp("secrets") / f"{secrets_name}{SECRETS_SUFFIX}"
    path.write_text(json.dumps(generated[secrets_name], indent=4))
    return path


@pytest.fixture(scope="module")
def flashed_fs(flashed_firmware, secrets_path, tmp_path_factory):
    """Make the board's filesystem hold secrets_path, flashing it only if it
    differs from what's there. The file is packed as-is, valid or not."""
    state = flashed_firmware
    digest = _digest(secrets_path)
    if digest == state.fs_digest:
        log.info("fs unchanged (%s), not flashing", secrets_path.name)
        return FsState(secrets_path, digest, flashed=False)

    image = tmp_path_factory.mktemp("fs") / "littlefs.bin"
    state.fs_digest = None
    try:
        start = time.perf_counter()
        hil_board_utils.esp_make_littlefs(state.firmware.partitions, image, secrets_path)
        built = time.perf_counter()
        hil_board_utils.esp_flash_fs(state.board.port, state.board.chip,
                                     state.firmware.partitions, image,
                                     baud=state.board.flash_baud)
        flashed = time.perf_counter()
    except hil_board_utils.HilBoardUtilsError as e:
        pytest.fail(str(e), pytrace=False)
    state.fs_digest = digest
    log.info("flashed fs with %s: build %.2fs, flash %.2fs", secrets_path.name,
             built - start, flashed - built)
    return FsState(secrets_path, digest, flashed=True,
                   build_s=built - start, flash_s=flashed - built)


# --- Serial ---------------------------------------------------------------


@pytest.fixture
def serial(board, flashed_fs, request, pytestconfig, tmp_path):
    """A SerialMonitor on the board's port. It doesn't reset the board; the
    test does (board_reset() or port_reset()), as its Act step. The capture
    is dumped when the test ends."""
    mon = hil_board_utils.SerialMonitor(board.port, board.read_baud)
    yield mon
    mon.close()
    log_dir = pytestconfig.getoption("serial_log_dir") or tmp_path
    path = mon.dump(Path(log_dir) / f"{request.node.name}.serial.log")
    log.info("serial log: %s", path)
