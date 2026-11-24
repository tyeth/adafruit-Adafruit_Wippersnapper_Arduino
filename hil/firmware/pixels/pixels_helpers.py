"""Helpers shared by the pixels HIL tests: sending a strand's Add, Write
and Remove, and expecting the DUT to handle each. A strand is the
conftest's PixelStrand (the neopixel_strand fixture).
"""

import logging
import re

log = logging.getLogger("hil.pixels")

# The Add's brightness, 0-255; IO's initial brightness
BRIGHTNESS = 128
# How long after checkin the DUT may take to handle a message, as the
# checkin fixture allows for the checkin itself.
CHECKIN_TIMEOUT = 30
# How long to watch for a component error after the last message
NO_ERROR_WITHIN = 3
# The firmware's MAX_PIXEL_STRANDS (src/components/pixels/controller.h):
# the strand slots PixelsController has
MAX_PIXEL_STRANDS = 10
# Lines the DUT prints when it crashes or reboots
CRASH_RE = re.compile(r"Guru Meditation|Backtrace:|rst:0x|ESP Reset Reason")
# The error.component message Handle_Pixels_Remove() publishes for a pin
# with no strand
NOT_FOUND = "Failed to find strand index!"


def gpio(pin):
    """The GPIO number the firmware takes from pin, e.g. 26 for "D26"."""
    return int(pin[1:])


def add(serial, protomq, uid, strand, brightness=BRIGHTNESS):
    """Send a pixels.Add for strand and expect the DUT to add it."""
    msg = {"type": strand.type, "num": strand.num,
           "ordering": strand.ordering, "brightness": brightness,
           "pinData": strand.pin}
    protomq.publish(uid, {"pixels": {"add": msg}})
    protomq.expect_b2d_message(uid, "pixels.add", match=msg, timeout=5)
    # AddNeoPixel() logs without a newline, then Handle_Pixels_Add() logs
    # on the same line
    serial.expect(rf"\[pixels\] Added NeoPixel strand on pin "
                  rf"{gpio(strand.pin)}\b", timeout=CHECKIN_TIMEOUT,
                  partial=True)
    serial.expect(rf"\[pixels\]: Added strand on Pin {strand.pin}\b",
                  timeout=5, partial=True)


def write(serial, protomq, uid, strand, color):
    """Send a pixels.Write of color to strand and expect the DUT to fill
    it."""
    msg = {"pinData": strand.pin, "color": color}
    protomq.publish(uid, {"pixels": {"write": msg}})
    protomq.expect_b2d_message(uid, "pixels.write", match=msg, timeout=5)
    serial.expect(r"^\[pixels\]: Filling strand!$", timeout=CHECKIN_TIMEOUT)


def remove(serial, protomq, uid, strand):
    """Send a pixels.Remove for strand and expect the DUT to remove it."""
    msg = {"pinData": strand.pin}
    protomq.publish(uid, {"pixels": {"remove": msg}})
    protomq.expect_b2d_message(uid, "pixels.remove", match=msg, timeout=5)
    serial.expect(rf"\[pixels\] Removed pixel strand from pin: "
                  rf"{gpio(strand.pin)}$", timeout=CHECKIN_TIMEOUT)


def assert_no_errors(serial, protomq, uid, strand):
    """Expect the DUT logged no pixels failure since boot, and publishes
    no error.component for strand within NO_ERROR_WITHIN."""
    serial.assert_none(r"\[pixels\].*(Failed|ERROR|Unknown pixel type)")
    protomq.assert_no_d2b_message(uid, "error.component",
                                  match={"pin": strand.pin},
                                  within=NO_ERROR_WITHIN)
