#!/usr/bin/env python3
"""Read-only Prometheus exporter for Midea Cloud air-conditioner telemetry."""

from __future__ import annotations

import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from midea_cloud import MideaCloud

PORT = int(os.environ.get("MIDEA_METRICS_PORT", "9899"))
POLL_INTERVAL = max(60, int(os.environ.get("MIDEA_METRICS_INTERVAL_SECONDS", "300")))
ACCOUNT = os.environ.get("MIDEA_ACCOUNT", "")
PASSWORD = os.environ.get("MIDEA_PASSWORD", "")
APP = os.environ.get("MIDEA_APP", "midea_air")
SERVER = os.environ.get("MIDEA_SERVER")

METRICS_LOCK = threading.Lock()
METRICS = {
    "configured": int(bool(ACCOUNT and PASSWORD)),
    "up": 0,
    "last_success": 0.0,
    "devices": {},
}


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


def render_metrics():
    with METRICS_LOCK:
        snapshot = {
            "configured": METRICS["configured"],
            "up": METRICS["up"],
            "last_success": METRICS["last_success"],
            "devices": dict(METRICS["devices"]),
        }

    lines = [
        "# HELP midea_checker_configured Whether Midea Cloud credentials are configured.",
        "# TYPE midea_checker_configured gauge",
        f'midea_checker_configured {snapshot["configured"]}',
        "# HELP midea_checker_up Whether the latest complete Midea Cloud poll succeeded.",
        "# TYPE midea_checker_up gauge",
        f'midea_checker_up {snapshot["up"]}',
        "# HELP midea_checker_last_success_timestamp Unix timestamp of the last complete successful poll.",
        "# TYPE midea_checker_last_success_timestamp gauge",
        f'midea_checker_last_success_timestamp {snapshot["last_success"]}',
    ]

    definitions = (
        ("midea_ac_online", "Whether Midea Cloud reports the air conditioner online."),
        ("midea_ac_running", "Whether the air conditioner is currently running."),
        ("midea_ac_indoor_temperature_celsius", "Indoor temperature reported by the air conditioner in Celsius."),
        ("midea_ac_outdoor_temperature_celsius", "Outdoor temperature reported by the air conditioner in Celsius."),
        ("midea_ac_target_temperature_celsius", "Target temperature configured on the air conditioner in Celsius."),
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

    return "\n".join(lines) + "\n"


def poll_once():
    if not ACCOUNT or not PASSWORD:
        with METRICS_LOCK:
            METRICS["configured"] = 0
            METRICS["up"] = 0
            METRICS["devices"] = {}
        raise RuntimeError("Midea credentials are not configured")

    client = MideaCloud(ACCOUNT, PASSWORD, app_name=APP, server=SERVER)
    client.login()
    appliances = client.list_appliances()
    devices = {}
    errors = []

    for appliance_id, info in appliances.items():
        if info.get("type") != 0xAC:
            continue

        online = bool(info.get("online"))
        values = {"online": int(online), "running": None}
        if online:
            try:
                status = client.get_ac_status(appliance_id)
                if not status:
                    raise RuntimeError("empty device status")
                running = status.get("running")
                values["running"] = int(bool(running)) if running is not None else None
                for field in ("indoor_temp", "outdoor_temp", "target_temp"):
                    values[field] = _number(status.get(field))
            except Exception as exc:
                errors.append(type(exc).__name__)
        devices[info.get("name") or appliance_id] = values

    with METRICS_LOCK:
        METRICS["configured"] = 1
        METRICS["devices"] = devices
        if errors:
            METRICS["up"] = 0
        else:
            METRICS["up"] = 1
            METRICS["last_success"] = time.time()

    if errors:
        raise RuntimeError("one or more device status queries failed")


def poll_forever():
    while True:
        try:
            poll_once()
        except Exception as exc:
            # Do not log exception details: API errors can contain account or
            # device identifiers. The exception class is enough to find the
            # failure category in the journal.
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
    threading.Thread(target=poll_forever, name="midea-poller", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
