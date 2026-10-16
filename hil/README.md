# HIL tests

## Running the provisioning tests

From the repository root:

```bash
cd hil
source venv/bin/activate

# The "good", "valid_io_only" and "valid_wifi_only" scenarios need these:
export HIL_IO_USERNAME=...
export HIL_IO_KEY=...
export HIL_WIFI_SSID=...
export HIL_WIFI_PASSWORD=...        # can be empty for an open network

pytest firmware/provisioning/ --log-cli-level=INFO
```

If the venv doesn't exist yet, create it first:

```bash
cd hil
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```
