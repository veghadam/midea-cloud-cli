#!/usr/bin/env python3
"""
Midea Air Conditioner cloud monitor.

Reads temperatures and status from Midea AC units over the internet using
the Midea Air cloud API. No local network access to the devices is needed.

Reverse-engineered from the Midea Air APK — uses the /new API endpoints
with AES-CBC encryption and m0 packet framing, which is the only combination
that actually works with the current Midea cloud backend.

Usage:
    python midea_cloud.py --account EMAIL --password PASSWORD

Environment variables MIDEA_ACCOUNT and MIDEA_PASSWORD can be used instead.
"""

import argparse
import datetime
import hashlib
import os
import struct
import sys
from urllib.parse import urlencode, unquote_plus, urlparse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ---------------------------------------------------------------------------
# Midea Air app constants (public, extracted from the published APK)
#
# These are NOT secrets. They are the same for every installation of the
# Midea Air app and are embedded in the publicly downloadable APK. They
# function as a public client identifier, similar to an OAuth client_id.
# The actual user credentials are never stored here — they are provided
# at runtime via CLI arguments or environment variables.
# ---------------------------------------------------------------------------
APP_ID = "1117"
APP_KEY = "ff0cf6f5f0c3471de36341cab3f7a9af"  # noqa: S105
SIGN_KEY = "xhdiwjnchekd4d512chdjx5d8e4c394D2D7S"  # noqa: S105

# Regional API servers — the correct one is auto-detected at login
API_SERVERS = {
    "default": "https://mapp.appsmb.com",
    "eu": "https://mapp-eu.appsmb.com",
    "us": "https://mapp-us.appsmb.com",
}

# Supported Midea apps that share the same cloud backend.
# Users can try a different app if their account was registered with it.
SUPPORTED_APPS = {
    "midea_air": {"app_id": "1117", "app_key": "ff0cf6f5f0c3471de36341cab3f7a9af"},
    "nethome_plus": {"app_id": "1017", "app_key": "3742e9e5842d4ad59c2db887e12449f9"},
}

# Device types
DEVICE_TYPE_AC = 0xAC
DEVICE_TYPE_DEHUMIDIFIER = 0xA1

# AC operating modes
AC_MODE_AUTO = 0
AC_MODE_COOL = 1
AC_MODE_DRY = 2
AC_MODE_HEAT = 3
AC_MODE_FAN = 4
AC_MODES = {0: "Auto", 1: "Cool", 2: "Dry", 3: "Heat", 4: "Fan"}
AC_MODES_BY_NAME = {v.lower(): k for k, v in AC_MODES.items()}

# AC fan speeds
AC_FAN_AUTO = 102
AC_FAN_LOW = 40
AC_FAN_MEDIUM = 60
AC_FAN_HIGH = 80

# ---------------------------------------------------------------------------
# CRC8 lookup table
# ---------------------------------------------------------------------------
CRC8_TABLE = [
    0x00,0x5E,0xBC,0xE2,0x61,0x3F,0xDD,0x83,0xC2,0x9C,0x7E,0x20,0xA3,0xFD,0x1F,0x41,
    0x9D,0xC3,0x21,0x7F,0xFC,0xA2,0x40,0x1E,0x5F,0x01,0xE3,0xBD,0x3E,0x60,0x82,0xDC,
    0x23,0x7D,0x9F,0xC1,0x42,0x1C,0xFE,0xA0,0xE1,0xBF,0x5D,0x03,0x80,0xDE,0x3C,0x62,
    0xBE,0xE0,0x02,0x5C,0xDF,0x81,0x63,0x3D,0x7C,0x22,0xC0,0x9E,0x1D,0x43,0xA1,0xFF,
    0x46,0x18,0xFA,0xA4,0x27,0x79,0x9B,0xC5,0x84,0xDA,0x38,0x66,0xE5,0xBB,0x59,0x07,
    0xDB,0x85,0x67,0x39,0xBA,0xE4,0x06,0x58,0x19,0x47,0xA5,0xFB,0x78,0x26,0xC4,0x9A,
    0x65,0x3B,0xD9,0x87,0x04,0x5A,0xB8,0xE6,0xA7,0xF9,0x1B,0x45,0xC6,0x98,0x7A,0x24,
    0xF8,0xA6,0x44,0x1A,0x99,0xC7,0x25,0x7B,0x3A,0x64,0x86,0xD8,0x5B,0x05,0xE7,0xB9,
    0x8C,0xD2,0x30,0x6E,0xED,0xB3,0x51,0x0F,0x4E,0x10,0xF2,0xAC,0x2F,0x71,0x93,0xCD,
    0x11,0x4F,0xAD,0xF3,0x70,0x2E,0xCC,0x92,0xD3,0x8D,0x6F,0x31,0xB2,0xEC,0x0E,0x50,
    0xAF,0xF1,0x13,0x4D,0xCE,0x90,0x72,0x2C,0x6D,0x33,0xD1,0x8F,0x0C,0x52,0xB0,0xEE,
    0x32,0x6C,0x8E,0xD0,0x53,0x0D,0xEF,0xB1,0xF0,0xAE,0x4C,0x12,0x91,0xCF,0x2D,0x73,
    0xCA,0x94,0x76,0x28,0xAB,0xF5,0x17,0x49,0x08,0x56,0xB4,0xEA,0x69,0x37,0xD5,0x8B,
    0x57,0x09,0xEB,0xB5,0x36,0x68,0x8A,0xD4,0x95,0xCB,0x29,0x77,0xF4,0xAA,0x48,0x16,
    0xE9,0xB7,0x55,0x0B,0x88,0xD6,0x34,0x6A,0x2B,0x75,0x97,0xC9,0x4A,0x14,0xF6,0xA8,
    0x74,0x2A,0xC8,0x96,0x15,0x4B,0xA9,0xF7,0xB6,0xE8,0x0A,0x54,0xD7,0x89,0x6B,0x35,
]


def crc8(data):
    crc = 0
    for b in data:
        crc = CRC8_TABLE[(crc ^ b) & 0xFF]
    return crc


# ---------------------------------------------------------------------------
# Byte / string helpers (matching APK's y6 utility class)
# ---------------------------------------------------------------------------
def bytes_to_signed_csv(data):
    """Convert bytes to comma-separated signed decimal string (Java byte format)."""
    return ",".join(str(b if b < 128 else b - 256) for b in data)


def signed_csv_to_bytes(csv_str):
    """Parse signed CSV string back to bytes."""
    return bytearray(int(v.strip()) & 0xFF for v in csv_str.split(","))


def int_to_le_bytes(val, size):
    """Encode integer as little-endian bytes."""
    return val.to_bytes(size, byteorder="little", signed=False)


def appliance_id_to_sn_bytes(appliance_id_str):
    """Convert numeric appliance ID to 6-byte device serial number."""
    val = int(appliance_id_str)
    le_bytes = val.to_bytes(8, byteorder="little", signed=False)
    return bytearray(le_bytes[:6])


# ---------------------------------------------------------------------------
# m0 packet frame (matching APK's m0 class)
#
# The Midea cloud relay requires commands to be wrapped in this transport
# frame.  Without it, the cloud returns error 1000 ("system error").
#
# Frame layout (total = 56 + payload_len):
#   [0-1]   0x5A 0x5A  magic
#   [2]     0x01       version
#   [3]     flags      (encrypt | auth<<4)
#   [4-5]   length     (LE uint16)
#   [6-7]   msg type   (LE uint16, 0x0020 for device command)
#   [8-11]  msg id     (LE uint32, incrementing counter)
#   [12-19] timestamp  (8 bytes)
#   [20-25] device SN  (6 bytes from appliance ID)
#   [26-39] reserved   (zeros)
#   [40..]  payload    (the actual device command bytes)
#   [last 16] checksum (MD5 when auth enabled, zeros otherwise)
# ---------------------------------------------------------------------------
_msg_counter = 1


def build_m0_frame(payload, msg_type=0x0020, appliance_id_str="0"):
    """Build an m0 transport frame around the given payload bytes."""
    global _msg_counter
    _msg_counter += 1

    total_len = 56 + (len(payload) if payload else 0)
    now = datetime.datetime.now()
    year_str = str(now.year)
    timestamp = bytearray([
        now.microsecond // 1000 & 0xFF,
        now.second, now.minute, now.hour,
        now.day, now.month,
        int(year_str[2:4]), int(year_str[0:2]),
    ])

    sn_bytes = appliance_id_to_sn_bytes(appliance_id_str)

    frame = bytearray(total_len)
    frame[0] = 0x5A
    frame[1] = 0x5A
    frame[2] = 0x01
    frame[3] = 0x00
    frame[4:6] = int_to_le_bytes(total_len, 2)
    frame[6:8] = int_to_le_bytes(msg_type, 2)
    frame[8:12] = int_to_le_bytes(_msg_counter, 4)
    frame[12:20] = timestamp
    frame[20:26] = sn_bytes
    if payload:
        frame[40:40 + len(payload)] = payload
    return frame


def parse_m0_frame(data):
    """Extract the inner payload from an m0 transport frame."""
    if len(data) < 46 or data[0] != 0x5A or data[1] != 0x5A:
        return data
    total_len = struct.unpack_from("<H", data, 4)[0]
    payload_len = total_len - 56
    if payload_len <= 0:
        return bytearray()
    return data[40:40 + payload_len]


# ---------------------------------------------------------------------------
# Crypto (matching APK's u2 / EncodeAndDecodeUtils classes)
# ---------------------------------------------------------------------------
class MideaSecurity:
    def __init__(self, app_key):
        self._app_key = app_key
        self._data_key = None
        self._data_iv = None

    def sign(self, url, data):
        """Sign an API request: sha256(path + sorted_params + appKey)."""
        path = urlparse(url).path
        query = sorted(data.items(), key=lambda x: x[0])
        query_str = unquote_plus(urlencode(query))
        return hashlib.sha256(
            (path + query_str + self._app_key).encode("ascii")
        ).hexdigest()

    def encrypt_password(self, login_id, password):
        """Encrypt password for login: sha256(loginId + sha256(password) + appKey)."""
        pw_hash = hashlib.sha256(password.encode("ascii")).hexdigest()
        return hashlib.sha256(
            (login_id + pw_hash + self._app_key).encode("ascii")
        ).hexdigest()

    def set_access_token(self, access_token_hex, random_data_hex):
        """Derive AES-CBC key and IV from the /new login response.

        The login returns accessToken and randomData as hex-encoded AES-CBC
        ciphertext.  Both are decrypted using a key derived from
        sha256(appKey) to obtain the session data_key and data_iv.
        """
        sha = hashlib.sha256(self._app_key.encode("ascii")).hexdigest()
        tmp_key = sha[:16].encode("ascii")
        tmp_iv = sha[16:32].encode("ascii")
        self._data_key = self._aes_cbc_decrypt(access_token_hex, tmp_key, tmp_iv)
        self._data_iv = self._aes_cbc_decrypt(random_data_hex, tmp_key, tmp_iv)

    def _aes_cbc_decrypt(self, hex_data, key, iv):
        encrypted = bytes.fromhex(hex_data)
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        return unpad(cipher.decrypt(encrypted), 16).decode("utf-8")

    def aes_encrypt(self, plaintext):
        """AES-CBC encrypt a string with the session data_key / data_iv."""
        raw = pad(plaintext.encode("utf-8"), 16)
        cipher = AES.new(
            self._data_key.encode("utf-8"),
            AES.MODE_CBC,
            iv=self._data_iv.encode("utf-8"),
        )
        return cipher.encrypt(raw).hex()

    def aes_decrypt(self, hex_data):
        """AES-CBC decrypt a hex string with the session data_key / data_iv."""
        encrypted = bytes.fromhex(hex_data)
        cipher = AES.new(
            self._data_key.encode("utf-8"),
            AES.MODE_CBC,
            iv=self._data_iv.encode("utf-8"),
        )
        return unpad(cipher.decrypt(encrypted), 16).decode("utf-8")


# ---------------------------------------------------------------------------
# AC command building and response parsing
# ---------------------------------------------------------------------------
def build_ac_status_query():
    """Build a type-0xAC status query command (33 bytes)."""
    data = bytearray([
        0xAA, 0x20, 0xAC, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03,
        0x41, 0x81, 0x00, 0xFF, 0x03, 0xFF, 0x00, 0x02,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01,
    ])
    data.append(crc8(data[10:]))
    data.append((~sum(data[1:]) + 1) & 0xFF)
    return data


def build_ac_set_command(power_on, mode=AC_MODE_AUTO, temp=24, fan=AC_FAN_AUTO,
                         prompt_tone=True, swing_v=False, swing_h=False):
    """Build a 33-byte AC SET command.

    The device requires SET commands to use the same 33-byte frame size as
    query commands (length byte = 0x20).  Longer formats (e.g. 35 bytes)
    are silently ignored by the device, returning error 3176.
    """
    power = 0x01 if power_on else 0
    tone = 0x40 if prompt_tone else 0
    mode_bits = (mode << 5) & 0xE0
    temp_int = int(temp)
    temp_half = 0x10 if round(temp * 2) % 2 != 0 else 0
    temp_bits = (temp_int - 16) & 0x0F
    fan_speed = fan & 0x7F
    swing = 0x30 | (0x0C if swing_v else 0) | (0x03 if swing_h else 0)

    data = bytearray([
        0xAA, 0x20, 0xAC, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02,
        0x40,
        power | tone,
        mode_bits | temp_half | temp_bits,
        fan_speed,
        0x00, 0x00, 0x00,
        swing,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01,
    ])
    data.append(crc8(data[10:]))
    data.append((~sum(data[1:]) + 1) & 0xFF)
    return data


def parse_ac_response(data):
    """Parse an AC status response and extract temperature readings."""
    if not data or len(data) < 2:
        return None

    # Find the 0xC0 response marker
    offset = -1
    for i in range(min(len(data), 20)):
        if data[i] == 0xC0:
            offset = i
            break
    if offset < 0:
        return None

    d = data[offset:]
    if len(d) < 13:
        return None

    running = (d[1] & 0x01) != 0
    mode_val = (d[2] >> 5) & 0x07
    mode = AC_MODES.get(mode_val, f"Unknown({mode_val})")
    target_temp = (d[2] & 0x0F) + 16
    if d[2] & 0x10:
        target_temp += 0.5

    indoor_temp = None
    if d[11] != 0 and d[11] != 0xFF:
        indoor_temp = (d[11] - 50) / 2.0
        if len(d) > 15:
            indoor_temp += 0.1 * (d[15] & 0x0F) * (1 if indoor_temp >= 0 else -1)

    outdoor_temp = None
    if d[12] != 0 and d[12] != 0xFF:
        outdoor_temp = (d[12] - 50) / 2.0
        if len(d) > 15:
            outdoor_temp += 0.1 * ((d[15] >> 4) & 0x0F) * (1 if outdoor_temp >= 0 else -1)

    return {
        "running": running,
        "mode": mode,
        "target_temp": target_temp,
        "indoor_temp": indoor_temp,
        "outdoor_temp": outdoor_temp,
    }


# ---------------------------------------------------------------------------
# Cloud client
# ---------------------------------------------------------------------------
class MideaCloud:
    """Client for the Midea Air cloud API (/new endpoints with CBC encryption)."""

    def __init__(self, account, password, app_name="midea_air", server=None):
        app = SUPPORTED_APPS.get(app_name, SUPPORTED_APPS["midea_air"])
        self.account = account
        self.password = password
        self.security = MideaSecurity(app["app_key"])
        self.app_id = app["app_id"]
        self.api_url = server or API_SERVERS["default"]
        self.session_id = None

    def _base_data(self):
        return {
            "appId": self.app_id,
            "format": "2",
            "clientType": "1",
            "language": "en_US",
            "src": self.app_id,
            "stamp": datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
            "deviceId": hashlib.md5(self.account.encode("ascii")).hexdigest()[:16],
        }

    def _api_request(self, endpoint, extra_args=None):
        data = self._base_data()
        if self.session_id:
            data["sessionId"] = self.session_id
        if extra_args:
            data.update(extra_args)

        url = self.api_url + endpoint
        data["sign"] = self.security.sign(url, data)

        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        code = str(payload.get("errorCode", "0"))
        if code != "0":
            raise Exception(
                f"API error {code}: {payload.get('msg', 'unknown')} ({endpoint})"
            )
        return payload.get("result")

    def login(self):
        """Authenticate with the Midea cloud and derive session encryption keys."""
        # Auto-detect regional server
        try:
            for region_url in API_SERVERS.values():
                self.api_url = region_url
                try:
                    resp = self._api_request(
                        "/v1/user/login/id/get",
                        {"loginAccount": self.account},
                    )
                    if resp and resp.get("loginId"):
                        break
                except Exception:
                    continue
            else:
                raise Exception("Could not reach any Midea API server")
        except Exception:
            raise

        login_id = resp["loginId"]

        password = self.security.encrypt_password(login_id, self.password)
        resp = self._api_request(
            "/v1/user/login/new",
            {
                "loginAccount": self.account,
                "password": password,
                "clientType": "1",
                "pushType": "5",
                "pushToken": "false",
                "encryptVersion": "1",
            },
        )
        self.session_id = resp["sessionId"]
        random_data = resp.get("randomData", "")
        if not random_data:
            raise Exception(
                "Login did not return randomData — the /new endpoint may not "
                "be supported for this account. Try a different --app."
            )
        self.security.set_access_token(resp["accessToken"], random_data)

    def list_appliances(self):
        """Return a dict of {id: {name, type, online, sn}} for all devices."""
        resp = self._api_request("/v1/appliance/user/list/get/new", {})
        devices = {}
        if resp and "list" in resp:
            for app in resp["list"]:
                dev_id = str(app.get("id", ""))
                devices[dev_id] = {
                    "name": app.get("name", "Unknown"),
                    "type": int(app.get("type", "0"), 16),
                    "online": app.get("onlineStatus") == "1",
                    "sn": app.get("sn", ""),
                }
        return devices

    def send_command(self, appliance_id, cmd_bytes):
        """Send a device command via the cloud relay and return the response bytes."""
        m0_frame = build_m0_frame(
            cmd_bytes, msg_type=0x0020, appliance_id_str=appliance_id
        )
        csv_data = bytes_to_signed_csv(m0_frame)
        encrypted_order = self.security.aes_encrypt(csv_data)

        resp = self._api_request(
            "/v1/appliance/transparent/send/new",
            {
                "applianceId": str(appliance_id),
                "order": encrypted_order,
                "funId": "0008",
                "timestamp": "true",
                "isFull": "false",
            },
        )
        if resp and resp.get("reply"):
            decrypted = self.security.aes_decrypt(resp["reply"])
            reply_bytes = signed_csv_to_bytes(decrypted)
            return parse_m0_frame(reply_bytes)
        return None

    def get_ac_status(self, appliance_id):
        """Query an AC unit and return parsed status, or None on failure."""
        cmd = build_ac_status_query()
        reply = self.send_command(appliance_id, cmd)
        if reply:
            return parse_ac_response(reply)
        return None

    def set_ac(self, appliance_id, power_on, mode=AC_MODE_AUTO, temp=24,
               fan=AC_FAN_AUTO, prompt_tone=True, swing_v=False, swing_h=False):
        """Set AC state and return the parsed response status."""
        cmd = build_ac_set_command(
            power_on=power_on, mode=mode, temp=temp, fan=fan,
            prompt_tone=prompt_tone, swing_v=swing_v, swing_h=swing_h,
        )
        reply = self.send_command(appliance_id, cmd)
        if reply:
            return parse_ac_response(reply)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Read temperatures from Midea AC units via the cloud."
    )
    parser.add_argument(
        "--account",
        default=os.environ.get("MIDEA_ACCOUNT"),
        help="Midea account email (or set MIDEA_ACCOUNT env var)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("MIDEA_PASSWORD"),
        help="Midea account password (or set MIDEA_PASSWORD env var)",
    )
    parser.add_argument(
        "--app",
        choices=list(SUPPORTED_APPS.keys()),
        default="midea_air",
        help="Which Midea app your account is registered with (default: midea_air)",
    )
    parser.add_argument(
        "--server",
        help="Override API server URL (default: auto-detect region)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list devices, don't query temperatures",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    # Control options
    ctrl = parser.add_argument_group("control", "Control an AC unit (requires --device)")
    ctrl.add_argument("--device", help="Device name or ID to control")
    ctrl.add_argument(
        "--on", dest="power", action="store_true", default=None, help="Turn AC on"
    )
    ctrl.add_argument(
        "--off", dest="power", action="store_false", help="Turn AC off"
    )
    ctrl.add_argument(
        "--mode",
        choices=["auto", "cool", "dry", "heat", "fan"],
        help="Set operating mode",
    )
    ctrl.add_argument("--temp", type=float, help="Set target temperature (16-30)")
    ctrl.add_argument(
        "--fan",
        choices=["auto", "low", "medium", "high"],
        help="Set fan speed",
    )

    args = parser.parse_args()

    if not args.account or not args.password:
        parser.error(
            "Account and password are required. Use --account/--password "
            "or set MIDEA_ACCOUNT/MIDEA_PASSWORD environment variables."
        )

    client = MideaCloud(
        args.account, args.password, app_name=args.app, server=args.server
    )

    print("Logging in...", file=sys.stderr)
    client.login()
    print(f"Logged in (server: {client.api_url})", file=sys.stderr)

    devices = client.list_appliances()
    if not devices:
        print("No devices found.", file=sys.stderr)
        sys.exit(1)

    # --- Control mode ---
    if args.device and args.power is not None:
        fan_map = {"auto": AC_FAN_AUTO, "low": AC_FAN_LOW,
                   "medium": AC_FAN_MEDIUM, "high": AC_FAN_HIGH}
        # Find device by name or ID
        target_id = None
        for dev_id, info in devices.items():
            if args.device.lower() in (info["name"].lower(), dev_id):
                target_id = dev_id
                break
        if not target_id:
            print(f"Device '{args.device}' not found.", file=sys.stderr)
            sys.exit(1)

        # Get current status to use as defaults
        current = client.get_ac_status(target_id)
        cur_mode = AC_MODES_BY_NAME.get(
            (current["mode"].lower() if current else "auto"), AC_MODE_AUTO
        )
        cur_temp = current["target_temp"] if current else 24

        mode = AC_MODES_BY_NAME[args.mode] if args.mode else cur_mode
        temp = args.temp if args.temp is not None else cur_temp
        fan = fan_map.get(args.fan, AC_FAN_AUTO) if args.fan else AC_FAN_AUTO

        result = client.set_ac(
            target_id, power_on=args.power, mode=mode, temp=temp, fan=fan,
        )
        if result:
            action = "ON" if args.power else "OFF"
            print(f"{devices[target_id]['name']}: {action}")
            print(f"  Mode:    {result['mode']}")
            print(f"  Target:  {result['target_temp']} C")
            print(f"  Running: {result['running']}")
        else:
            print("Command sent (no reply from device)", file=sys.stderr)
        return

    if args.list_only:
        for dev_id, info in devices.items():
            status = "ONLINE" if info["online"] else "OFFLINE"
            print(f"{info['name']}  ID={dev_id}  type=0x{info['type']:02X}  [{status}]")
        return

    if args.json:
        import json

        results = {}
        for dev_id, info in devices.items():
            if info["type"] != DEVICE_TYPE_AC or not info["online"]:
                continue
            try:
                status = client.get_ac_status(dev_id)
                if status:
                    results[info["name"]] = status
            except Exception as e:
                results[info["name"]] = {"error": str(e)}
        print(json.dumps(results, indent=2))
        return

    # Human-readable output
    print()
    for dev_id, info in devices.items():
        if info["type"] != DEVICE_TYPE_AC:
            continue
        if not info["online"]:
            print(f"{info['name']}: OFFLINE\n")
            continue

        print(f"{info['name']}:")
        try:
            status = client.get_ac_status(dev_id)
            if status:
                indoor = (
                    f"{status['indoor_temp']:.1f} C"
                    if status["indoor_temp"] is not None
                    else "N/A"
                )
                outdoor = (
                    f"{status['outdoor_temp']:.1f} C"
                    if status["outdoor_temp"] is not None
                    else "N/A"
                )
                print(f"  Indoor:  {indoor}")
                print(f"  Outdoor: {outdoor}")
                print(f"  Target:  {status['target_temp']} C")
                print(f"  Running: {status['running']}")
                print(f"  Mode:    {status['mode']}")
            else:
                print("  No reply from device")
        except Exception as e:
            print(f"  Error: {e}")
        print()


if __name__ == "__main__":
    main()
