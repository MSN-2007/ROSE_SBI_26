"""
actions/hardware_control.py — Hardware, IoT, and embedded microcontroller integration.

Enables JARVIS to connect with and control hardware devices:
  * ESP32 & ESP8266 (NodeMCU, Wemos, custom MicroPython/Arduino webservers, Tasmota, ESPHome, WLED)
  * Raspberry Pi (HTTP endpoints, Flask/FastAPI REST APIs, remote scripts, MQTT)
  * Relays, switches, smart plugs, LEDs, motors, and servos (Turn ON / OFF / TOGGLE / PWM)
  * Sensors & Telemetry (Read temperature, humidity, light, distance, motion, ADC/pin readouts)
  * Protocols: HTTP/REST, MQTT, Raw TCP/UDP Sockets, and USB Serial (UART)
  * Local network scanning to automatically discover active hardware devices on the LAN
  * Persistent device registry saved in config/hardware_devices.json
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from core import outcome

# Optional MQTT import (bundled in requirements.txt)
try:
    import paho.mqtt.client as mqtt
    _MQTT_AVAILABLE = True
except ImportError:
    _MQTT_AVAILABLE = False

# Optional serial import
try:
    import serial
    import serial.tools.list_ports
    _SERIAL_AVAILABLE = True
except ImportError:
    _SERIAL_AVAILABLE = False


_BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _BASE_DIR / "config" / "hardware_devices.json"
_CONFIG_LOCK = threading.RLock()


# ── Configuration & Device Registry ──────────────────────────────────────────

def _load_config() -> dict:
    """Load hardware devices configuration safely."""
    with _CONFIG_LOCK:
        if not _CONFIG_FILE.exists():
            return {"devices": {}, "mqtt_broker": {"host": "127.0.0.1", "port": 1883}}
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"devices": {}}
        except Exception as e:
            print(f"[Hardware] Warning: Could not read {_CONFIG_FILE.name}: {e}")
            return {"devices": {}, "mqtt_broker": {"host": "127.0.0.1", "port": 1883}}


def _save_config(data: dict) -> None:
    """Save hardware devices configuration atomically."""
    with _CONFIG_LOCK:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_FILE.with_name(_CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, _CONFIG_FILE)
                return
            except PermissionError:
                time.sleep(0.05)


def _get_device_entry(target: str, config: dict) -> tuple[str, dict]:
    """Look up a device by exact name, normalized name, or construct a dynamic entry for an IP/host."""
    target_clean = str(target or "").strip()
    devices = config.get("devices", {})

    # 1. Exact match
    if target_clean in devices:
        return target_clean, devices[target_clean]

    # 2. Case-insensitive / slug match
    norm = target_clean.lower().replace(" ", "_").replace("-", "_")
    for name, entry in devices.items():
        if name.lower().replace(" ", "_").replace("-", "_") == norm:
            return name, entry

    # 3. If target is an IP or hostname, create a dynamic HTTP entry
    is_ip = re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$", target_clean)
    is_host = target_clean.endswith(".local") or target_clean.startswith("http://") or target_clean.startswith("https://")
    if is_ip or is_host:
        host = target_clean
        port = 80
        if "://" in host:
            parsed = urlparse(host)
            host = parsed.hostname or host
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        elif ":" in host:
            parts = host.split(":")
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                port = 80
        return target_clean, {
            "name": target_clean,
            "type": "generic",
            "protocol": "http",
            "host": host,
            "port": port,
        }

    return target_clean, {}


# ── HTTP / REST Hardware Handler ─────────────────────────────────────────────

def _http_request(url: str, method: str = "GET", params: dict | None = None,
                  json_data: dict | None = None, timeout: float = 3.5) -> tuple[bool, int, str]:
    """Perform an HTTP request with safe timeouts and exception handling."""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"http://{url}"
    try:
        if method.upper() == "POST":
            r = requests.post(url, params=params, json=json_data, timeout=timeout)
        else:
            r = requests.get(url, params=params, timeout=timeout)
        return (r.status_code < 400), r.status_code, r.text.strip()
    except requests.exceptions.Timeout:
        return False, 408, f"Connection to {url} timed out after {timeout}s"
    except requests.exceptions.ConnectionError as e:
        return False, 503, f"Connection refused or device unreachable at {url}"
    except Exception as e:
        return False, 500, f"HTTP error: {e}"


def _format_sensor_reading(raw_text: str) -> dict[str, Any]:
    """Parse sensor payload (JSON or text) into clean telemetry facts."""
    facts = {}
    try:
        data = json.loads(raw_text)
        if isinstance(data, dict):
            # Flatten or extract standard sensor metrics
            for k, v in data.items():
                k_clean = str(k).lower().strip()
                if "temp" in k_clean:
                    unit = "°C" if isinstance(v, (int, float)) and v < 60 else ""
                    facts["temperature"] = f"{v}{unit}"
                elif "hum" in k_clean:
                    facts["humidity"] = f"{v}%" if isinstance(v, (int, float)) else str(v)
                elif "press" in k_clean:
                    facts["pressure"] = f"{v} hPa"
                elif "dist" in k_clean:
                    facts["distance"] = f"{v} cm"
                elif "lux" in k_clean or "light" in k_clean:
                    facts["light"] = f"{v} lux"
                elif "motion" in k_clean or "pir" in k_clean:
                    facts["motion"] = "detected" if v in (True, 1, "1", "true") else "clear"
                elif "relay" in k_clean or "state" in k_clean or "power" in k_clean:
                    facts[k_clean] = str(v).upper()
                else:
                    facts[k_clean] = str(v)
            facts["summary"] = ", ".join(f"{k}: {val}" for k, val in facts.items())
            return facts
    except (ValueError, TypeError):
        pass

    # Regex fallback on plain text (e.g. "Temp: 24.5 C, Humidity: 55%")
    temp_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:°?C|C\b|degC)", raw_text, re.I)
    if temp_m:
        facts["temperature"] = f"{temp_m.group(1)}°C"
    hum_m = re.search(r"(\d+(?:\.\d+)?)\s*%", raw_text)
    if hum_m:
        facts["humidity"] = f"{hum_m.group(1)}%"

    facts["raw_text"] = raw_text[:200]
    facts["summary"] = raw_text[:120] if raw_text else "No reading returned"
    return facts


# ── MQTT Hardware Handler ─────────────────────────────────────────────────────

def _mqtt_client_factory(client_id: str):
    """Instantiate a Paho MQTT client compatible with versions 1.x and 2.x."""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id)
    return mqtt.Client(client_id)


def _mqtt_publish(broker: str, port: int, topic: str, payload: str,
                  username: str = "", password: str = "", timeout: float = 3.0) -> tuple[bool, str]:
    """Publish a command to an MQTT broker."""
    if not _MQTT_AVAILABLE:
        return False, "paho-mqtt library is not installed in the environment."
    try:
        client = _mqtt_client_factory(f"jarvis_pub_{int(time.time() * 1000) % 10000}")
        if username:
            client.username_pw_set(username, password)
        client.connect(broker, port, keepalive=10)
        client.loop_start()
        inf = client.publish(topic, payload, qos=1)
        inf.wait_for_publish(timeout=timeout)
        client.loop_stop()
        client.disconnect()
        return True, f"Published '{payload}' to topic '{topic}'"
    except Exception as e:
        return False, f"MQTT publish failed: {e}"


def _mqtt_read_once(broker: str, port: int, topic: str,
                    username: str = "", password: str = "", timeout: float = 2.5) -> tuple[bool, str]:
    """Subscribe to a topic and capture the latest or retained message."""
    if not _MQTT_AVAILABLE:
        return False, "paho-mqtt library is not installed."
    result = {"received": False, "payload": ""}
    event = threading.Event()

    def on_message(client, userdata, msg):
        try:
            result["payload"] = msg.payload.decode("utf-8", errors="replace").strip()
            result["received"] = True
            event.set()
        except Exception:
            pass

    try:
        client = _mqtt_client_factory(f"jarvis_sub_{int(time.time() * 1000) % 10000}")
        if username:
            client.username_pw_set(username, password)
        client.on_message = on_message
        client.connect(broker, port, keepalive=10)
        client.subscribe(topic, qos=0)
        client.loop_start()
        event.wait(timeout=timeout)
        client.loop_stop()
        client.disconnect()

        if result["received"]:
            return True, result["payload"]
        return False, f"No MQTT message received on topic '{topic}' within {timeout}s"
    except Exception as e:
        return False, f"MQTT subscription failed: {e}"


# ── Socket / Raw TCP Handler ──────────────────────────────────────────────────

def _socket_command(host: str, port: int, command: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Send a plain text or byte command over a raw TCP socket to microcontrollers."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            if not command.endswith("\n"):
                command += "\n"
            s.sendall(command.encode("utf-8"))
            data = s.recv(1024)
            response = data.decode("utf-8", errors="replace").strip()
            return True, response or "OK (Command delivered)"
    except socket.timeout:
        return False, f"Socket connection to {host}:{port} timed out after {timeout}s"
    except Exception as e:
        return False, f"Socket communication failed: {e}"


# ── Serial / USB Handler ──────────────────────────────────────────────────────

def _serial_command(port: str, command: str, baudrate: int = 115200, timeout: float = 2.0) -> tuple[bool, str]:
    """Communicate with an ESP or Arduino over USB COM port."""
    if not _SERIAL_AVAILABLE:
        return False, "USB Serial requires pyserial. Install with: pip install pyserial"
    try:
        with serial.Serial(port, baudrate=baudrate, timeout=timeout) as ser:
            time.sleep(0.1)
            ser.write(f"{command.strip()}\n".encode("utf-8"))
            ser.flush()
            response = ser.readline().decode("utf-8", errors="replace").strip()
            return True, response or "OK (Serial command delivered)"
    except Exception as e:
        return False, f"Serial communication on {port} failed: {e}"


# ── Local Network Scanner ─────────────────────────────────────────────────────

def _probe_port(ip: str, port: int, timeout: float = 0.6) -> bool:
    """Test if a single TCP port is open."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, port)) == 0
    except Exception:
        return False


def _identify_web_device(ip: str, port: int) -> dict[str, str]:
    """Fetch HTTP server headers / title to classify ESP / Raspberry Pi."""
    url = f"http://{ip}:{port}/"
    info = {"ip": ip, "port": port, "type": "Generic HTTP", "banner": ""}
    try:
        r = requests.get(url, timeout=1.2)
        server = r.headers.get("Server", "")
        body = r.text[:500].lower()
        title_m = re.search(r"<title>([^<]+)</title>", r.text, re.I)
        title = title_m.group(1).strip() if title_m else ""

        if "esp32" in server.lower() or "esp32" in body or "esp32" in title.lower():
            dev_type = "ESP32 Device"
        elif "esp8266" in server.lower() or "esp8266" in body or "esp8266" in title.lower():
            dev_type = "ESP8266 Device"
        elif "tasmota" in body or "tasmota" in title.lower():
            dev_type = "Tasmota Smart Device"
        elif "esphome" in body or "esphome" in title.lower() or "esphome" in server.lower():
            dev_type = "ESPHome Device"
        elif "wled" in body or "wled" in title.lower():
            dev_type = "WLED Light Controller"
        elif "raspberry" in body or "werkzeug" in server.lower() or "uvicorn" in server.lower():
            dev_type = "Raspberry Pi / Linux Server"
        else:
            dev_type = f"Web Server ({server or 'Port ' + str(port)})"

        info["type"] = dev_type
        info["banner"] = title or server or "Responsive"
    except Exception:
        pass
    return info


def _scan_local_network(max_hosts: int = 50) -> list[dict[str, Any]]:
    """Quickly probe local subnet for active ESP32, ESP8266, and Raspberry Pi devices."""
    discovered = []
    try:
        # Determine current local subnet
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "192.168.1.1"

    subnet_prefix = ".".join(local_ip.split(".")[:3])
    targets = [f"{subnet_prefix}.{i}" for i in range(1, min(max_hosts + 1, 255))]

    # Scan common IoT ports: 80 (HTTP), 8080 (REST), 5000 (RasPi Flask), 1883 (MQTT)
    open_targets: list[tuple[str, int]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        future_to_probe = {
            executor.submit(_probe_port, ip, port, 0.4): (ip, port)
            for ip in targets
            for port in (80, 8080, 5000)
        }
        for future in concurrent.futures.as_completed(future_to_probe):
            ip, port = future_to_probe[future]
            try:
                if future.result():
                    open_targets.append((ip, port))
            except Exception:
                pass

    # Classify found web targets
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(lambda p: _identify_web_device(p[0], p[1]), open_targets)
        for res in results:
            discovered.append(res)

    return discovered


# ── Action Execution Functions ────────────────────────────────────────────────

def _do_turn_on(device_name: str, entry: dict, endpoint_arg: str, player=None) -> str:
    """Execute power ON command for HTTP, MQTT, or Socket device."""
    protocol = entry.get("protocol", "http").lower()
    host = entry.get("host", device_name)
    port = int(entry.get("port", 80))

    if protocol == "mqtt":
        topic = endpoint_arg or entry.get("command_topic") or f"home/{device_name}/set"
        payload = entry.get("payload_on", "ON")
        cfg = _load_config()
        broker = cfg.get("mqtt_broker", {}).get("host", "127.0.0.1")
        b_port = int(cfg.get("mqtt_broker", {}).get("port", 1883))
        ok, msg = _mqtt_publish(broker, b_port, topic, payload)
        if ok:
            _log(f"Turned ON '{device_name}' via MQTT topic {topic}", player)
            return outcome.done("turn_on", device=device_name, state="ON", protocol="mqtt", topic=topic)
        return outcome.failed("turn_on", msg, device=device_name)

    elif protocol == "socket":
        cmd = endpoint_arg or entry.get("on_command", "ON")
        ok, resp = _socket_command(host, port, cmd)
        if ok:
            _log(f"Turned ON '{device_name}' via socket: {resp}", player)
            return outcome.done("turn_on", device=device_name, state="ON", response=resp)
        return outcome.failed("turn_on", resp, device=device_name)

    else:
        # Default: HTTP REST
        endpoints = [endpoint_arg] if endpoint_arg else [
            entry.get("on_endpoint"),
            "/relay/on",
            "/on",
            "/led/on",
            "/power/on",
            "/cm?cmnd=Power%20ON",     # Tasmota
            "/win&T=1",                # WLED
        ]
        endpoints = [ep for ep in endpoints if ep]

        last_err = ""
        for ep in endpoints:
            url = f"http://{host}:{port}{ep if ep.startswith('/') or ep.startswith('?') else '/' + ep}"
            # Try GET first, then POST if 405
            ok, code, text = _http_request(url, method="GET")
            if not ok and code == 405:
                ok, code, text = _http_request(url, method="POST")
            if ok:
                _log(f"Turned ON '{device_name}' at {url} (HTTP {code})", player)
                return outcome.done("turn_on", device=device_name, state="ON", endpoint=ep, response=text[:100])
            last_err = text

        return outcome.failed("turn_on", f"Failed to turn on {device_name}: {last_err}", device=device_name)


def _do_turn_off(device_name: str, entry: dict, endpoint_arg: str, player=None) -> str:
    """Execute power OFF command for HTTP, MQTT, or Socket device."""
    protocol = entry.get("protocol", "http").lower()
    host = entry.get("host", device_name)
    port = int(entry.get("port", 80))

    if protocol == "mqtt":
        topic = endpoint_arg or entry.get("command_topic") or f"home/{device_name}/set"
        payload = entry.get("payload_off", "OFF")
        cfg = _load_config()
        broker = cfg.get("mqtt_broker", {}).get("host", "127.0.0.1")
        b_port = int(cfg.get("mqtt_broker", {}).get("port", 1883))
        ok, msg = _mqtt_publish(broker, b_port, topic, payload)
        if ok:
            _log(f"Turned OFF '{device_name}' via MQTT topic {topic}", player)
            return outcome.done("turn_off", device=device_name, state="OFF", protocol="mqtt", topic=topic)
        return outcome.failed("turn_off", msg, device=device_name)

    elif protocol == "socket":
        cmd = endpoint_arg or entry.get("off_command", "OFF")
        ok, resp = _socket_command(host, port, cmd)
        if ok:
            _log(f"Turned OFF '{device_name}' via socket: {resp}", player)
            return outcome.done("turn_off", device=device_name, state="OFF", response=resp)
        return outcome.failed("turn_off", resp, device=device_name)

    else:
        # Default: HTTP REST
        endpoints = [endpoint_arg] if endpoint_arg else [
            entry.get("off_endpoint"),
            "/relay/off",
            "/off",
            "/led/off",
            "/power/off",
            "/cm?cmnd=Power%20OFF",    # Tasmota
            "/win&T=0",                # WLED
        ]
        endpoints = [ep for ep in endpoints if ep]

        last_err = ""
        for ep in endpoints:
            url = f"http://{host}:{port}{ep if ep.startswith('/') or ep.startswith('?') else '/' + ep}"
            ok, code, text = _http_request(url, method="GET")
            if not ok and code == 405:
                ok, code, text = _http_request(url, method="POST")
            if ok:
                _log(f"Turned OFF '{device_name}' at {url} (HTTP {code})", player)
                return outcome.done("turn_off", device=device_name, state="OFF", endpoint=ep, response=text[:100])
            last_err = text

        return outcome.failed("turn_off", f"Failed to turn off {device_name}: {last_err}", device=device_name)


def _do_toggle(device_name: str, entry: dict, endpoint_arg: str, player=None) -> str:
    """Toggle power state of device."""
    protocol = entry.get("protocol", "http").lower()
    host = entry.get("host", device_name)
    port = int(entry.get("port", 80))

    if protocol == "mqtt":
        topic = endpoint_arg or entry.get("command_topic") or f"home/{device_name}/set"
        payload = "TOGGLE"
        cfg = _load_config()
        broker = cfg.get("mqtt_broker", {}).get("host", "127.0.0.1")
        b_port = int(cfg.get("mqtt_broker", {}).get("port", 1883))
        ok, msg = _mqtt_publish(broker, b_port, topic, payload)
        if ok:
            _log(f"Toggled '{device_name}' via MQTT topic {topic}", player)
            return outcome.done("toggle", device=device_name, state="TOGGLED", topic=topic)
        return outcome.failed("toggle", msg, device=device_name)

    endpoints = [endpoint_arg] if endpoint_arg else [
        entry.get("toggle_endpoint"),
        "/relay/toggle",
        "/toggle",
        "/cm?cmnd=Power%20TOGGLE",
        "/win&T=2",
    ]
    endpoints = [ep for ep in endpoints if ep]
    last_err = ""
    for ep in endpoints:
        url = f"http://{host}:{port}{ep if ep.startswith('/') or ep.startswith('?') else '/' + ep}"
        ok, code, text = _http_request(url, method="GET")
        if not ok and code == 405:
            ok, code, text = _http_request(url, method="POST")
        if ok:
            _log(f"Toggled '{device_name}' at {url}", player)
            return outcome.done("toggle", device=device_name, state="TOGGLED", endpoint=ep, response=text[:100])
        last_err = text

    return outcome.failed("toggle", f"Could not toggle {device_name}: {last_err}", device=device_name)


def _do_set_value(device_name: str, entry: dict, val: str, endpoint_arg: str, player=None) -> str:
    """Set numeric or PWM value (e.g. brightness, speed, level 0-255)."""
    if not val:
        return outcome.failed("set_value", "No value provided to set", device=device_name)

    protocol = entry.get("protocol", "http").lower()
    host = entry.get("host", device_name)
    port = int(entry.get("port", 80))

    if protocol == "mqtt":
        topic = endpoint_arg or entry.get("command_topic") or f"home/{device_name}/set"
        cfg = _load_config()
        broker = cfg.get("mqtt_broker", {}).get("host", "127.0.0.1")
        b_port = int(cfg.get("mqtt_broker", {}).get("port", 1883))
        ok, msg = _mqtt_publish(broker, b_port, topic, str(val))
        if ok:
            _log(f"Set '{device_name}' to {val} via MQTT", player)
            return outcome.done("set_value", device=device_name, value=val, topic=topic)
        return outcome.failed("set_value", msg, device=device_name)

    endpoints = [endpoint_arg] if endpoint_arg else [
        f"/set?value={val}",
        f"/pwm?val={val}",
        f"/dimmer?val={val}",
        f"/win&A={val}",             # WLED brightness
        f"/cm?cmnd=Dimmer%20{val}",  # Tasmota dimmer
    ]
    last_err = ""
    for ep in endpoints:
        url = f"http://{host}:{port}{ep if ep.startswith('/') or ep.startswith('?') else '/' + ep}"
        ok, code, text = _http_request(url, method="GET")
        if not ok and code == 405:
            ok, code, text = _http_request(url, method="POST", json_data={"value": val})
        if ok:
            _log(f"Set '{device_name}' to {val} via {url}", player)
            return outcome.done("set_value", device=device_name, value=val, endpoint=ep)
        last_err = text

    return outcome.failed("set_value", f"Could not set value on {device_name}: {last_err}", device=device_name)


def _do_read_sensor(device_name: str, entry: dict, endpoint_arg: str, player=None) -> str:
    """Fetch sensor or telemetry data and return formatted facts."""
    protocol = entry.get("protocol", "http").lower()
    host = entry.get("host", device_name)
    port = int(entry.get("port", 80))

    if protocol == "mqtt":
        topic = endpoint_arg or entry.get("sensor_topic") or entry.get("state_topic") or f"home/{device_name}/state"
        cfg = _load_config()
        broker = cfg.get("mqtt_broker", {}).get("host", "127.0.0.1")
        b_port = int(cfg.get("mqtt_broker", {}).get("port", 1883))
        ok, raw = _mqtt_read_once(broker, b_port, topic)
        if not ok:
            return outcome.failed("read_sensor", raw, device=device_name)
        facts = _format_sensor_reading(raw)
        _log(f"Sensor '{device_name}' reading: {facts.get('summary')}", player)
        _show_panel("Hardware Telemetry", f"Device: {device_name}\nTopic: {topic}\n\n{facts.get('summary')}", player)
        return outcome.done("read_sensor", device=device_name, **facts)

    endpoints = [endpoint_arg] if endpoint_arg else [
        entry.get("read_endpoint"),
        entry.get("status_endpoint"),
        "/sensor",
        "/sensors",
        "/data",
        "/status",
        "/read",
        "/metrics",
        "/cm?cmnd=Status%2010",     # Tasmota sensor status
        "/api/sensors",             # RasPi REST API
    ]
    endpoints = [ep for ep in endpoints if ep]
    last_err = ""
    for ep in endpoints:
        url = f"http://{host}:{port}{ep if ep.startswith('/') or ep.startswith('?') else '/' + ep}"
        ok, code, text = _http_request(url, method="GET")
        if ok and text:
            facts = _format_sensor_reading(text)
            _log(f"Read sensor '{device_name}': {facts.get('summary')}", player)
            _show_panel("Sensor Telemetry", f"Device: {device_name}\nEndpoint: {ep}\n\n{facts.get('summary')}", player)
            return outcome.done("read_sensor", device=device_name, endpoint=ep, **facts)
        last_err = text

    return outcome.failed("read_sensor", f"Could not read sensor data from {device_name}: {last_err}", device=device_name)


def _do_get_status(device_name: str, entry: dict, player=None) -> str:
    """Check connectivity, ping, and responsiveness of a device."""
    protocol = entry.get("protocol", "http").lower()
    host = entry.get("host", device_name)
    port = int(entry.get("port", 80))

    t0 = time.monotonic()
    reachable = _probe_port(host, port, timeout=1.5)
    lat_ms = int((time.monotonic() - t0) * 1000)

    if not reachable:
        return outcome.failed("get_status", f"Device {device_name} ({host}:{port}) is offline or unreachable.", device=device_name)

    status_info = f"Online (latency {lat_ms}ms, port {port} open)"
    _log(f"Device '{device_name}' is {status_info}", player)
    return outcome.done("get_status", device=device_name, host=host, port=port, status="online", latency=f"{lat_ms}ms")


def _do_list_devices(config: dict, player=None) -> str:
    """List all registered devices in the registry with room and duties."""
    devices = config.get("devices", {})
    if not devices:
        return outcome.done("list_devices", count=0, message="No hardware devices are registered yet.")

    lines = []
    summary_parts = []
    for name, d in devices.items():
        proto = d.get("protocol", "http").upper()
        host = d.get("host", "unknown")
        port = d.get("port", 80)
        dev_type = d.get("type", "generic").upper()
        room = d.get("room", "")
        role = d.get("role", "")
        duties = d.get("duties", "")
        desc = d.get("description", "")
        
        info_parts = [f"{dev_type} @ {host}:{port}"]
        if room:
            info_parts.append(f"Room: {room}")
        if role:
            info_parts.append(f"Role: {role}")
        if duties:
            info_parts.append(f"Duties: {duties}")
        elif desc:
            info_parts.append(f"Desc: {desc}")
            
        lines.append(f"• {name} [{', '.join(info_parts)}]")
        summary_parts.append(f"{name} ({room or proto})")

    full_text = "Registered Hardware Devices:\n\n" + "\n".join(lines)
    _show_panel("Hardware Devices", full_text, player)
    _log(f"Found {len(devices)} hardware devices: {', '.join(summary_parts)}", player)
    return outcome.done("list_devices", count=len(devices), devices=", ".join(summary_parts))


def _do_scan_devices(player=None) -> str:
    """Perform local network scan for IoT devices."""
    _log("Scanning local network for ESP and Raspberry Pi devices…", player)
    found = _scan_local_network(max_hosts=40)
    if not found:
        return outcome.done("scan_devices", found_count=0, message="No responsive web or IoT devices found on the local subnet.")

    lines = []
    summary_list = []
    for item in found:
        ip = item["ip"]
        port = item["port"]
        dev_type = item["type"]
        banner = item.get("banner", "")
        banner_str = f" ({banner})" if banner else ""
        lines.append(f"• {ip}:{port} — {dev_type}{banner_str}")
        summary_list.append(f"{ip}:{port} [{dev_type}]")

    text = f"Discovered {len(found)} devices on local network:\n\n" + "\n".join(lines)
    _show_panel("Network Hardware Discovery", text, player)
    _log(f"Network scan discovered {len(found)} devices.", player)
    return outcome.done("scan_devices", found_count=len(found), devices="; ".join(summary_list))


def _do_add_device(name: str, config: dict, host: str, protocol: str = "http",
                   port: int = 80, dev_type: str = "esp32", room: str = "",
                   role: str = "", duties: str = "", details: str = "", player=None) -> str:
    """Register or update a hardware device entry with room and duties."""
    name_clean = name.strip().lower().replace(" ", "_")
    if not name_clean:
        return outcome.failed("add_device", "Device name cannot be empty.")
    if not host:
        return outcome.failed("add_device", "Device host or IP address is required.")

    devices = config.setdefault("devices", {})
    entry = devices.get(name_clean, {
        "name": name_clean,
        "type": dev_type or "esp32",
        "protocol": protocol or "http",
        "host": host,
        "port": port or 80,
    })
    entry["host"] = host
    entry["protocol"] = protocol or entry.get("protocol", "http")
    entry["port"] = port or entry.get("port", 80)
    if dev_type:
        entry["type"] = dev_type
    if room:
        entry["room"] = room
    if role:
        entry["role"] = role
    if duties:
        entry["duties"] = duties

    # Parse details if JSON was provided
    if details:
        try:
            extra = json.loads(details)
            if isinstance(extra, dict):
                entry.update(extra)
        except Exception:
            entry["description"] = details

    devices[name_clean] = entry
    _save_config(config)
    _log(f"Added device '{name_clean}' [{dev_type}] in Room '{room or 'General'}' at {host}:{port} ({protocol}) with duties: {duties or 'None'}", player)
    return outcome.done("add_device", device=name_clean, host=host, room=room, duties=duties, protocol=protocol, port=port)


def _do_remove_device(name: str, config: dict, player=None) -> str:
    """Delete a device from the registry."""
    name_clean = name.strip().lower().replace(" ", "_")
    devices = config.get("devices", {})
    if name_clean in devices:
        del devices[name_clean]
        _save_config(config)
        _log(f"Removed hardware device '{name_clean}'", player)
        return outcome.done("remove_device", device=name_clean)
    return outcome.failed("remove_device", f"Device '{name_clean}' is not in the registry.")


# ── Helpers for Logging & Thread-Safe UI ───────────────────────────────────────

def _log(message: str, player=None) -> None:
    print(f"[Hardware] {message}")
    if player:
        try:
            player.write_log(f"JARVIS [Hardware]: {message}")
        except Exception:
            pass


def _show_panel(title: str, text: str, player=None) -> None:
    if player and hasattr(player, "show_content"):
        try:
            player.show_content(title, text)
        except Exception:
            pass


# ── Main Action Handler ───────────────────────────────────────────────────────

def hardware_control(parameters: dict, player=None, session_memory=None) -> str:
    """Route hardware control actions safely and return structured facts."""
    params = dict(parameters or {})
    action = str(params.get("action") or "").strip().lower()
    device_arg = str(params.get("device") or "").strip()
    value_arg = str(params.get("value") or "").strip()
    proto_arg = str(params.get("protocol") or "").strip().lower()
    endpoint_arg = str(params.get("endpoint") or "").strip()
    details_arg = str(params.get("details") or "").strip()
    room_arg = str(params.get("room") or "").strip()
    role_arg = str(params.get("role") or "").strip()
    duties_arg = str(params.get("duties") or "").strip()
    type_arg = str(params.get("type") or params.get("device_type") or "esp32").strip()

    config = _load_config()

    try:
        # UI page display action
        if action in ("show_page", "open_page", "page", "open_ui", "show_ui", "show_hardware"):
            if player and hasattr(player, "show_hardware"):
                player.show_hardware()
                _log("Opened Hardware & IoT Hub on screen.", player)
                return outcome.done("show_page", message="Hardware Hub opened on screen.")
            # Fall back to showing device list in HUD panel
            return _do_list_devices(config, player)

        # Global registry & discovery actions
        if action in ("list_devices", "list", "show_devices"):
            return _do_list_devices(config, player)

        if action in ("scan_devices", "scan", "discover"):
            return _do_scan_devices(player)

        if action in ("add_device", "register_device", "add"):
            host = params.get("host") or device_arg or endpoint_arg
            # If details contains host/ip
            if not host and details_arg:
                m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", details_arg)
                if m:
                    host = m.group(0)
            port = int(params.get("port") or 80)
            if ":" in str(host) and not str(host).startswith("http"):
                parts = str(host).split(":")
                host = parts[0]
                try:
                    port = int(parts[1])
                except ValueError:
                    port = 80
            name = params.get("name") or device_arg or "new_device"
            return _do_add_device(name, config, host, proto_arg or "http", port, type_arg, room_arg, role_arg, duties_arg, details_arg, player)

        if action in ("remove_device", "delete_device", "remove"):
            return _do_remove_device(device_arg, config, player)

        # Device-specific actions require a target device or IP
        if not device_arg:
            return outcome.failed(action or "hardware_control", "Missing 'device' parameter (specify device name or IP address).")

        device_name, entry = _get_device_entry(device_arg, config)
        if proto_arg:
            entry["protocol"] = proto_arg

        if action in ("turn_on", "on", "power_on", "start"):
            return _do_turn_on(device_name, entry, endpoint_arg, player)

        if action in ("turn_off", "off", "power_off", "stop"):
            return _do_turn_off(device_name, entry, endpoint_arg, player)

        if action in ("toggle", "switch"):
            return _do_toggle(device_name, entry, endpoint_arg, player)

        if action in ("set_value", "set", "dim", "pwm", "speed", "brightness"):
            return _do_set_value(device_name, entry, value_arg, endpoint_arg, player)

        if action in ("read_sensor", "read", "sensor", "telemetry", "get_data", "temperature"):
            return _do_read_sensor(device_name, entry, endpoint_arg, player)

        if action in ("get_status", "status", "ping", "check"):
            return _do_get_status(device_name, entry, player)

        return outcome.failed("hardware_control", f"Unrecognized hardware action '{action}'. Choose from turn_on, turn_off, toggle, set_value, read_sensor, get_status, scan_devices, list_devices, add_device.")

    except Exception as e:
        _log(f"Error handling action '{action}': {e}", player)
        return outcome.failed(action or "hardware_control", f"Hardware operation failed: {e}", device=device_arg)


# ── Tool Declaration (auto-discovered by core/action_loader.py) ──────────────

TOOL = {
    "name": "hardware_control",
    "description": (
        "Controls, monitors, and reads telemetry from hardware devices, microcontrollers, "
        "and home automation systems like ESP32, ESP8266, Raspberry Pi, Arduino, relays, "
        "smart switches, LEDs, and sensors. "
        "Use this tool whenever the user asks to: "
        "- turn ON, turn OFF, or toggle a physical device, relay, light, fan, socket, or pin; "
        "- set PWM, brightness, speed, or level (0-100 or 0-255); "
        "- read sensor data (temperature, humidity, motion, distance, telemetry); "
        "- check device status or ping a hardware host; "
        "- scan the local Wi-Fi / LAN network for active ESP32 or Raspberry Pi devices; "
        "- list, add, or remove named hardware devices."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "The hardware action to perform: "
                    "'turn_on' (power on device/relay/LED), "
                    "'turn_off' (power off device/relay/LED), "
                    "'toggle' (toggle power state), "
                    "'set_value' (set brightness/speed/level), "
                    "'read_sensor' (read temperature, humidity, telemetry), "
                    "'get_status' (check reachability and latency), "
                    "'scan_devices' (scan local LAN for ESP/Raspberry Pi boards), "
                    "'list_devices' (list all registered hardware devices), "
                    "'add_device' (register a new hardware device), "
                    "'remove_device' (remove a registered device)."
                ),
            },
            "device": {
                "type": "STRING",
                "description": (
                    "The friendly name of the saved device (e.g. 'desk_light', 'room_temp', 'bedroom_fan', 'raspi_node'), "
                    "or an IP address/hostname (e.g. '192.168.1.50', 'esp32.local', 'raspberrypi.local')."
                ),
            },
            "value": {
                "type": "STRING",
                "description": (
                    "Value for 'set_value' (e.g. brightness '128', speed '75', level '50', or MQTT custom payload)."
                ),
            },
            "protocol": {
                "type": "STRING",
                "description": (
                    "Optional communication protocol override: 'http' (REST web server, default), "
                    "'mqtt' (MQTT broker), 'socket' (raw TCP socket), or 'serial' (USB COM port)."
                ),
            },
            "endpoint": {
                "type": "STRING",
                "description": (
                    "Optional custom HTTP endpoint path (e.g. '/relay/1/on', '/cm?cmnd=Power%20ON', '/api/sensor') "
                    "or MQTT topic (e.g. 'home/living_room/light/set')."
                ),
            },
            "details": {
                "type": "STRING",
                "description": (
                    "Optional configuration details or description when registering a device via 'add_device'."
                ),
            },
        },
        "required": ["action"],
    },
    "handler": hardware_control,
}
