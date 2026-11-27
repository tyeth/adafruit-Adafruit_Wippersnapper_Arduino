# HIL host install: agent runbook

Instructions for a coding agent setting up a Raspberry Pi as a HIL host for testing Adafruit IO WipperSnapper.
Background and rationale for each step: [INSTALL.md](INSTALL.md) (same step
numbers). Probe wiring: [README.md](README.md#probes).

Target: Raspberry Pi 5, Raspberry Pi OS 64-bit (Debian 13 "trixie").

## Rules

1. **Check before acting.** Every step has a Check. If it passes, skip the
   step and say so. Re-running this runbook must be safe.
2. **Stop and ask** before anything marked 🧑 (needs a human). Say exactly
   what you need and why, then wait.
3. **Never read, print, write or log credential values.** You may create
   `~/.config/hil/creds.env` from the template and set its mode; the user
   fills in the values. Check for presence only (see step 8).
4. **Ask before**: editing `/boot/firmware/config.txt`, adding udev rules or
   sudoers entries, running `raspi-blinka.py` (it reboots), rebooting,
   power-cycling USB ports, or erasing/flashing a board.
5. **Don't leave ProtoMQ running.** Tests start their own broker on 1884.
6. **Don't commit** anything under `hil/` you created (creds, logs, venv,
   board-entry edits) unless asked.
7. Report at the end: each step as ✅ already done / ✅ done now / ⏸ waiting
   on human / ❌ failed (with the command and output).

## Inputs

Collect these before step 3. Ask the user for any you can't discover.

| Input | How to get it | Used in |
|-------|---------------|---------|
| `REPO` | path of this checkout (default `~/Adafruit_Wippersnapper_Arduino`) | all |
| `BOARD` | board name in `hil-boards.json` (e.g. `feather_esp32_v2`) | 10–12 |
| hub location + port | `sudo uhubctl` with the DUT plugged in; 🧑 confirm which port is the DUT | 3, 10 |
| hub vendor ID | `lsusb` (e.g. `0bda` Realtek) | 3 |
| `firmware_dir` | user, or a `hil-firmware-<board>` artifact from `build-hil.yml` | 9, 10 |
| probes wired? | user; if unsure, start with `"probes": {}` | 10 |
| self-hosted runner? | user (optional step 13) | 13 |

## Steps

### 1. System packages and groups
- Check: `dpkg -s git python3-venv python3-pip python3-lgpio i2c-tools uhubctl nodejs npm build-essential pipx >/dev/null && id -nG | tr ' ' '\n' | grep -cxE 'dialout|i2c|gpio|plugdev'` prints `4`
- Do: `sudo apt update && sudo apt install -y git python3-venv python3-pip python3-lgpio i2c-tools uhubctl nodejs npm build-essential pipx`
- Do: `sudo usermod -aG dialout,i2c,gpio,plugdev "$USER"`
- 🧑 The group change needs a logout/login. Ask the user to do it, then re-run the Check.
- Verify: `node --version` is v18 or newer.

### 2. I2C and PWM overlay
- Check: `grep -qx 'dtparam=i2c_arm=on' /boot/firmware/config.txt && grep -qx 'dtoverlay=pwm,pin=12,func=4' /boot/firmware/config.txt`
- Do (ask first): append whichever line is missing to `/boot/firmware/config.txt`. Skip I2C if you'll run `raspi-blinka.py` in step 7 (it enables I2C).
- 🧑 Reboot needed. Batch it with the step 7 reboot if possible.
- Verify (after reboot): `ls /dev/i2c-1` exists.

### 3. uhubctl without sudo
- Check: `uhubctl -l <hub> -p <port>` (no sudo) prints the port status without a permission error.
- Do (ask first): write `/etc/udev/rules.d/52-uhubctl.rules` with the rule from [INSTALL.md step 3](INSTALL.md#3-usb-port-power-uhubctl), replacing `0bda` with the hub vendor ID. Then `sudo udevadm control --reload-rules && sudo udevadm trigger`.
- 🧑 The hub may need to be replugged for the rule to apply.
- Verify (ask first, it power-cycles the DUT): `uhubctl -l <hub> -p <port> -a cycle`
- Alternative if the user declines the rule: tests run with `--sudo`, which needs a passwordless sudoers entry for uhubctl (🧑).

### 4. esptool
- Check: `esptool version` (on `PATH` outside any venv; v4 or v5)
- Do: `pipx install esptool && pipx ensurepath`
- Verify: open a new shell (or `export PATH="$HOME/.local/bin:$PATH"`), `esptool version`

### 5. mklittlefs
- Check: `command -v mklittlefs && mklittlefs --version`
- Do: `git clone --recursive https://github.com/earlephilhower/mklittlefs.git ~/mklittlefs && make -C ~/mklittlefs && sudo install -m 755 ~/mklittlefs/mklittlefs /usr/local/bin/`
- Verify: `mklittlefs --version`

### 6. Wippersnapper repos and ProtoMQ
- Check each: `git -C ~/<repo> rev-parse --abbrev-ref HEAD`
  - `~/Wippersnapper_Boards` (any branch)
  - `~/Wippersnapper_Components` → `main`
  - `~/Wippersnapper_Protobuf` → `api-v2`
  - `~/ProtoMQ`
- Do: `git clone` any that are missing (use `--branch main` / `--branch api-v2` as above). If a repo exists on the wrong branch, report it; don't switch branches without asking.
- Do: `git -C ~/<repo> pull --ff-only` on the first three (ask first if the tree is dirty).
- Do (ProtoMQ):
  ```bash
  cd ~/ProtoMQ
  npm install
  [ -f .env.json ] || echo '{ "protobufSource": "../Wippersnapper_Protobuf/proto" }' > .env.json
  npm run import-protos
  npm run build-web
  ```
  If `.env.json` exists with a different `protobufSource`, report it; don't overwrite.
- Verify: `ss -ltn | grep -q ':1884 '` must be **empty** first (if not, report `pgrep -af 'node main.js'` and ask before killing). Then:
  ```bash
  timeout 10 node main.js --mqtt-port=1884 --http-port=5173; echo "exit=$?"
  ```
  `exit=124` (killed by timeout, i.e. it stayed up) is a pass. Afterwards confirm nothing listens on 1884.

### 7. Python venv and Blinka
The venv is `hil/venv` (what `run_firmware_suite.sh` uses). Ignore any
other env dirs in `hil/`.
- Check: `$REPO/hil/venv/bin/python -c 'import board, busio, pytest'`
- Do:
  ```bash
  cd "$REPO/hil"
  [ -d venv ] || python3 -m venv --system-site-packages venv   # needs apt python3-lgpio
  venv/bin/pip install --upgrade adafruit-python-shell
  ```
- Do (ask first; it reboots): run the checked-in `raspi-blinka.py` (don't download a new copy):
  ```bash
  cd "$REPO/hil" && source venv/bin/activate && sudo -E env PATH=$PATH python3 raspi-blinka.py
  ```
- 🧑 After the reboot, the session will drop. Resume from here.
- Do: `"$REPO/hil/venv/bin/pip" install -r "$REPO/hil/requirements.txt"`
- Verify: the Check passes.

### 8. Credentials
- Check (presence only, never print values):
  ```bash
  f=~/.config/hil/creds.env
  [ -f "$f" ] && [ "$(stat -c %a "$f")" = 600 ] && \
  for k in HIL_IO_USERNAME HIL_IO_KEY HIL_WIFI_SSID HIL_WIFI_PASSWORD; do
    grep -qE "^$k=." "$f" && ! grep -qE "^$k=DEFAULT_" "$f" && echo "$k ok" || echo "$k MISSING"
  done
  ```
- Do (if the file is missing): `mkdir -p ~/.config/hil && cp "$REPO/hil/creds.env.example" ~/.config/hil/creds.env && chmod 600 ~/.config/hil/creds.env`
- 🧑 Ask the user to fill in the four values (e.g. `nano ~/.config/hil/creds.env`). The WiFi network must let the DUT reach this host. They should set `HIL_PROTOMQ_HOST` if the DUT sees this host at an address other than its LAN IP.
- Do (ask first): add the creds loader to `~/.bashrc` if it isn't already there:
  `if [ -f ~/.config/hil/creds.env ]; then set -a; . ~/.config/hil/creds.env; set +a; fi`
- Verify: the Check prints `ok` for all four.

### 9. Firmware
- Check: `ls <firmware_dir>/*.combined.bin <firmware_dir>/*.partitions.bin`. There must be exactly one of each.
- Do: if it's missing, 🧑 ask the user for a `WS_HIL_BUILD` build (e.g. the `hil-firmware-<board>` artifact from `build-hil.yml`) and put it there. Extract downloads into a fresh empty directory.
- Verify: the Check passes. Report the version from the file name.

### 10. Board entry
- Check: `hil-boards.json` has an entry with `"board": "<BOARD>"` whose `location`, `firmware_dir` and `wippersnapper_boards_id` match the Inputs, and `~/Wippersnapper_Boards/boards/<wippersnapper_boards_id>` exists.
- Do: edit the entry, or write a separate file and pass it with `--boards`. Show the user the diff. Don't add probes the user hasn't confirmed are wired.
- Verify: `python3 -m json.tool hil-boards.json >/dev/null`. If probes are configured, `i2cdetect -y 1` shows their addresses (e.g. MCP23017 `0x20`, MCP4728 `0x60`).

### 11. Smoke test (ask first; it flashes the DUT)
```bash
cd "$REPO/hil" && source venv/bin/activate
set -a; . ~/.config/hil/creds.env; set +a
pytest tests_hil_board_utils/test_hil_probe.py --log-cli-level=INFO
pytest firmware/checkin/ --board "$BOARD" --log-cli-level=INFO --serial-log-dir /tmp/hil-smoke
```
- Pass = checkin passes. That means flashing, port power, serial, the broker and the credentials all work.
- On failure, use the [troubleshooting table](INSTALL.md#troubleshooting) and `/tmp/hil-smoke/*.log`. Don't print lines from the logs that contain credential values.

### 12. Full suites
Only run these if the user asks. They're long, so offer to run them in the background.
```bash
mkdir -p /tmp/hil-logs
cd "$REPO/hil"
BOARD=$BOARD LOG_DIR=/tmp/hil-logs ./run_firmware_suite.sh api
BOARD=$BOARD LOG_DIR=/tmp/hil-logs ./run_firmware_suite.sh system
```

### 13. Self-hosted runner (optional, 🧑)
- Check: `systemctl list-units 'actions.runner.*' --no-legend` shows an active unit.
- 🧑 The user has to get a registration token (repo Settings → Actions → Runners → New self-hosted runner, Linux ARM64). Never ask them to paste the token into chat; have them run `config.sh` themselves:
  ```bash
  cd ~/actions-runner
  ./config.sh --url https://github.com/<owner>/Adafruit_Wippersnapper_Arduino --token <token> --labels wshil
  sudo ./svc.sh install && sudo ./svc.sh start
  ```
- Verify: the Check passes. The runner shows as Idle with the `wshil` label.

## Done check

All of these pass (run them in order and report each one):

```bash
node --version; esptool version; mklittlefs --version
uhubctl -l <hub> -p <port>
"$REPO/hil/venv/bin/python" -c 'import board, busio, pytest'
git -C ~/Wippersnapper_Components rev-parse --abbrev-ref HEAD   # main
git -C ~/Wippersnapper_Protobuf rev-parse --abbrev-ref HEAD     # api-v2
! ss -ltn | grep -q ':1884 '                                     # no stray broker
# step 8 presence check: four "ok"
# step 11 smoke test: checkin passes
```
