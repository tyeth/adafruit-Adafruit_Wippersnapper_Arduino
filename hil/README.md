# HIL tests

## Running tests

From the repository root:

```bash
cd hil
source venv/bin/activate

export HIL_IO_USERNAME=...
export HIL_IO_KEY=...
export HIL_WIFI_SSID=...
export HIL_WIFI_PASSWORD=...
# optional: the tests start a ProtoMQ broker on this host for each test
export HIL_PROTOMQ_HOST=<this host's IP, as the DUT sees it>  # default: LAN IP
export HIL_PROTOMQ_PORT=1884                                 # MQTT port
export HIL_PROTOMQ_HTTP_PORT=5173                            # API/web UI port
export HIL_PROTOMQ_DIR=~/ProtoMQ                             # checkout
export HIL_WS_BOARDS_DIR=~/Wippersnapper_Boards               # board definitions

pytest firmware/provisioning/ --log-cli-level=INFO
```

If the venv doesn't exist yet, create it first:

```bash
cd hil
python3 -m venv --system-site-packages venv  # Pi 5 Blinka needs the apt python3-lgpio
source venv/bin/activate
pip3 install --upgrade adafruit-python-shell
wget https://raw.githubusercontent.com/adafruit/Raspberry-Pi-Installer-Scripts/master/raspi-blinka.py
sudo -E env PATH=$PATH python3 raspi-blinka.py  # enables I2C, installs Blinka into the venv; reboots
pip install -r requirements.txt
```

## Probe

Tests that check the DUT's pin levels (e.g. `digital/`'s write tests) read
them through an MCP23017 on this host's I2C bus, described by the board
entry's `probe` in `hil-boards.json`:

```json
"probe": { "type": "mcp23017", "address": "0x20", "pin_map": { "D14": "GPA0" } }
```

`pin_map` maps a DUT pin to the expander pin it's wired to, `GPA0`-`GPA7`
or `GPB0`-`GPB7`. Check the expander shows up with `i2cdetect -y 1`. Tests
that need the probe are skipped on boards without one.

## CI firmware

For now, `build-hil.yml`'s `hil-test` job flashes the prebuilt firmware in
the board entry's `firmware_dir` (`hil-boards.json`, e.g.
`/home/pi/firmware/feather_esp32_v2`) on the HIL host, not the `build` job's
artifact, and doesn't wait for `build`. So CI tests whatever is in that dir,
not the PR's `src/`: copy a new build there (exactly one `*.combined.bin`
and one `*.partitions.bin`) to test a firmware change.
`run_firmware_suite.sh` passes `--firmware-dir` only if `FIRMWARE_DIR` is
set.

To go back to testing the `build` artifact, in `hil-test`: add `build` back
to `needs`, download the `hil-firmware-<board>` artifact into a dir, and
export that dir as `FIRMWARE_DIR`.
