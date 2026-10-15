# secrets.json provisioning edge cases

Edge cases for the secrets.json parsers, with the serial output each one
produces and how the board behaves. Derived from reading the source, not yet
confirmed on hardware; rows marked *(verify)* are the least certain.

## Which parser the HIL board runs

`hil/hil-boards.json` targets `feather_esp32_v2`, which builds with
`USE_LITTLEFS`. `provision()` therefore calls
`WipperSnapper_LittleFS::parseSecrets()` (`src/Wippersnapper.cpp`), not
`Wippersnapper_FS.cpp`. The TinyUSB parser (`Wippersnapper_FS.cpp`) is only
built for ESP32-S2/S3, RP2040 and SAMD boards. The two parsers are nearly the
same, but some error strings differ.

## What a failure does: halted, no WDT reboot

Every error goes through `fsHalt()`:

- Sets the status LED solid to `WS_LED_STATUS_FS_WRITE`, then loops forever,
  printing `Fatal Error: Halted execution!` and the message once a second.
- No WDT is running yet. `WS.enableWDT()` is first called in `connect()`,
  which runs after `provision()`. The loop task isn't subscribed to the task
  watchdog, and `delay()` lets the idle task run, so the board stays halted
  until a reset or power cycle.
- To prove there's no reboot, watch serial for more than 60 s
  (`WS_WDT_TIMEOUT`) and assert no `rst:` / `ets ` boot banner appears.
- On LittleFS the two credential errors end in `\n`, and `PRINTLNVAR` adds
  another newline, so a blank line follows each repeat. `$`-anchored regexes
  still match.
- The LittleFS init failure sets the LED red, but `fsHalt` immediately
  overwrites it with the FS_WRITE color, so an LED check can't tell the two
  cases apart.

The other failure path is `haltError()`, which prints
`ERROR [WDT RESET IN N]` as a countdown and then lets the WDT reboot the
board. Some bad secrets get past parsing and end up there (see below).

## Cases that halt (LittleFS, testable on HIL)

| Fixture | Serial output |
|---|---|
| Chip erased, firmware flashed, no FS image | `ERROR: Failure initializing LittleFS!` (`LittleFS.begin()` doesn't format a failed mount) |
| FS image without secrets.json | `ERROR: No secrets.json found on filesystem - did you upload credentials?` |
| `secrets.json` is a directory *(verify)* | Probably gets past `exists()` and `open()`, then `ERROR: deserializeJson() failed with code EmptyInput` |
| 0-byte file | `ERROR: deserializeJson() failed with code EmptyInput` |
| Truncated JSON (`{"io_username": "x",`) | `... failed with code IncompleteInput` |
| Garbage, trailing comma, `//` comment | `... failed with code InvalidInput` |
| UTF-8 BOM, which Windows Notepad adds *(verify)* | Most likely `InvalidInput` |
| Nesting deeper than 10 levels | `... failed with code TooDeep` |
| Top-level array `[]` | Parses fine, then `ERROR: Could not find network_type_wifi in secrets.json!` |
| `io_username` or `io_key` missing, or not a string | Falls back to the placeholder in `ConfigJson.cpp`, so you get `ERROR: Invalid IO credentials in secrets.json! ...`. The message blames the value when the key is actually missing. |
| Only one of user or key is a placeholder | `ERROR: Invalid IO credentials ...` (the check is an OR) |
| Real IO creds with placeholder SSID or password | `ERROR: Invalid network credentials in secrets.json! ...` |
| Both placeholders | Only the IO error, because it's checked first (covered by `test_default_secrets`) |
| Exactly 256 alt networks | `ERROR: No alternative network entries found under network_type_wifi.alternative_networks in secrets.json!`. The count is cast to `int8_t` and 256 wraps to 0. |

`ERROR: Could not open secrets.json file for reading!` is very hard to reach
on LittleFS, because `exists()` has already passed by then.

## Cases that get through parsing (bugs to pin down)

All of these reach `connect()`. `printDeviceInfo()` prints
`Adafruit.io User:` and `WiFi Network:`, so tests can assert on those lines.

| Fixture | Result |
|---|---|
| `network_ssid` missing or not a string | SSID becomes `unset-ssid` and isn't caught. The WiFi scan fails, then `ERROR: Unable to find WiFi network, rebooting soon...` counts down and the WDT reboots the board. |
| `network_type_wifi` is a string, number or null *(verify)* | Prints `Found single wifi network in secrets.json`, then the same `unset-ssid` path |
| Over-long strings: ssid >31, pass >63, user >30, key >40, url >63 characters | `strlcpy` truncates them without a warning. Assert the truncated user in `Adafruit.io User:`. MQTT auth fails later. |
| More than 3 alt networks | `WARNING: More than 3 networks in secrets.json, only the first 3 will be used. Not using <ssid4>`, then it carries on |
| 128-255 alt networks | `int8_t` goes negative, the loop never runs, and `_isWiFiMulti = true` with no networks filled in. Prints `Network count: -128` (or similar) and no `Added SSID:` lines. |
| Alt entries that aren't objects (`["a","b"]`) | Each one becomes `unset-ssid` with an empty password |
| `status_pixel_brightness` is a string, 5.0, or -1 | A string falls back to 0.2. Out-of-range numbers go to `setStatusLEDBrightness` unchecked. |
| `io_port` is a string | Falls back to 8883 |
| `{...valid...}garbage` | Accepted: ArduinoJson stops after the first complete value |
| `io_url` that isn't io.adafruit.com | Prints extra `Adafruit.io URL:` / `Adafruit.io Port:` lines |

The multi-network path also prints each alt network's password in plain text
(`PASS: ...`).

## TinyUSB-only behavior (`Wippersnapper_FS.cpp`)

Applies if an ESP32-S2/S3 or RP2040 board is added to the HIL.

- The deserialize error is missing a space: it prints
  `ERROR: Unable to parse secrets.json file - deserializeJson() failed with codeInvalidInput`.
- A missing, empty, or `0x00`/`0xFF`-first-byte secrets.json gets
  overwritten with the default template, then it prints
  `ERROR: Please edit the secrets.json file. Then, reset your board.` and
  halts with USB mass storage attached. LittleFS halts instead.
- `New filesystem created! Press the reset button on your board.` can't be
  reached. A freshly formatted FS has no secrets.json, so
  `createSecretsFile()` halts inside `initFilesystem()` before the
  `_freshFS` check.
- If `flash.begin` or `mkfs` fails, it prints
  `ERROR Initializing Filesystem`. As on LittleFS, the red LED is
  immediately overwritten.
- The "Please edit", "Invalid IO" and "Invalid network" errors are also
  appended to `wipper_boot_out.txt`, so a test can mount the drive and check
  that file too.
- There's no `exists()` check before parsing, so the "No secrets.json found"
  case doesn't exist here.

## Suggested next tests

1. No FS image
2. No secrets file
3. EmptyInput / IncompleteInput
4. Missing `io_username` (locks in the misleading message)
5. Missing `network_ssid` (the first case that ends in a WDT reboot instead
   of a halt)
6. More than 3 networks warning
7. The 128 / 256 `int8_t` wrap
