# Guide: Setting up a HIL Host for testing Adafruit WipperSnapper Firmware

Note: This is intended for developers at Adafruit Industries. It is not supported, documented, or guaranteed to work outside of the WipperSnapper project whatsoever!

## What is this?

Adafruit's Adafruit.io WipperSnapper project has a rather complicated set of requirements. Notably, a WipperSnapper device communicates with the Adafruit IO MQTT broker via the "WipperSnapper API" (a protobuf-based API). 

Because testing entire suites of component types and firmware is extremely time consuming, we've decided to automate it using a hardware-in-the-loop (HIL) system. This system is isolated from Adafruit IO.

## System Design
A Raspberry Pi (the HIL host) is wired to a board known as the Device Under Test (the DUT).

The HIL host is entirely run via GitHub and GitHub Actions since it acts as a "self-hosted runner". Once the host is set up, users do not interact with it via ssh or physically. It is a fixture that is triggered thru GitHub.

**When does the HIL host run?**
* A new release of Adafruit IO WipperSnapper is created via GitHub
* It may also be manually triggered by Adafruit Staff from the Adafruit WipperSnapper GitHub repo.

The HIL Host includes its own MQTT broker (ProtoMQ). It is designed to act exactly like Adafruit IO's MQTT broker. During test runs, the HIL host is able to "act like the Adafruit IO MQTT broker" and validates what the DUT does both on the broker-side and electrically on its pins.

**Design**

```
                 GitHub Actions (build-hil.yml)
                          │ self-hosted runner, label "wshil"
                          ▼
┌──────────────────────── HIL host (Raspberry Pi 5) ────────────────────────┐
│  pytest (hil/firmware)                                                    │
│    ├─ hil_board_utils.py ── esptool, mklittlefs ──── USB serial ──┐       │
│    ├─ uhubctl ───────────── USB hub port power ───────────────────┤       │
│    ├─ hil_protomq.py ────── ProtoMQ broker (:1884) ◄── WiFi ──┐   │       │
│    └─ hil_probe.py ──────── I2C / GPIO probes ────── wires ─┐ │   │       │
└─────────────────────────────────────────────────────────────┼─┼───┼───────┘
                                                              ▼ │   ▼
                                                      ┌─────────┴───────┐
                                                      │   DUT (ESP32…)  │
                                                      └─────────────────┘
```

**Parts:**

| Part | Role |
|------|------|
| pytest + fixtures (`hil/firmware/conftest.py`) | This handles the test runner. All tests are developed for Pytest. The pytest fixture chain is `board → flashed_board → serial / device_uid / protomq → checkin` |
| `hil_board_utils.py` | This "multitool" utility can: Create a LittleFS image holding the test's `secrets.json` (sized from the firmware's `partitions.bin`), Erase and flashes a DUT with esptool, find a DUT's serial port across a hub, and watch the DUT's serial output |
| `uhubctl` | Required to power-cycle the DUT's USB hub port, for firmware uploads, clean boots and to recover a hung board |
| ProtoMQ (`hil_protomq.py`) |  MQTT broker that speaks the WipperSnapper protobuf API. Each test starts its own broker instance, auto-responds to or asserts on device→broker messages, and publishes broker→device commands through ProtoMQ's HTTP API |
| Probes (`hil_probe.py`, the board entry's `probes`) | This is the host-side hardware wired to the DUT: MCP23017 (reads digital levels), MCP4728 DAC (drives analog inputs), Pi GPIO PulseIn (PWM and Servo), DS18B20 for OneWire/DS OneWire, PMS5003 (UART), NeoPixel/DotStar strands, and a strand of I2C sensors |
| `hil-boards.json` | Each DUT has one entry in this file: chip, hub port, `firmware_dir`, the tests in each suite, and probe wiring |
| Wippersnapper_Boards / _Components / _Protobuf | board definitions (used by ProtoMQ to simulate the Adafruit IO checkin process), component definitions (for simulating Adafruit IO sending sensor-specific configuration and values for I2c and UART), and the api-v2 protos for use with ProtoMQ |

**What one test does:**

1. Builds the secrets for its scenario (`good`, `invalid_password`, …) from
   `creds.env`. The IO credentials and broker host point the DUT at
   ProtoMQ on this host, not at io.adafruit.com.
2. Erases and flashes the DUT: the firmware `*.combined.bin` plus the
   secrets LittleFS image. API tests share one flash while their scenario
   matches; system tests always start from an erased chip.
3. Starts ProtoMQ and power-cycles the DUT.
4. The DUT joins WiFi, connects to ProtoMQ, and checks in.
5. The test publishes commands (e.g. add a digital output, write a pixel)
   and asserts on the DUT's replies on the broker, its serial log, and the
   probes' readings of its pins.
6. Stops the broker and saves `<test>.serial.log` and `<test>.protomq.log`.

**Suites:** each board entry lists `api_tests` (component behaviour over
the API) and `system_tests` (provisioning, network and MQTT failure
handling). The api tests run first.

**CI:** `build-hil.yml` builds the firmware with `WS_HIL_BUILD` defined
(this keeps nearby SSIDs out of the public logs). It then runs on the
self-hosted `wshil` runner: `hil-prep` re-clones the three Wippersnapper
repos, and `hil-test` runs `run_firmware_suite.sh api` (plus `system` on
releases or `suite: full`) once per board in `hil-boards.json`, one board
at a time. Logs and JUnit XML are uploaded as `hil-logs-<board>`. For now,
`hil-test` flashes the prebuilt firmware in the board entry's
`firmware_dir`, not the build job's artifact. CI only runs on the
`brentru` fork, and never for PRs from other forks.


How to set up a Raspberry Pi as a hardware-in-the-loop (HIL) host and run
the tests in `hil/` against a board under test (DUT). Written against a
Raspberry Pi 5 on Raspberry Pi OS (Debian 13 "trixie"), 64-bit. For what the
tests do and how probes are wired, see [README.md](README.md). Coding
agents: follow [INSTALL.agent.md](INSTALL.agent.md) instead.

The finished host has:

| What | Where (default) | Used for |
|------|-----------------|----------|
| This repo | anywhere, e.g. `~/Adafruit_Wippersnapper_Arduino` | the tests, run from `hil/` |
| Python venv | `hil/venv` | pytest, Blinka, sensor drivers |
| `esptool` | on `PATH` | erasing and flashing the DUT |
| `mklittlefs` | on `PATH` | building the secrets filesystem image |
| `uhubctl` | `/usr/sbin/uhubctl` | power-cycling the DUT's USB port |
| Node.js 18+ and ProtoMQ | `~/ProtoMQ` | the MQTT broker each test starts |
| Wippersnapper_Boards | `~/Wippersnapper_Boards` | board definitions |
| Wippersnapper_Components | `~/Wippersnapper_Components` (`main`) | component definitions (i2c, uart) |
| Wippersnapper_Protobuf | `~/Wippersnapper_Protobuf` (`api-v2`) | protos for ProtoMQ and the i2c tests |
| Credentials | `~/.config/hil/creds.env` | WiFi and IO credentials |
| Firmware | the board entry's `firmware_dir` | the build that gets flashed |

## 1. System packages and permissions

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip python3-lgpio \
  i2c-tools uhubctl nodejs npm build-essential pipx
```

Add your user to the groups the tests need (serial ports, I2C, GPIO, USB),
then log out and back in:

```bash
sudo usermod -aG dialout,i2c,gpio,plugdev "$USER"
```

## 2. Interfaces and overlays

Enable I2C (`sudo raspi-config` -> Interface Options -> I2C, or let
`raspi-blinka.py` do it in step 7). For the AO-00 PulseIn self-check, add
the hardware PWM overlay on GPIO12. `/boot/firmware/config.txt` should end
up containing:

```ini
dtparam=i2c_arm=on
dtoverlay=pwm,pin=12,func=4
```

Reboot after editing it.

## 3. USB port power (uhubctl)

The tests power-cycle the DUT through a USB hub with per-port power
switching. Find the hub and port the DUT is on:

```bash
sudo uhubctl
```

Note the hub location (e.g. `3-1.3`) and port number for the board entry
in step 10.

To run uhubctl without sudo, give `dialout` write access to the hub's port
`disable` files. For a Realtek (`0bda`) hub, create
`/etc/udev/rules.d/52-uhubctl.rules`:

```
# uhubctl without sudo: let dialout control port power on Realtek (0bda) hubs
SUBSYSTEM=="usb", DRIVER=="hub", ATTRS{idVendor}=="0bda", \
  RUN+="/bin/sh -c \"chown -f root:dialout $sys$devpath/*-port*/disable || true\"", \
  RUN+="/bin/sh -c \"chmod -f 660 $sys$devpath/*-port*/disable || true\""
SUBSYSTEM=="usb", DRIVER=="usb", MODE="0664", GROUP="plugdev"
```

Change `0bda` to your hub's vendor ID (`lsusb`), then reload and replug
the hub:

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
uhubctl -l 3-1.3 -p 1 -a cycle   # should work without sudo
```

Alternatively, skip the rule and pass `--sudo` to pytest (runs `sudo -n
uhubctl`, which needs a passwordless sudoers entry for uhubctl).

## 4. esptool

Install it user-wide so it's on `PATH` outside the venv (v4 and v5 both
work):

```bash
pipx install esptool
esptool version
```

(`pip install --user --break-system-packages esptool` also works; it
lands in `~/.local/bin`.)

## 5. mklittlefs

Build it from source and install it on `PATH`:

```bash
git clone --recursive https://github.com/earlephilhower/mklittlefs.git ~/mklittlefs
cd ~/mklittlefs
make
sudo install -m 755 mklittlefs /usr/local/bin/
mklittlefs --version
```

If it's not on `PATH`, `hil_board_utils.py` can't find it and the flash
fixtures fail.

## 6. Wippersnapper repos and ProtoMQ

```bash
cd ~
git clone https://github.com/adafruit/Wippersnapper_Boards.git
git clone --branch main https://github.com/adafruit/Wippersnapper_Components.git
git clone --branch api-v2 https://github.com/adafruit/Wippersnapper_Protobuf.git
git clone https://github.com/adafruit/ProtoMQ.git
```

CI (`build-hil.yml`'s `hil-prep` job) re-clones the first three before
every run. On a manual setup, `git pull` them now and then so the tests
don't run against stale definitions.

Set up ProtoMQ against the api-v2 protos:

```bash
cd ~/ProtoMQ
npm install
echo '{ "protobufSource": "../Wippersnapper_Protobuf/proto" }' > .env.json
npm run import-protos
npm run build-web
```

Check it starts, then stop it (Ctrl-C). The tests start and stop their own
broker, so don't leave one running on port 1884:

```bash
node main.js --mqtt-port=1884 --http-port=5173
```

Rerun `npm run import-protos` whenever Wippersnapper_Protobuf changes.

## 7. This repo and the Python venv

```bash
git clone https://github.com/adafruit/Adafruit_Wippersnapper_Arduino.git
cd Adafruit_Wippersnapper_Arduino/hil
python3 -m venv --system-site-packages venv   # Pi 5 Blinka needs the apt python3-lgpio
source venv/bin/activate
pip install --upgrade adafruit-python-shell
wget https://raw.githubusercontent.com/adafruit/Raspberry-Pi-Installer-Scripts/master/raspi-blinka.py
sudo -E env PATH=$PATH python3 raspi-blinka.py   # enables I2C, installs Blinka into the venv; reboots
```

After the reboot:

```bash
cd Adafruit_Wippersnapper_Arduino/hil
source venv/bin/activate
pip install -r requirements.txt
```

## 8. Credentials

```bash
mkdir -p ~/.config/hil
cp creds.env.example ~/.config/hil/creds.env
chmod 600 ~/.config/hil/creds.env
nano ~/.config/hil/creds.env
```

Fill in `HIL_IO_USERNAME`, `HIL_IO_KEY`, `HIL_WIFI_SSID` and
`HIL_WIFI_PASSWORD`. The WiFi network must be one the DUT can join and
from which it can reach this host (the DUT connects to the ProtoMQ broker
here, not to Adafruit IO). Set `HIL_PROTOMQ_HOST` if the DUT reaches this
host at an address other than its LAN IP. Never commit this file.

`run_firmware_suite.sh` loads it itself. For manual pytest runs, add this to
`~/.bashrc`:

```bash
if [ -f ~/.config/hil/creds.env ]; then set -a; . ~/.config/hil/creds.env; set +a; fi
```

## 9. Firmware

The tests flash the build in the board entry's `firmware_dir`, which must
hold exactly one `*.combined.bin` and one `*.partitions.bin`, e.g.:

```
/home/pi/firmware/feather_esp32_v2/
  wippersnapper.feather_esp32_v2.littlefs.<version>.combined.bin
  wippersnapper.feather_esp32_v2.littlefs.<version>.partitions.bin
  ... (bootloader, boot_app0, elf, map: ignored)
```

Use a build with `WS_HIL_BUILD` defined (`build-hil.yml` writes
`src/ws_hil_config.h` for this), e.g. the `hil-firmware-<board>` artifact
from a `build-hil.yml` run. To test a different dir without editing the
board entry, pass `--firmware-dir` (or set `FIRMWARE_DIR` for
`run_firmware_suite.sh`).

## 10. Board entry

Edit `hil-boards.json` (or pass your own file with `--boards`):

- `board`: the name passed to `--board`
- `wippersnapper_boards_id`: the board's folder in Wippersnapper_Boards
- `chip`, `flash_baud`, `read_baud`, `auto_reset`: esptool and serial settings
- `location`: the uhubctl `hub` and `port` from step 3
- `firmware_dir`: from step 9
- `api_tests`, `system_tests`, `skip_tests`: which tests run in each suite
- `probes`: how the host is wired to the DUT; see [README.md](README.md#probes).
  Tests whose probe is missing are skipped, so start with none if the
  wiring isn't done yet.

Check the host-side I2C probes show up (e.g. MCP23017 at `0x20`, MCP4728
at `0x60`):

```bash
i2cdetect -y 1
```

## 11. Smoke test

With the DUT plugged into the hub port, from `hil/`:

```bash
source venv/bin/activate
pytest tests_hil_board_utils/test_hil_probe.py --log-cli-level=INFO   # host probes
pytest firmware/checkin/ --board feather_esp32_v2 --log-cli-level=INFO  # flash + ProtoMQ + checkin
```

If checkin passes, flashing, port power, serial, the broker and the
credentials all work.

## 12. Running the tests

Manually, from `hil/` with the venv active:

```bash
pytest firmware --board feather_esp32_v2 --suite api --log-cli-level=INFO
pytest firmware --board feather_esp32_v2 --suite system --log-cli-level=INFO
pytest firmware/provisioning/ --log-cli-level=INFO   # one folder
pytest firmware -k io_user                           # by name
pytest firmware --suite api --run-slow               # include slow (sleep soak) tests
```

Useful options: `--serial-log-dir DIR` (serial and `<test>.protomq.log`
logs per test), `--firmware-dir DIR`, `--boards FILE`, `--uhubctl PATH`,
`--sudo`, `--timeout=600`.

The way CI runs them:

```bash
cd hil
BOARD=feather_esp32_v2 LOG_DIR=/tmp/hil-logs ./run_firmware_suite.sh api
BOARD=feather_esp32_v2 LOG_DIR=/tmp/hil-logs ./run_firmware_suite.sh system
```

This writes `junit-<suite>.xml` and the serial logs to `LOG_DIR`. Create
`LOG_DIR` first.

## 13. Self-hosted CI runner (optional)

To have `build-hil.yml` run on this host, install a GitHub Actions
self-hosted runner (repo Settings -> Actions -> Runners -> New self-hosted
runner, Linux ARM64) under `~/actions-runner`, give it the `wshil` label,
and install it as a service so it survives reboots:

```bash
cd ~/actions-runner
./config.sh --url https://github.com/<owner>/Adafruit_Wippersnapper_Arduino \
  --token <token> --labels wshil
sudo ./svc.sh install && sudo ./svc.sh start
```

The runner uses the same `~/.config/hil/creds.env`, `~/ProtoMQ`,
`firmware_dir` and tools set up above. The job creates its own `hil/venv`
in the checkout.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `mklittlefs not found on PATH` | step 5; `which mklittlefs` |
| `no working esptool found on PATH` | step 4; `esptool version` |
| uhubctl permission denied | step 3 udev rule, or `--sudo`; you're in `dialout` (`id`) |
| DUT serial port not found | `location` hub/port match `uhubctl` output; the port is powered |
| `node not found on PATH` | step 1; `node --version` (18+) |
| ProtoMQ port in use | a broker left running: `pgrep -af 'node main.js'`, then kill it |
| DUT never checks in | `HIL_PROTOMQ_HOST` is reachable from the DUT's WiFi; check `<test>.protomq.log` and the serial log |
| Probe tests skipped | the board entry has no such probe, or it's not on `i2cdetect -y 1` |
| `$HIL_... is not set` | `creds.env` not sourced (step 8) |
