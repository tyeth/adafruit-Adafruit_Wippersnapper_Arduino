"""
Shared helpers for the digitalIO HIL tests (test_digital_out.py,
test_digital_in.py).
"""

from hil_checkin import CHECKIN_TIMEOUT, checkin  # noqa: F401


def remove(serial, protomq, uid, pin):
    """Send a digital.Remove for pin and expect the DUT to remove it."""
    protomq.publish(uid, {"digital": {"remove": {"pin": pin}}})
    serial.expect(rf"^\[dio\] Pin removed: {pin.lstrip('D')}$", timeout=5)
