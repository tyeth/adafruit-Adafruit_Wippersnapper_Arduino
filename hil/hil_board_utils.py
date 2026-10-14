#!/usr/bin/env python3
"""HIL board utilities: build a LittleFS image containing secrets.json, sized from
an ESP32 partitions.bin, and flash it alongside the firmware with esptool.

CLI:
    python hil_board_utils.py -partitions partitions.bin -o output.bin
    python hil_board_utils.py -partitions partitions.bin -get-offset
    python hil_board_utils.py -partitions partitions.bin -get-offset -o output.bin

Library:
    import hil_board_utils

    offset = hil_board_utils.esp_get_fs_offset("partitions.bin")
    result = hil_board_utils.esp_make_littlefs("partitions.bin", "output.bin",
                                              secrets_path="secrets.json")
    result.partition.offset, result.partition.size, result.listing

    hil_board_utils.esp_flash(port="/dev/ttyACM0", chip="esp32",
                              partitions_path="partitions.bin",
                              combined_bin="firmware.combined.bin",
                              fs_image="output.bin")

All library functions raise HilBoardUtilsError subclasses instead of exiting.
"""

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# ESP32 flash geometry: 4 KB erase sectors, 256-byte write pages.
# Not stored in any file; these are the standard Arduino/PlatformIO values.
ESP_BLOCK_SIZE = 4096
ESP_PAGE_SIZE = 256

ESP_ENTRY_SIZE = 32
ESP_ENTRY_MAGIC = b"\xaa\x50"
ESP_MD5_MAGIC = b"\xeb\xeb"
ESP_MAX_TABLE_SIZE = 0xC00  # ESP-IDF partition table max length

ESP_TYPE_DATA = 0x01
ESP_SUBTYPE_SPIFFS = 0x82
ESP_SUBTYPE_LITTLEFS = 0x83
ESP_FS_SUBTYPES = (ESP_SUBTYPE_SPIFFS, ESP_SUBTYPE_LITTLEFS)

# Merged firmware images (*.combined.bin) are written at 0x0 and must have the
# 0xE9 image magic where the chip's ROM expects the bootloader.
ESP_COMBINED_OFFSET = 0x0
ESP_IMAGE_MAGIC = 0xE9
ESP_BOOTLOADER_OFFSETS = {
    "esp32": 0x1000,
    "esp32s2": 0x1000,
    "esp32s3": 0x0,
    "esp32c2": 0x0,
    "esp32c3": 0x0,
    "esp32c6": 0x0,
    "esp32h2": 0x0,
    "esp32c5": 0x2000,
    "esp32p4": 0x2000,
}

SECRETS_FILENAME = "secrets.json"


# --- Errors ---------------------------------------------------------------


class HilBoardUtilsError(Exception):
    """Base class for all errors raised by this module."""


class EspPartitionError(HilBoardUtilsError):
    pass


class SecretsError(HilBoardUtilsError):
    pass


class MklittlefsError(HilBoardUtilsError):
    pass


class EspFlashError(HilBoardUtilsError):
    pass


class EsptoolError(HilBoardUtilsError):
    pass


class PortError(HilBoardUtilsError):
    pass


class UhubctlError(HilBoardUtilsError):
    pass


# --- Partition table ------------------------------------------------------


@dataclass(frozen=True)
class EspPartition:
    index: int
    type: int
    subtype: int
    offset: int
    size: int
    label: str
    flags: int


def esp_parse_partition_entry(raw, index):
    """Decode one 32-byte aa50 partition entry."""
    ptype, subtype, offset, size = struct.unpack_from("<BBII", raw, 2)
    label = raw[12:28].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    (flags,) = struct.unpack_from("<I", raw, 28)
    return EspPartition(index, ptype, subtype, offset, size, label, flags)


def esp_check_table_length(data):
    if not data:
        raise EspPartitionError("partition table is empty")
    if len(data) % ESP_ENTRY_SIZE:
        raise EspPartitionError(
            f"partition table length {len(data)} is not a multiple of {ESP_ENTRY_SIZE}"
        )
    if len(data) > ESP_MAX_TABLE_SIZE:
        raise EspPartitionError(
            f"partition table length {len(data)} exceeds max {ESP_MAX_TABLE_SIZE:#x}"
        )


def esp_check_md5_entry(data, pos):
    """MD5 entry: 0xEBEB, 14 bytes 0xFF, 16-byte MD5 of all preceding entries."""
    expected = data[pos + 16 : pos + 32]
    actual = hashlib.md5(data[:pos]).digest()
    if expected != actual:
        raise EspPartitionError(
            f"partition table MD5 mismatch (expected {expected.hex()}, "
            f"got {actual.hex()})"
        )


def esp_check_partitions(partitions):
    """Require at least one entry, no zero sizes, and no overlaps."""
    if not partitions:
        raise EspPartitionError("no valid partition entries found (missing aa50 magic)")
    for p in partitions:
        if p.size == 0:
            raise EspPartitionError(f"partition '{p.label}' has zero size")
    ordered = sorted(partitions, key=lambda p: p.offset)
    for a, b in zip(ordered, ordered[1:]):
        if a.offset + a.size > b.offset:
            raise EspPartitionError(f"partitions '{a.label}' and '{b.label}' overlap")


def esp_parse_partitions(data):
    """Parse and validate an ESP32 binary partition table into Partitions."""
    esp_check_table_length(data)
    partitions = []
    for pos in range(0, len(data), ESP_ENTRY_SIZE):
        raw = data[pos : pos + ESP_ENTRY_SIZE]
        magic = raw[:2]
        if magic == ESP_ENTRY_MAGIC:
            partitions.append(esp_parse_partition_entry(raw, pos // ESP_ENTRY_SIZE))
        elif magic == ESP_MD5_MAGIC:
            esp_check_md5_entry(data, pos)
        elif raw == b"\xff" * ESP_ENTRY_SIZE:
            break  # end of table
        else:
            raise EspPartitionError(
                f"invalid entry at byte {pos:#x}: magic {magic.hex()} "
                f"(expected aa50, ebeb, or ffff padding)"
            )
    esp_check_partitions(partitions)
    return partitions


def esp_read_partitions(path):
    """Read and parse a partitions.bin file."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise EspPartitionError(f"cannot read partitions file: {e}") from e
    try:
        return esp_parse_partitions(data)
    except EspPartitionError as e:
        raise EspPartitionError(f"{path}: {e}") from e


def esp_find_fs_partition(partitions):
    """Return the single SPIFFS/LittleFS data partition, block-aligned."""
    candidates = [
        p for p in partitions if p.type == ESP_TYPE_DATA and p.subtype in ESP_FS_SUBTYPES
    ]
    if not candidates:
        raise EspPartitionError(
            "no SPIFFS/LittleFS data partition (type 0x01, subtype 0x82/0x83)"
        )
    if len(candidates) > 1:
        labels = ", ".join(p.label for p in candidates)
        raise EspPartitionError(
            f"multiple filesystem partitions found ({labels}); expected exactly one"
        )

    part = candidates[0]
    if part.offset % ESP_BLOCK_SIZE:
        raise EspPartitionError(
            f"partition '{part.label}' offset {part.offset:#x} is not "
            f"{ESP_BLOCK_SIZE}-byte aligned"
        )
    if part.size % ESP_BLOCK_SIZE:
        raise EspPartitionError(
            f"partition '{part.label}' size {part.size:#x} is not a multiple "
            f"of the {ESP_BLOCK_SIZE}-byte block size"
        )
    return part


def esp_get_fs_partition(partitions_path):
    """Read partitions.bin and return its filesystem partition."""
    partitions = esp_read_partitions(partitions_path)
    try:
        return esp_find_fs_partition(partitions)
    except EspPartitionError as e:
        raise EspPartitionError(f"{partitions_path}: {e}") from e


def esp_get_fs_offset(partitions_path):
    """Read partitions.bin and return the filesystem partition's flash offset."""
    return esp_get_fs_partition(partitions_path).offset


# --- secrets.json ---------------------------------------------------------


def require_string(obj, key, name=None, allow_empty=False):
    name = name or key
    if key not in obj:
        raise SecretsError(f"missing '{name}'")
    value = obj[key]
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        kind = "a string" if allow_empty else "a non-empty string"
        raise SecretsError(f"'{name}' must be {kind}")


def require_number(obj, key, lo, hi, integer=False):
    if key not in obj:
        raise SecretsError(f"missing '{key}'")
    value = obj[key]
    types = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, types):
        raise SecretsError(f"'{key}' must be {'an integer' if integer else 'a number'}")
    if not lo <= value <= hi:
        raise SecretsError(f"'{key}' must be between {lo} and {hi}, got {value}")


def validate_secrets(secrets):
    """Check secrets has the fields WipperSnapper expects:

    {
      "io_username": "...",
      "io_key": "...",
      "network_type_wifi": {"network_ssid": "...", "network_password": "..."},
      "status_pixel_brightness": 0.2
    }

    Optional: "io_url" (non-empty string), "io_port" (integer 1-65535).
    """
    if not isinstance(secrets, dict):
        raise SecretsError("top level must be a JSON object")

    require_string(secrets, "io_username")
    require_string(secrets, "io_key")

    if "network_type_wifi" not in secrets:
        raise SecretsError("missing 'network_type_wifi'")
    wifi = secrets["network_type_wifi"]
    if not isinstance(wifi, dict):
        raise SecretsError("'network_type_wifi' must be an object")
    require_string(wifi, "network_ssid", "network_type_wifi.network_ssid")
    require_string(wifi, "network_password", "network_type_wifi.network_password",
                   allow_empty=True)

    require_number(secrets, "status_pixel_brightness", 0.0, 1.0)

    if "io_url" in secrets:
        require_string(secrets, "io_url")
    if "io_port" in secrets:
        require_number(secrets, "io_port", 1, 65535, integer=True)


def load_secrets(path):
    """Read, parse, and validate a secrets.json file. Returns the dict."""
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise SecretsError(f"cannot read secrets file: {e}") from e
    try:
        secrets = json.loads(text)
    except json.JSONDecodeError as e:
        raise SecretsError(f"{path} is not valid JSON: {e}") from e
    try:
        validate_secrets(secrets)
    except SecretsError as e:
        raise SecretsError(f"{path}: {e}") from e
    return secrets


# --- mklittlefs -----------------------------------------------------------


def find_mklittlefs(fallback=None):
    """Locate mklittlefs: PATH first, then the fallback path, else fail."""
    on_path = shutil.which("mklittlefs")
    if on_path:
        return on_path
    if fallback:
        found = shutil.which(str(fallback))
        if found:
            return found
        raise MklittlefsError(f"-mklittlefs '{fallback}' is not an executable file")
    raise MklittlefsError("mklittlefs not found on PATH; pass its path with -mklittlefs")


def esp_geometry_args(size):
    return ["-b", str(ESP_BLOCK_SIZE), "-p", str(ESP_PAGE_SIZE), "-s", str(size)]


def run_mklittlefs(tool, args):
    """Run mklittlefs and return its stdout."""
    try:
        result = subprocess.run(
            [tool, *args], check=True, capture_output=True, text=True
        )
    except OSError as e:
        raise MklittlefsError(f"cannot run mklittlefs ('{tool}'): {e}") from e
    except subprocess.CalledProcessError as e:
        raise MklittlefsError(
            f"mklittlefs failed (exit {e.returncode}):\n{e.stdout}{e.stderr}"
        ) from e
    return result.stdout


def esp_create_image(tool, secrets_path, size, output):
    """Pack secrets_path as /secrets.json into a LittleFS image, then verify it
    by listing the image. Returns the listing."""
    geometry = esp_geometry_args(size)
    with tempfile.TemporaryDirectory() as pack_dir:
        shutil.copy(secrets_path, Path(pack_dir) / SECRETS_FILENAME)
        run_mklittlefs(tool, ["-c", pack_dir, *geometry, str(output)])

    listing = run_mklittlefs(tool, ["-l", *geometry, str(output)])
    if f"/{SECRETS_FILENAME}" not in listing:
        raise MklittlefsError(
            f"/{SECRETS_FILENAME} not found in image listing:\n{listing}"
        )
    return listing


# --- esptool --------------------------------------------------------------


@dataclass(frozen=True)
class Esptool:
    path: str
    version: str

    @property
    def write_flash_command(self):
        # esptool v5 renamed write_flash to write-flash
        major = int(self.version.split(".")[0]) if self.version[:1].isdigit() else 0
        return "write-flash" if major >= 5 else "write_flash"


def esp_esptool_version(path):
    """Return esptool's version string, or None if it can't run."""
    try:
        result = subprocess.run([path, "version"], capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    return lines[-1].strip() if lines else None


def esp_find_esptool(fallback=None):
    """Locate a working esptool: PATH first (esptool, esptool.py), then fallback."""
    for name in ("esptool", "esptool.py"):
        path = shutil.which(name)
        if path:
            version = esp_esptool_version(path)
            if version:
                return Esptool(path, version)
    if fallback:
        path = shutil.which(str(fallback))
        if not path:
            raise EsptoolError(f"esptool '{fallback}' is not an executable file")
        version = esp_esptool_version(path)
        if not version:
            raise EsptoolError(f"esptool '{fallback}' does not run ('version' failed)")
        return Esptool(path, version)
    raise EsptoolError("no working esptool found on PATH and no fallback path given")


def esp_bootloader_offset(chip):
    try:
        return ESP_BOOTLOADER_OFFSETS[chip]
    except KeyError:
        known = ", ".join(ESP_BOOTLOADER_OFFSETS)
        raise EspFlashError(f"unknown chip '{chip}' (known: {known})") from None


def esp_check_combined_image(path, chip):
    """Require 0xFF padding up to the bootloader offset, then the 0xE9 magic."""
    offset = esp_bootloader_offset(chip)
    try:
        with open(path, "rb") as f:
            head = f.read(offset + 1)
    except OSError as e:
        raise EspFlashError(f"cannot read combined image: {e}") from e
    if len(head) <= offset:
        raise EspFlashError(f"{path}: too short to contain a bootloader at {offset:#x}")
    if head[:offset] != b"\xff" * offset:
        raise EspFlashError(
            f"{path}: expected 0xFF padding before {offset:#x} for {chip}; "
            f"wrong chip or not a merged image?"
        )
    if head[offset] != ESP_IMAGE_MAGIC:
        raise EspFlashError(
            f"{path}: expected image magic 0xe9 at {offset:#x} for {chip}, "
            f"got {head[offset]:#04x}"
        )


def esp_check_flash_layout(combined_bin, fs_image, partition):
    """The firmware must end before the fs partition, and the fs image must fit it."""
    combined_size = Path(combined_bin).stat().st_size
    fs_size = Path(fs_image).stat().st_size
    if ESP_COMBINED_OFFSET + combined_size > partition.offset:
        raise EspFlashError(
            f"combined image ({combined_size:#x} bytes) overlaps the "
            f"'{partition.label}' partition at {partition.offset:#x}"
        )
    if fs_size > partition.size:
        raise EspFlashError(
            f"filesystem image ({fs_size:#x} bytes) is larger than the "
            f"'{partition.label}' partition ({partition.size:#x} bytes)"
        )


def esp_write_flash_args(esptool, chip, port, images):
    """Build esptool args; images is a list of (offset, path) pairs."""
    args = ["--chip", chip, "--port", str(port), esptool.write_flash_command]
    for offset, path in images:
        args += [f"{offset:#x}", str(path)]
    return args


def esp_run_esptool(esptool, args):
    """Run esptool and return its stdout."""
    try:
        result = subprocess.run(
            [esptool.path, *args], check=True, capture_output=True, text=True
        )
    except OSError as e:
        raise EsptoolError(f"cannot run esptool ('{esptool.path}'): {e}") from e
    except subprocess.CalledProcessError as e:
        raise EsptoolError(
            f"esptool failed (exit {e.returncode}):\n{e.stdout}{e.stderr}"
        ) from e
    return result.stdout


# --- USB serial port (Linux) ----------------------------------------------

# uhubctl hub names: "1" is a root hub (bus 1); "1-1", "1-1.3" are hubs below it.
USB_HUB_RE = re.compile(r"^\d+(-\d+(\.\d+)*)?$")

# Ports resolved this session, keyed by board.
_port_cache = {}


def validate_board_entry(entry):
    """Check a board entry like:

    {"board": "feather_esp32_v2", "location": {"hub": "1-1", "port": 2}}
    """
    if not isinstance(entry, dict):
        raise PortError("board entry must be a JSON object")
    board = entry.get("board")
    if not isinstance(board, str) or not board.strip():
        raise PortError("board entry 'board' must be a non-empty string")
    loc = entry.get("location")
    if not isinstance(loc, dict):
        raise PortError(f"board '{board}': 'location' must be an object")
    hub, port = loc.get("hub"), loc.get("port")
    if not isinstance(hub, str) or not USB_HUB_RE.match(hub):
        raise PortError(
            f"board '{board}': hub must look like '1', '1-1' or '1-1.3', "
            f"got {hub!r}"
        )
    if isinstance(port, bool) or not isinstance(port, int) or port < 1:
        raise PortError(
            f"board '{board}': port must be a positive integer, got {port!r}"
        )


def usb_device_path(hub, port):
    """Device path for whatever is plugged into port of hub.

    Root hubs (a bare bus number) join with '-', all other hubs with '.':
    ("1", 2) -> "1-2", ("1-1", 2) -> "1-1.2", ("1-1.3", 4) -> "1-1.3.4"
    """
    sep = "-" if hub.isdigit() else "."
    return f"{hub}{sep}{port}"


def match_serial_ports(ports, device_path):
    """Return the device names of ports at device_path.

    pyserial's location is the USB interface path, e.g. "1-1.2:1.0", so
    compare the part before the ':'.
    """
    return sorted(
        p.device for p in ports
        if p.location and p.location.split(":", 1)[0] == device_path
    )


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError as e:
        raise PortError("pyserial is required to find ports (pip install pyserial)") from e
    return list_ports.comports()


def find_port(entry, refresh=False):
    """Resolve a board entry to its tty (e.g. /dev/ttyACM0) and cache it for
    the session. Pass refresh=True to look it up again (e.g. after a power cycle).
    """
    validate_board_entry(entry)
    board = entry["board"]
    if not refresh and board in _port_cache:
        return _port_cache[board]

    loc = entry["location"]
    path = usb_device_path(loc["hub"], loc["port"])
    matches = match_serial_ports(list_serial_ports(), path)
    if len(matches) > 1:
        raise PortError(f"board '{board}': multiple ttys at {path}: {', '.join(matches)}")
    if not matches:
        raise PortError(f"board '{board}': no serial port at {path}")
    _port_cache[board] = matches[0]
    return matches[0]


def get_port(board):
    """Return the port find_port already resolved for board."""
    try:
        return _port_cache[board]
    except KeyError:
        raise PortError(f"board '{board}': port not resolved; call find_port first") from None


def clear_port(board=None):
    """Forget one board's cached port, or all of them."""
    if board is None:
        _port_cache.clear()
    else:
        _port_cache.pop(board, None)


# --- Power ----------------------------------------------------------------

# The board entry's location hub/port are the uhubctl -l/-p values. uhubctl
# handles the USB3 companion hub itself (switching 1-1.3 also switches 2-1.3).

# Debian/Raspberry Pi OS installs uhubctl here (apt install uhubctl).
UHUBCTL_FALLBACK = "/usr/sbin/uhubctl"


def find_uhubctl(fallback=UHUBCTL_FALLBACK):
    """Locate uhubctl: PATH first, then the fallback path, else fail."""
    on_path = shutil.which("uhubctl")
    if on_path:
        return on_path
    if fallback:
        found = shutil.which(str(fallback))
        if found:
            return found
        raise UhubctlError(f"uhubctl '{fallback}' is not an executable file")
    raise UhubctlError("uhubctl not found on PATH and no fallback path given")


def run_uhubctl(args, uhubctl=None, sudo=True):
    """Run uhubctl and return its stdout.

    On Linux uhubctl needs root to switch ports (it writes the sysfs port
    disable files), so by default it runs under 'sudo -n', which fails
    instead of prompting if a password would be needed.
    """
    tool = find_uhubctl(uhubctl) if uhubctl else find_uhubctl()
    cmd = ["sudo", "-n", tool, *args] if sudo else [tool, *args]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True,
                                timeout=60)
    except OSError as e:
        raise UhubctlError(f"cannot run uhubctl ('{tool}'): {e}") from e
    except subprocess.TimeoutExpired as e:
        raise UhubctlError(f"uhubctl timed out: {' '.join(cmd)}") from e
    except subprocess.CalledProcessError as e:
        raise UhubctlError(
            f"uhubctl failed (exit {e.returncode}):\n{e.stdout}{e.stderr}"
        ) from e
    return result.stdout


def uhubctl_args(hub, port, action, delay=None):
    args = ["-l", hub, "-p", str(port), "-a", action]
    if delay is not None:
        args += ["-d", str(delay)]
    return args


def _power_on(hub, port, uhubctl=None, sudo=True):
    return run_uhubctl(uhubctl_args(hub, port, "on"), uhubctl, sudo)


def _power_off(hub, port, uhubctl=None, sudo=True):
    return run_uhubctl(uhubctl_args(hub, port, "off"), uhubctl, sudo)


def _power_cycle(hub, port, delay=2, uhubctl=None, sudo=True):
    """Turn the port off, wait delay seconds, and turn it back on."""
    return run_uhubctl(uhubctl_args(hub, port, "cycle", delay), uhubctl, sudo)


def _power_cycle_board(entry, delay=2, uhubctl=None, sudo=True):
    """Power-cycle the board entry's location and forget its cached port."""
    validate_board_entry(entry)
    clear_port(entry["board"])
    loc = entry["location"]
    _power_cycle(loc["hub"], loc["port"], delay, uhubctl, sudo)


def _wait_for_port(entry, timeout=15, interval=0.5):
    """Poll until the board's serial port re-enumerates; return it."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return find_port(entry, refresh=True)
        except PortError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(interval)


# --- High-level API -------------------------------------------------------


@dataclass(frozen=True)
class BuildResult:
    partition: EspPartition
    output: Path
    listing: str


def esp_make_littlefs(partitions_path, output, secrets_path=SECRETS_FILENAME,
                      mklittlefs=None):
    """Validate inputs and build a LittleFS image sized to the fs partition."""
    part = esp_get_fs_partition(partitions_path)
    tool = find_mklittlefs(mklittlefs)
    load_secrets(secrets_path)
    listing = esp_create_image(tool, secrets_path, part.size, output)
    return BuildResult(part, Path(output), listing)


@dataclass(frozen=True)
class FlashResult:
    partition: EspPartition
    command: list
    output: str


def esp_flash(port, chip, partitions_path, combined_bin, fs_image, esptool=None):
    """Flash the merged firmware at 0x0 and the fs image at its partition offset."""
    if not port:
        raise EspFlashError("port is required (e.g. /dev/ttyACM0)")
    part = esp_get_fs_partition(partitions_path)
    esp_check_combined_image(combined_bin, chip)
    try:
        esp_check_flash_layout(combined_bin, fs_image, part)
    except OSError as e:
        raise EspFlashError(f"cannot read image: {e}") from e
    tool = esp_find_esptool(esptool)
    args = esp_write_flash_args(
        tool, chip, port,
        [(ESP_COMBINED_OFFSET, combined_bin), (part.offset, fs_image)],
    )
    output = esp_run_esptool(tool, args)
    return FlashResult(part, [tool.path, *args], output)


# --- CLI ------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a LittleFS image with secrets.json, sized from partitions.bin"
    )
    parser.add_argument("-partitions", required=True, type=Path,
                        help="ESP32 binary partition table (e.g. *.partitions.bin)")
    parser.add_argument("-o", type=Path, help="output LittleFS image file")
    parser.add_argument("-get-offset", action="store_true",
                        help="print the filesystem partition offset")
    parser.add_argument("-secrets", type=Path, default=Path(SECRETS_FILENAME),
                        help="secrets file to pack (default: secrets.json)")
    parser.add_argument("-mklittlefs",
                        help="path to mklittlefs binary, used if not found on PATH")
    args = parser.parse_args(argv)
    if args.o is None and not args.get_offset:
        parser.error("nothing to do: pass -o <output.bin> and/or -get-offset")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.get_offset:
            print(f"{esp_get_fs_offset(args.partitions):#x}", flush=True)
        if args.o is not None:
            result = esp_make_littlefs(args.partitions, args.o, args.secrets,
                                       args.mklittlefs)
            part = result.partition
            print(f"partition '{part.label}': offset {part.offset:#x}, "
                  f"size {part.size:#x} ({part.size} bytes)", file=sys.stderr)
            print(result.listing.rstrip(), file=sys.stderr)
            print(f"wrote {result.output} ({result.output.stat().st_size} bytes)",
                  file=sys.stderr)
    except HilBoardUtilsError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
