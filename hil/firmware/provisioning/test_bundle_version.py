"""
HIL tests for the Wippersnapper_Protobuf bundle version the firmware was
built with (WS_PB_BUNDLE_VERSION, from src/protos/VERSION), checked against
the VERSION file in the Wippersnapper_Protobuf checkout.

    HIL_WS_PROTOBUF_DIR      optional, the Wippersnapper_Protobuf checkout
                             (api-v2), default ~/Wippersnapper_Protobuf

Exercises:
- `src/wippersnapper.cpp::provision()`
- `src/wippersnapper.cpp::connect()`
- `src/wippersnapper.cpp::PrintDeviceInfo()`
"""
import logging
import os
import re
from pathlib import Path

import pytest

DEFAULT_WS_PROTOBUF_DIR = "~/Wippersnapper_Protobuf"

log = logging.getLogger("hil.provisioning")

# semver 2.0.0, from https://semver.org (named groups dropped), anchored by
# the caller
SEMVER = (
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?:[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
)


def _expected_bundle_version():
    """The version in VERSION at the root of the Wippersnapper_Protobuf
    checkout at $HIL_WS_PROTOBUF_DIR, stripped of surrounding whitespace."""
    value = os.environ.get("HIL_WS_PROTOBUF_DIR")
    protobuf_dir = value if value and value.strip() else DEFAULT_WS_PROTOBUF_DIR
    path = Path(protobuf_dir).expanduser() / "VERSION"
    try:
        version = path.read_text().strip()
    except OSError as e:
        pytest.fail(f"can't read Wippersnapper_Protobuf VERSION: {e}",
                    pytrace=False)
    if not re.fullmatch(SEMVER, version):
        pytest.fail(f"{path} is not a semver version: {version!r}",
                    pytrace=False)
    log.info("expected protobuf bundle %s, from %s", version, path)
    return version


def test_protobuf_bundle_version_reported(board, valid_secrets, flashed_board,
                                          serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The expected version is the Wippersnapper_Protobuf checkout's
    #    VERSION file.
    expected = _expected_bundle_version()

    # 2. The chip was erased and flashed with the firmware and valid
    #    secrets, so it boots through provision() into connect().
    assert flashed_board.secrets_path is not None

    # --- Act ----------------------------------------------------------
    # 3. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 4. The tagged boot line reports a semver bundle version, right after
    #    the build line, matching the Wippersnapper_Protobuf VERSION.
    serial.expect(r"^\[ws:boot\] build=", timeout=30)
    tagged = serial.expect(rf"^\[ws:boot\] protobuf bundle=({SEMVER})$",
                           timeout=5)
    version = tagged.group(1)
    log.info("firmware reports protobuf bundle %s, Wippersnapper_Protobuf "
             "VERSION is %s: %s", version, expected,
             "match" if version == expected else "MISMATCH")
    assert version == expected, (
        f"firmware protobuf bundle {version} != Wippersnapper_Protobuf "
        f"VERSION {expected}")

    # 5. The device info block reports the same version.
    serial.expect(r"^-------Device Information-------$", timeout=30)
    serial.expect(rf"^Protobuf Bundle Version: {re.escape(version)}$",
                  timeout=5)

    # 6. No build-time "VERSION missing" fallback was compiled in.
    serial.assert_none(r"[Pp]rotobuf [Bb]undle [Vv]ersion missing")
