#!/usr/bin/env bash
# UAT ONLY (arduino-esp32 4.0.0 / ESP-IDF 6.1): stand-in fixes for libraries
# that stop the build before later libraries are compiled, so one run shows
# every breakage. Each patch prints what it did; none of this is for upstream.
set -euo pipefail
source .github/scripts/retry.sh
LIBS=/home/runner/Arduino/libraries

# Arduino_GFX 1.6.9: IDF 6 made esp_lcd_i80_bus_config_t pins gpio_num_t, so
# int8_t initialisers no longer convert (Arduino_ESP32LCD8.cpp:786-800)
if [ ! -d "$LIBS/GFX_Library_for_Arduino" ]; then
  clone_retry --quiet --depth 1 -b v1.6.9 https://github.com/moononournation/Arduino_GFX.git "$LIBS/GFX_Library_for_Arduino"
fi
f="$LIBS/GFX_Library_for_Arduino/src/databus/Arduino_ESP32LCD8.cpp"
sed -i -E 's/(\.(dc|wr|cs)_gpio_num = )(_[a-z0-9]+)/\1(gpio_num_t)\3/; s/\{_d0, _d1, _d2, _d3, _d4, _d5, _d6, _d7\}/{(gpio_num_t)_d0, (gpio_num_t)_d1, (gpio_num_t)_d2, (gpio_num_t)_d3, (gpio_num_t)_d4, (gpio_num_t)_d5, (gpio_num_t)_d6, (gpio_num_t)_d7}/' "$f"
echo "UAT patch: Arduino_GFX ESP32LCD8 gpio_num_t casts: $(grep -c '(gpio_num_t)_' "$f") sites"

# Adafruit TinyUSB: IDF 6 sdkconfig has CONFIG_SOC_USB_OTG_SUPPORTED but no
# CONFIG_USB_OTG_SUPPORTED, so CFG_TUD_ENABLED was 0 (TinyUSBDevice undeclared)
f="$LIBS/Adafruit_TinyUSB_Arduino/src/arduino/ports/esp32/tusb_config_esp32.h"
if [ -f "$f" ]; then
  sed -i 's/^#if defined(CONFIG_USB_OTG_SUPPORTED) \&\& CONFIG_USB_OTG_SUPPORTED$/#if (defined(CONFIG_USB_OTG_SUPPORTED) \&\& CONFIG_USB_OTG_SUPPORTED) || (defined(CONFIG_SOC_USB_OTG_SUPPORTED) \&\& CONFIG_SOC_USB_OTG_SUPPORTED)/' "$f"
  echo "UAT patch: TinyUSB $(git -C "$LIBS/Adafruit_TinyUSB_Arduino" rev-parse --short HEAD) device gate: $(grep -c CONFIG_SOC_USB_OTG_SUPPORTED "$f") line(s)"
fi
echo "UAT libs: TinyUSB $(grep ^version= $LIBS/Adafruit_TinyUSB_Arduino/library.properties 2>/dev/null || echo none), GFX $(grep ^version= $LIBS/GFX_Library_for_Arduino/library.properties)"
