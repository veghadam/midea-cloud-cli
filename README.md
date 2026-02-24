# midea-cloud-cli

Read temperatures and status from Midea air conditioners over the internet — no local network access needed.

Works with accounts registered in the **Midea Air** or **NetHome Plus** apps.

## Why this exists

Every existing open-source Midea library fails to communicate with devices through the cloud, returning the infamous **error 1000 ("system error")** on the `transparent/send` endpoint. After reverse-engineering the Midea Air APK, I discovered three things that all existing libraries get wrong:

1. **`/new` API endpoints are required** — The old endpoints (`/v1/user/login`, `/v1/appliance/transparent/send`) are broken server-side. The working variants end in `/new` and require `encryptVersion=1`.

2. **m0 packet framing is mandatory** — Device commands must be wrapped in a 56-byte transport frame (0x5A5A magic header) before sending. Every library sends raw command bytes, which the cloud relay silently rejects.

3. **AES-CBC encryption** — The `/new` login returns `randomData` used to derive an AES-CBC IV. The old login only supports ECB, which the `/new` transparent send rejects.

## Install

```bash
pip install -r requirements.txt
```

Requires Python 3.8+.

## Usage

```bash
# Using command-line arguments
python midea_cloud.py --account your@email.com --password yourpassword

# Using environment variables
export MIDEA_ACCOUNT=your@email.com
export MIDEA_PASSWORD=yourpassword
python midea_cloud.py
```

### Options

| Flag | Description |
|------|-------------|
| `--account` | Midea account email (or `MIDEA_ACCOUNT` env var) |
| `--password` | Midea account password (or `MIDEA_PASSWORD` env var) |
| `--app` | App your account is registered with: `midea_air` (default) or `nethome_plus` |
| `--server` | Override API server URL (auto-detected by default) |
| `--list-only` | Only list devices, don't query temperatures |
| `--json` | Output results as JSON |

### Example output

```
Logging in...
Logged in (server: https://mapp-eu.appsmb.com)

Living room:
  Indoor:  22.5 C
  Outdoor: 8.0 C
  Target:  23 C
  Running: True
  Mode:    Heat

Bedroom:
  Indoor:  20.0 C
  Outdoor: 8.0 C
  Target:  21 C
  Running: False
  Mode:    Auto
```

### JSON output

```bash
python midea_cloud.py --json
```

```json
{
  "Living room": {
    "running": true,
    "mode": "Heat",
    "target_temp": 23,
    "indoor_temp": 22.5,
    "outdoor_temp": 8.0
  }
}
```

## Using as a library

```python
from midea_cloud import MideaCloud

client = MideaCloud("your@email.com", "yourpassword")
client.login()

# List all devices
devices = client.list_appliances()

# Get AC status
for dev_id, info in devices.items():
    if info["type"] == 0xAC and info["online"]:
        status = client.get_ac_status(dev_id)
        print(f"{info['name']}: {status['indoor_temp']} C")
```

## Supported devices

- **Air conditioners** (type 0xAC) — full temperature reading support
- Other Midea device types can be added by implementing their command/response protocols

## How it works

```
                    ┌──────────────┐
                    │  Midea Cloud │
                    │  (mapp*.     │
 You ──────────────►│  appsmb.com) │──────────────► AC Unit
   login/new        │              │  transparent/
   CBC encryption   │              │  send/new
   m0 framing       └──────────────┘
```

1. **Login** via `/v1/user/login/new` with `encryptVersion=1` — returns `accessToken` and `randomData`
2. **Derive AES-CBC keys** from `sha256(appKey)` → decrypt `accessToken` (data key) and `randomData` (data IV)
3. **Build command** — 33-byte AC status query with CRC8 checksum
4. **Wrap in m0 frame** — 0x5A5A header, message type, timestamp, device SN, payload
5. **Encrypt** — Convert frame to signed CSV, AES-CBC encrypt with session keys
6. **Send** via `/v1/appliance/transparent/send/new` with `funId=0008`
7. **Decrypt response** — AES-CBC decrypt, parse CSV, unwrap m0 frame, parse AC status bytes

## Regional servers

The API server is auto-detected based on your account. Known servers:

- `https://mapp.appsmb.com` — Global
- `https://mapp-eu.appsmb.com` — Europe
- `https://mapp-us.appsmb.com` — Americas

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `error 3101` | Wrong email/password | Check credentials |
| `error 3102` | Account not found on this cloud | Try `--app nethome_plus` |
| `error 3004` | Invalid appliance ID | Report as a bug |
| `error 1000` | Shouldn't happen with this tool | File an issue with full output |
| `error 1011` | Encryption mismatch | File an issue |

## Credits

Built by reverse-engineering the Midea Air APK (`com.midea.aircondition.obm` v5.13.0801). Inspired by [midea-beautiful-air](https://github.com/nbogojevic/midea-beautiful-air), [midea-local](https://github.com/midea-lan/midea-local), and [midea_auto_cloud](https://github.com/sususweet/midea_auto_cloud) — none of which support working cloud communication.

## License

MIT
