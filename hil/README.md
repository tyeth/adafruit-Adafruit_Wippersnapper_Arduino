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
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```
