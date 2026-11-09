#!/usr/bin/env python3
"""Prometheus exporter and optional hysteresis thermostat for Midea ACs.

The thermostat is opt-in. It only controls explicitly allowlisted device names.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from midea_cloud import AC_FAN_AUTO, AC_MODE_HEAT, MideaCloud

PORT = int(os.environ.get("MIDEA_METRICS_PORT", "9899"))
POLL_INTERVAL = max(60, int(os.environ.get("MIDEA_METRICS_INTERVAL_SECONDS", "300")))
ACCOUNT = os.environ.get("MIDEA_ACCOUNT", "")
PASSWORD = os.environ.get("MIDEA_PASSWORD", "")
APP = os.environ.get("MIDEA_APP", "midea_air")
SERVER = os.environ.get("MIDEA_SERVER")

THERMOSTAT_ENABLED = os.environ.get("MIDEA_THERMOSTAT_ENABLED", "0") == "1"
THERMOSTAT_ON_C = float(os.environ.get("MIDEA_THERMOSTAT_ON_C", "6"))
THERMOSTAT_OFF_C = float(os.environ.get("MIDEA_THERMOSTAT_OFF_C", "10"))
THERMOSTAT_SETPOINT_C = max(17, float(os.environ.get("MIDEA_THERMOSTAT_SETPOINT_C", "17")))
THERMOSTAT_DEVICE_NAMES = {
    name.strip()
    for name in os.environ.get("MIDEA_THERMOSTAT_DEVICE_NAMES", "").split(",")
    if name.strip()
}
STATE_FILE = Path(os.environ.get(
    "MIDEA_THERMOSTAT_STATE_FILE",
    "/var/lib/midea-thermostat/state.json",
))

METRICS_LOCK = threading.Lock()
METRICS = {
    "configured": int(bool(ACCOUNT and PASSWORD)),
    "up": 0,
    "last_success": 0.0,
    "devices": {},
    "thermostat_error": 0,
    "thermostat_heating": {},
}

CLIENT = None
CLIENT_LOGIN_AT = 0.0
CLIENT_REFRESH_SECONDS = 1800


def _number(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(name, labels, value):
    rendered = ",".join(f'{key}="{_escape(val)}"' for key, val in sorted(labels.items()))
    return f"{name}{{{rendered}}} {value}"


def _load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except Exception:
        print("Midea thermostat state file could not be read; starting with an empty latch state.", flush=True)
        state = {}

    if not isinstance(state, dict):
        state = {}
    heating = state.get("heating", {})
    last_action = state.get("last_action", {})
    return {
        "heating": heating if isinstance(heating, dict) else {},
        "last_action": last_action if isinstance(last_action, dict) else {},
    }


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(STATE_FILE)


def _client():
    global CLIENT, CLIENT_LOGIN_AT
    now = time.time()
    if CLIENT is None or now - CLIENT_LOGIN_AT >= CLIENT_REFRESH_SECONDS:
        client = MideaCloud(ACCOUNT, PASSWORD, app_name=APP, server=SERVER)
        client.login()
        CLIENT = client
        CLIENT_LOGIN_AT = now
    return CLIENT


def _set_ac(client, appliance_id, power_on):
    return client.set_ac(
        appliance_id,
        power_on=power_on,
        mode=AC_MODE_HEAT,
        temp=THERMOSTAT_SETPOINT_C,
        fan=AC_FAN_AUTO,
        prompt_tone=False,
    )


def _apply_thermostat(client, appliance_id, device, values, state, now):
    if not THERMOSTAT_ENABLED or device not in THERMOSTAT_DEVICE_NAMES:
        return False
    if not values.get("online") or values.get("indoor_temp") is None:
        return False

    temperature = values["indoor_temp"]
    heating = state["heating"]
    last_action = state["last_action"]
    active = bool(heating.get(device))

    # Recover a prior successful heat command if the state file was lost.
    current_mode_is_heat = str(values.get("mode", "")).lower() == "heat"
    current_target = _number(values.get("target_temp"))
    if not active and temperature < THERMOSTAT_OFF_C:
        active = (
            current_mode_is_heat
            and current_target == THERMOSTAT_SETPOINT_C
            and bool(values.get("running"))
        )
        if active:
            heating[device] = True
            last_action.setdefault(device, now)
            _save_state(state)

    action = None
    if (
        not active
        and temperature >= THERMOSTAT_OFF_C
        and current_mode_is_heat
        and current_target == THERMOSTAT_SETPOINT_C
    ):
        action = False
    elif active and temperature >= THERMOSTAT_OFF_C:
        action = False
    elif not active and temperature <= THERMOSTAT_ON_C:
        action = True
    elif active and temperature < THERMOSTAT_OFF_C:
        mode_is_heat = str(values.get("mode", "")).lower() == "heat"
        target = _number(values.get("target_temp"))
        retry_due = now - float(last_action.get(device, 0)) >= 300
        if (not mode_is_heat or target != THERMOSTAT_SETPOINT_C) and retry_due:
            action = True

    if action is None:
        return False

    result = _set_ac(client, appliance_id, action)
    if not result:
        raise RuntimeError("AC did not acknowledge thermostat command")

    last_action[device] = now
    if action:
        heating[device] = True
        verb = "heat on"
    else:
        heating.pop(device, None)
        last_action.pop(device, None)
        verb = "heat off"
    _save_state(state)
    print(
        f"Midea thermostat accepted {verb} for {device} "
        f"(room={temperature:.1f}C, setpoint={THERMOSTAT_SETPOINT_C:.0f}C)",
        flush=True,
    )
    return True


def render_metrics():
    with METRICS_LOCK:
        snapshot = {
            "configured": METRICS["configured"],
            "up": METRICS["up"],
            "last_success": METRICS["last_success"],
            "devices": dict(METRICS["devices"]),
            "thermostat_error": METRICS["thermostat_error"],
            "thermostat_heating": dict(METRICS["thermostat_heating"]),
        }

    lines = [
        "# HELP midea_checker_configured Whether Midea Cloud credentials are configured.",
        "# TYPE midea_checker_configured gauge",
        f'midea_checker_configured {snapshot["configured"]}',
        "# HELP midea_checker_up Whether the latest complete Midea Cloud status poll succeeded.",
        "# TYPE midea_checker_up gauge",
        f'midea_checker_up {snapshot["up"]}',
        "# HELP midea_checker_last_success_timestamp Unix timestamp of the last complete successful poll.",
        "# TYPE midea_checker_last_success_timestamp gauge",
        f'midea_checker_last_success_timestamp {snapshot["last_success"]}',
        "# HELP midea_thermostat_enabled Whether the automatic Midea temperature controller is enabled.",
        "# TYPE midea_thermostat_enabled gauge",
        f"midea_thermostat_enabled {int(THERMOSTAT_ENABLED)}",
        "# HELP midea_thermostat_control_error Whether a thermostat command failed during the latest poll.",
        "# TYPE midea_thermostat_control_error gauge",
        f'midea_thermostat_control_error {snapshot["thermostat_error"]}',
    ]

    definitions = (
        ("midea_ac_online", "Whether Midea Cloud reports the air conditioner online."),
        ("midea_ac_running", "Whether the air conditioner is currently running."),
        ("midea_ac_indoor_temperature_celsius", "Indoor temperature reported by the air conditioner in Celsius."),
        ("midea_ac_outdoor_temperature_celsius", "Outdoor temperature reported by the air conditioner in Celsius."),
        ("midea_ac_target_temperature_celsius", "Target temperature configured on the air conditioner in Celsius."),
        ("midea_thermostat_heating", "Whether the thermostat hysteresis latch is currently heating this device."),
    )
    for name, help_text in definitions:
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))

    for device, values in sorted(snapshot["devices"].items()):
        labels = {"device": device}
        for metric, key in (
            ("midea_ac_online", "online"),
            ("midea_ac_running", "running"),
            ("midea_ac_indoor_temperature_celsius", "indoor_temp"),
            ("midea_ac_outdoor_temperature_celsius", "outdoor_temp"),
            ("midea_ac_target_temperature_celsius", "target_temp"),
        ):
            value = values.get(key)
            if value is not None:
                lines.append(_sample(metric, labels, value))

    if THERMOSTAT_ENABLED:
        for device in sorted(snapshot["devices"]):
            active = snapshot["thermostat_heating"].get(device, False)
            lines.append(_sample("midea_thermostat_heating", {"device": device}, int(bool(active))))

    return "\n".join(lines) + "\n"


def poll_once():
    global CLIENT
    if not ACCOUNT or not PASSWORD:
        with METRICS_LOCK:
            METRICS["configured"] = 0
            METRICS["up"] = 0
            METRICS["devices"] = {}
        raise RuntimeError("Midea credentials are not configured")

    client = _client()
    appliances = client.list_appliances()
    devices = {}
    errors = []
    thermostat_error = 0
    state = _load_state()
    now = time.time()

    for appliance_id, info in appliances.items():
        if info.get("type") != 0xAC:
            continue

        name = info.get("name") or appliance_id
        values = {
            "online": int(bool(info.get("online"))),
            "running": None,
            "mode": None,
            "target_temp": None,
        }
        if info.get("online"):
            try:
                status = client.get_ac_status(appliance_id)
                if not status:
                    raise RuntimeError("empty device status")
                running = status.get("running")
                values["running"] = int(bool(running)) if running is not None else None
                values["mode"] = status.get("mode")
                for field in ("indoor_temp", "outdoor_temp", "target_temp"):
                    values[field] = _number(status.get(field))
            except Exception as exc:
                errors.append(type(exc).__name__)
                devices[name] = values
                continue

        devices[name] = values
        if name in THERMOSTAT_DEVICE_NAMES:
            try:
                _apply_thermostat(client, appliance_id, name, values, state, now)
            except Exception as exc:
                thermostat_error = 1
                print(
                    f"Midea thermostat command failed for {name} ({type(exc).__name__})",
                    flush=True,
                )

    with METRICS_LOCK:
        METRICS["configured"] = 1
        METRICS["devices"] = devices
        METRICS["thermostat_error"] = thermostat_error
        METRICS["thermostat_heating"] = dict(state["heating"])
        if errors:
            METRICS["up"] = 0
        else:
            METRICS["up"] = 1
            METRICS["last_success"] = time.time()

    if errors:
        CLIENT = None
        raise RuntimeError("one or more device status queries failed")


def poll_forever():
    global CLIENT, CLIENT_LOGIN_AT
    while True:
        try:
            poll_once()
        except Exception as exc:
            CLIENT = None
            CLIENT_LOGIN_AT = 0.0
            with METRICS_LOCK:
                METRICS["up"] = 0
                METRICS["devices"] = {}
            # Do not log exception details; API errors can contain account or
            # device identifiers. The exception class is enough to diagnose
            # the failure category in the journal.
            print(f"Midea Cloud poll failed ({type(exc).__name__})", flush=True)
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = render_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/healthz":
            with METRICS_LOCK:
                healthy = METRICS["up"] == 1
            body = b"ok\n" if healthy else b"poll unavailable\n"
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


if __name__ == "__main__":
    if THERMOSTAT_ENABLED and not THERMOSTAT_DEVICE_NAMES:
        raise SystemExit("MIDEA_THERMOSTAT_ENABLED requires an explicit device-name allowlist")
    if THERMOSTAT_ENABLED and THERMOSTAT_OFF_C <= THERMOSTAT_ON_C:
        raise SystemExit("MIDEA_THERMOSTAT_OFF_C must be greater than MIDEA_THERMOSTAT_ON_C")
    threading.Thread(target=poll_forever, name="midea-poller", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
