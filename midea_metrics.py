#!/usr/bin/env python3
"""Prometheus exporter and optional hysteresis thermostat for Midea ACs.

The thermostat is opt-in. It only controls explicitly allowlisted device names.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
import math
import os
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from midea_cloud import AC_FAN_AUTO, AC_MODES_BY_NAME, MideaCloud

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
THERMOSTAT_MIN_ON_SECONDS = max(0, int(os.environ.get("MIDEA_THERMOSTAT_MIN_ON_SECONDS", "1800")))
THERMOSTAT_MIN_OFF_SECONDS = max(0, int(os.environ.get("MIDEA_THERMOSTAT_MIN_OFF_SECONDS", "1800")))
THERMOSTAT_WARMUP_TIMEOUT_SECONDS = max(300, int(os.environ.get("MIDEA_THERMOSTAT_WARMUP_TIMEOUT_SECONDS", "600")))
THERMOSTAT_PREHEAT_C = float(os.environ.get("MIDEA_THERMOSTAT_PREHEAT_C", "7"))
WEATHER_URL = os.environ.get(
    "MIDEA_WEATHER_URL",
    "https://www.idokep.hu/elorejelzes/M%C3%81TRASZENTISTV%C3%81N",
)
WEATHER_CACHE_SECONDS = max(900, int(os.environ.get("MIDEA_WEATHER_CACHE_SECONDS", "1800")))
WEATHER_COLD_FORECAST_C = float(os.environ.get("MIDEA_WEATHER_COLD_FORECAST_C", "0"))
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
    "thermostat_warmup_failed": {},
}

CLIENT = None
CLIENT_LOGIN_AT = 0.0
CLIENT_REFRESH_SECONDS = max(60, POLL_INTERVAL - 30)


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


class _HourlyForecastParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.current = None
        self.field = None

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        classes = set(dict(attrs).get("class", "").split())
        if "wide-hourly-forecast-card" in classes:
            self._finish_item()
            self.current = {}
        elif self.current is not None and "wide-hourly-forecast-hour" in classes:
            self.field = "hour"
        elif self.current is not None and "tempValue" in classes:
            self.field = "temperature"

    def handle_data(self, data):
        if self.current is not None and self.field:
            self.current.setdefault(self.field, "")
            self.current[self.field] += data.strip()

    def handle_endtag(self, tag):
        if tag == "div" and self.field:
            self.field = None

    def _finish_item(self):
        if not self.current:
            return
        hour = self.current.get("hour", "").strip()
        raw = self.current.get("temperature", "").strip()
        try:
            temperature = float(raw)
        except (TypeError, ValueError):
            temperature = None
        if hour and temperature is not None:
            self.items.append({"hour": hour, "temperature": temperature})
        self.current = None
        self.field = None

    def close(self):
        super().close()
        self._finish_item()


WEATHER_CACHE = {"fetched_at": 0.0, "last_success": 0.0, "hours": None}


def _weather_forecast(now):
    if now - WEATHER_CACHE["fetched_at"] < WEATHER_CACHE_SECONDS:
        return WEATHER_CACHE["hours"]
    request = Request(
        WEATHER_URL,
        headers={"User-Agent": "MideaHouseHeating/1.0"},
    )
    try:
        with urlopen(request, timeout=12) as response:
            document = response.read(2_000_000).decode("utf-8", "replace")
        parser = _HourlyForecastParser()
        parser.feed(document)
        parser.close()
        hours = [item for item in parser.items if item["hour"] and item["temperature"] is not None]
        if not hours:
            raise ValueError("hourly forecast was not found")
        WEATHER_CACHE.update(fetched_at=now, last_success=now, hours=hours)
        return hours
    except Exception as exc:
        # Keep the last usable forecast; temperature hysteresis remains active
        # if the weather page is unavailable.
        if WEATHER_CACHE["hours"] is None:
            print(f"Midea weather forecast unavailable ({type(exc).__name__})", flush=True)
        WEATHER_CACHE["fetched_at"] = now
        if now - WEATHER_CACHE["last_success"] > 6 * 3600:
            return None
        return WEATHER_CACHE["hours"]


def _load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except Exception:
        print("Midea thermostat state file could not be read; starting with an empty controller state.", flush=True)
        state = {}

    # Ignore the old latch-only format. Device status is the source of truth
    # after this controller version starts.
    if not isinstance(state, dict) or state.get("version") != 2:
        state = {}
    keys = ("heating", "last_action", "on_started", "off_since", "warmup_failed")
    return {key: state.get(key, {}) if isinstance(state.get(key, {}), dict) else {} for key in keys}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    payload = {"version": 2, **state}
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
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
    # The Midea SET frame always carries mode/target/fan fields. Read them first
    # and echo them unchanged so a power toggle cannot replace saved settings.
    current = client.get_ac_status(appliance_id)
    if not current:
        raise RuntimeError("could not read current AC settings before power command")
    mode = AC_MODES_BY_NAME.get(str(current.get("mode", "")).lower())
    target = _number(current.get("target_temp"))
    fan = current.get("fan_speed")
    if fan is None:
        fan = AC_FAN_AUTO
    else:
        fan = int(fan)
    if mode is None or target is None:
        raise RuntimeError("current AC settings are incomplete")
    return client.set_ac(
        appliance_id,
        power_on=power_on,
        mode=mode,
        temp=target,
        fan=fan,
        prompt_tone=False,
    )


def _apply_thermostat(client, appliance_id, device, values, state, now):
    if not THERMOSTAT_ENABLED or device not in THERMOSTAT_DEVICE_NAMES:
        return False
    if not values.get("online") or values.get("indoor_temp") is None:
        return False

    temperature = values["indoor_temp"]
    running = bool(values.get("running"))
    mode_is_heat = str(values.get("mode", "")).lower() == "heat"
    heating = state["heating"]
    last_action = state["last_action"]
    on_started = state["on_started"]
    off_since = state["off_since"]
    warmup_failed = state["warmup_failed"]
    action = None

    if running:
        heating[device] = mode_is_heat
        if device not in on_started:
            on_started[device] = {"at": now, "temperature": temperature}
        cycle = on_started[device]
        elapsed = now - float(cycle.get("at", now))
        baseline = _number(cycle.get("temperature"))
        gained = temperature - baseline if baseline is not None else 0
        if not mode_is_heat:
            warmup_failed[device] = True
        elif elapsed >= THERMOSTAT_WARMUP_TIMEOUT_SECONDS and gained < 1.0:
            warmup_failed[device] = True
        elif gained >= 1.0:
            warmup_failed.pop(device, None)

        if temperature >= THERMOSTAT_OFF_C and elapsed >= THERMOSTAT_MIN_ON_SECONDS:
            action = False
    else:
        was_running = bool(heating.pop(device, False))
        on_started.pop(device, None)
        warmup_failed.pop(device, None)
        if was_running:
            off_since[device] = now

        # If a cold night is forecast, use the warmer hours to preheat sooner.
        forecast = _weather_forecast(now)
        outdoor = _number(values.get("outdoor_temp"))
        forecast_values = [item["temperature"] for item in (forecast or [])[:8]]
        forecast_is_freezing = bool(
            forecast_values and min(forecast_values) <= WEATHER_COLD_FORECAST_C
        )
        trigger = THERMOSTAT_ON_C
        if forecast_is_freezing and outdoor is not None and outdoor >= 3:
            trigger = max(trigger, THERMOSTAT_PREHEAT_C)

        if temperature <= trigger:
            last_off = _number(off_since.get(device))
            minimum_off_elapsed = (
                last_off is None or now - last_off >= THERMOSTAT_MIN_OFF_SECONDS
            )
            # Prevent short cycling, but never delay protection at/below 6 C.
            if minimum_off_elapsed or temperature <= THERMOSTAT_ON_C:
                action = True

    if action is None:
        _save_state(state)
        return False

    result = _set_ac(client, appliance_id, action)
    if not result:
        raise RuntimeError("AC did not acknowledge power command")

    last_action[device] = now
    if action:
        heating[device] = False  # Waiting for the next poll to verify Heat mode.
        on_started[device] = {"at": now, "temperature": temperature}
        off_since.pop(device, None)
        warmup_failed.pop(device, None)
        verb = "power on"
    else:
        heating.pop(device, None)
        on_started.pop(device, None)
        off_since[device] = now
        warmup_failed.pop(device, None)
        verb = "power off"
    _save_state(state)
    print(
        f"Midea thermostat accepted {verb} for {device} "
        f"(indoor={temperature:.1f}C, trigger={THERMOSTAT_ON_C:.1f}C, "
        f"stop={THERMOSTAT_OFF_C:.1f}C)",
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
            "thermostat_warmup_failed": dict(METRICS["thermostat_warmup_failed"]),
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
        "# HELP midea_thermostat_warmup_failed Whether an AC failed to warm its sensor by 1 C within the allowed time.",
        "# TYPE midea_thermostat_warmup_failed gauge",
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
            failed = snapshot["thermostat_warmup_failed"].get(device, False)
            lines.append(_sample("midea_thermostat_warmup_failed", {"device": device}, int(bool(failed))))

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
        METRICS["thermostat_warmup_failed"] = dict(state["warmup_failed"])
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
