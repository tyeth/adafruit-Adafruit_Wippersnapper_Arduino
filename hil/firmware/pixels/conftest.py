"""Fixtures for the pixels HIL tests."""

import logging
import pathlib

import pytest

import hil_camera as _hil_camera

log = logging.getLogger("hil.pixels")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Stash each phase's report on the item, so fixtures can tell in
    teardown whether the test failed."""
    rep = yield
    setattr(item, f"rep_{rep.when}", rep)
    return rep


@pytest.fixture
def hil_camera(hil_camera, request):
    """The firmware conftest's hil_camera, but deletes the test's captures
    (each photo and its .json) when the test ends. A failed test keeps
    them, for debugging."""
    images = []

    def capture(*args, **kwargs):
        envelope = hil_camera(*args, **kwargs)
        images.append(_hil_camera.HERE / envelope["image"])
        return envelope

    yield capture

    failed = any(getattr(request.node, f"rep_{when}", None) is not None
                 and getattr(request.node, f"rep_{when}").failed
                 for when in ("setup", "call"))
    if failed:
        log.info("test failed, keeping %d captures", len(images))
        return
    for image in images:
        for path in (pathlib.Path(image), pathlib.Path(image).with_suffix(".json")):
            path.unlink(missing_ok=True)
    log.info("deleted %d captures", len(images))
