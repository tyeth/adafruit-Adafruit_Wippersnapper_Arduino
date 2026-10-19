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
export HIL_IO_URL=<num>.tcp.ngrok.io
export HIL_IO_PORT=<port>

pytest firmware/provisioning/ --log-cli-level=INFO
```

If the venv doesn't exist yet, create it first:

```bash
cd hil
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```
