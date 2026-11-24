"""Helpers shared by the pixels HIL tests: sending a strand's Add, Write
and Remove, and expecting the DUT to handle each. A strand is the
conftest's PixelStrand (the neopixel_strand and dotstar_strand fixtures).
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
# How long a color sequence holds each color, in seconds
HOLD_SECONDS = 5
# The colors a sequence writes, in order: 0xWWRRGGBB, white off (DotStars
# ignore the most significant byte)
RED, BLUE, GREEN = 0x00FF0000, 0x000000FF, 0x0000FF00
SEQUENCE = (("red", RED), ("blue", BLUE), ("green", GREEN))
# On, then off: RGB white (DotStars ignore the most significant byte, and
# RGBW pixels' white stays off), then black. Every channel 0 is off:
# gamma32() maps 0 to 0, and brightness scales 0 to 0.
WHITE, BLACK = 0x00FFFFFF, 0x00000000
ON_OFF = (("white", WHITE), ("black", BLACK))
# The firmware's MAX_PIXEL_STRANDS (src/components/pixels/controller.h):
# the strand slots PixelsController has
MAX_PIXEL_STRANDS = 10
# Lines the DUT prints when it crashes or reboots
CRASH_RE = re.compile(r"Guru Meditation|Backtrace:|rst:0x|ESP Reset Reason")
# The error.component message Handle_Pixels_Remove() publishes for a pin
# with no strand
NOT_FOUND = "Failed to find strand index!"
# What AddNeoPixel()/AddDotStar() log each Type as
TYPE_NAMES = {"T_NEOPIXEL": "NeoPixel", "T_DOTSTAR": "DotStar"}


def gpio(pin):
    """The GPIO number the firmware takes from pin, e.g. 26 for "D26"."""
    return int(pin[1:])


def add(serial, protomq, uid, strand, brightness=BRIGHTNESS):
    """Send a pixels.Add for strand and expect the DUT to add it."""
    msg = {"type": strand.type, "num": strand.num,
           "ordering": strand.ordering, "brightness": brightness,
           "pinData": strand.pin}
    if strand.pin_clock is not None:
        msg["pinDotstarClock"] = strand.pin_clock
    protomq.publish(uid, {"pixels": {"add": msg}})
    protomq.expect_b2d_message(uid, "pixels.add", match=msg, timeout=5)
    # AddNeoPixel()/AddDotStar() log without a newline, then
    # Handle_Pixels_Add() logs on the same line
    added = (rf"\[pixels\] Added {TYPE_NAMES[strand.type]} strand on pin "
             rf"{gpio(strand.pin)}")
    if strand.pin_clock is not None:
        added += rf" and clock pin {gpio(strand.pin_clock)}"
    serial.expect(added + r"\b", timeout=CHECKIN_TIMEOUT, partial=True)
    serial.expect(rf"\[pixels\]: Added strand on Pin {strand.pin}\b",
                  timeout=5, partial=True)


def write(serial, protomq, uid, strand, color):
    """Send a pixels.Write of color to strand and expect the DUT to fill
    it."""
    msg = {"pinData": strand.pin, "color": color}
    protomq.publish(uid, {"pixels": {"write": msg}})
    protomq.expect_b2d_message(uid, "pixels.write", match=msg, timeout=5)
    serial.expect(r"^\[pixels\]: Filling strand!$", timeout=CHECKIN_TIMEOUT)


def hold(protomq, uid, strand, name, color, seconds=HOLD_SECONDS):
    """Hold strand at color, just written, for seconds, expecting no
    error.component for it meanwhile."""
    log.info("%s: holding %s (0x%08X) for %ss", strand.pin, name, color,
             seconds)
    protomq.assert_no_d2b_message(uid, "error.component",
                                  match={"pin": strand.pin}, within=seconds)


def write_sequence(serial, protomq, uid, strand, sequence=SEQUENCE,
                   seconds=HOLD_SECONDS):
    """Write each (name, color) in sequence to strand, holding each for
    seconds."""
    for name, color in sequence:
        write(serial, protomq, uid, strand, color)
        hold(protomq, uid, strand, name, color, seconds)


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
