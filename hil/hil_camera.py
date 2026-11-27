"""Camera instrumentation for HIL tests: photograph the DUT with the Pi
camera (rpicam-still) and have a vision model report what the NeoPixel
strand shows, as JSON matching PIXELS_SCHEMA.

capture() returns an envelope (and writes it next to the photo as .json):

    {"timestamp": "2026-11-24T15:02:11Z", "camera": 0,
     "image": "captures/2026-11-24T150211Z-cam0.jpg",
     "model": "gpt-5.6-luna",
     "result": {"neopixel": {"pixels_power_status": "on",
                             "pixels_color": "red"}}}

The model only produces "result"; the rest is filled in here.

Needs OPENAI_API_KEY. $HIL_VISION_MODEL overrides the model, and
$HIL_CAMERA_SCENE, if set, is added to the prompt to describe where the
strand sits in the frame. $HIL_CAMERA_ROI ("x,y,w,h", fractions of the
frame) crops the photo to the strand with rpicam-still --roi.

Run by hand: venv/bin/python hil_camera.py [--camera 0] [--image test.jpg]
"""

import argparse
import base64
import datetime
import json
import os
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
CAPTURES_DIR = HERE / "captures"
DEFAULT_MODEL = "gpt-5.6-luna"

PIXELS_SCHEMA = {
    "type": "object",
    "properties": {
        "neopixel": {
            "type": "object",
            "properties": {
                "pixels_power_status": {"type": "string",
                                        "enum": ["on", "off"]},
                "pixels_color": {"type": "string",
                                 "enum": ["red", "green", "blue", "white",
                                          "other", "none"]},
            },
            "required": ["pixels_power_status", "pixels_color"],
            "additionalProperties": False,
        },
    },
    "required": ["neopixel"],
    "additionalProperties": False,
}

PROMPT = """\
This photo is of a hardware-in-the-loop test bench with a NeoPixel strand \
(WS2812-style RGB LEDs). Report:
- pixels_power_status: "on" if its LEDs are visibly emitting light, else "off".
- pixels_color: the color the lit LEDs emit: "red", "green", "blue", \
"white", or "other" for any other color. Use "none" when the strand is off.
Unlit NeoPixels have pale yellowish diffusers; lit ones glow brightly, \
with bloom or a halo around each LED. Pale diffusers without a glow are \
off, not white.
Judge by the light the LEDs emit, not by reflections, wire colors, or \
other indicator LEDs on the boards. If the strand is not visible or you \
cannot tell it is lit, report it as off/none."""


def take_photo(path, camera=0, timeout_ms=2000, roi=None):
    """Take a still with rpicam-still into path, cropped to roi ("x,y,w,h",
    fractions of the frame) if given."""
    cmd = ["rpicam-still", "-n", "-t", str(timeout_ms), "--camera",
           str(camera), "--autofocus-on-capture", "-o", str(path)]
    if roi:
        cmd += ["--roi", roi]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def analyze(image_path, model=None, scene=None, client=None):
    """Ask the vision model what the strand in image_path shows; returns
    the dict matching PIXELS_SCHEMA."""
    from openai import OpenAI  # only needed by camera tests

    client = client or OpenAI()
    model = model or os.environ.get("HIL_VISION_MODEL", DEFAULT_MODEL)
    scene = scene if scene is not None else os.environ.get("HIL_CAMERA_SCENE")
    prompt = PROMPT + (f"\n\nScene layout: {scene}" if scene else "")
    b64 = base64.b64encode(pathlib.Path(image_path).read_bytes()).decode()
    resp = client.responses.create(
        model=model,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "detail": "high",
             "image_url": f"data:image/jpeg;base64,{b64}"},
        ]}],
        text={"format": {"type": "json_schema", "name": "pixels",
                         "schema": PIXELS_SCHEMA, "strict": True}},
    )
    return json.loads(resp.output_text)


def capture(camera=0, image=None, model=None, scene=None, roi=None):
    """Photograph the bench (or use an existing image) and analyze it.
    Returns the envelope, also written beside the image as .json."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    model = model or os.environ.get("HIL_VISION_MODEL", DEFAULT_MODEL)
    if image is None:
        CAPTURES_DIR.mkdir(exist_ok=True)
        image = CAPTURES_DIR / f"{now:%Y-%m-%dT%H%M%SZ}-cam{camera}.jpg"
        take_photo(image, camera,
                   roi=roi or os.environ.get("HIL_CAMERA_ROI"))
    image = pathlib.Path(image).resolve()
    envelope = {
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "camera": camera,
        "image": (str(image.relative_to(HERE))
                  if image.is_relative_to(HERE) else str(image)),
        "model": model,
        "result": analyze(image, model=model, scene=scene),
    }
    image.with_suffix(".json").write_text(json.dumps(envelope, indent=2) + "\n")
    return envelope


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--image", help="analyze this image instead of taking one")
    p.add_argument("--model", help=f"default $HIL_VISION_MODEL or {DEFAULT_MODEL}")
    p.add_argument("--scene", help="where the strand is in the frame")
    p.add_argument("--roi", help='crop, "x,y,w,h" fractions of the frame')
    a = p.parse_args()
    print(json.dumps(capture(a.camera, a.image, a.model, a.scene, a.roi), indent=2))


if __name__ == "__main__":
    main()
