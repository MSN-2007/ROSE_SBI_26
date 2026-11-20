"""
actions/homelab_monitor.py — Homelab, Server, Docker & Service Monitoring with Multi-Channel Alerting.

Monitors homelab infrastructure:
  * Docker Containers: Detects crashed, exited, restarting, or unhealthy containers and scans logs for fatal errors.
  * Server Saturation: Monitors CPU, RAM, Disk, and load thresholds when servers are busy.
  * Service Healthchecks: Probes HTTP/REST endpoints (Plex, Home Assistant, Pi-hole, Farm APIs).
  * Multi-Channel Alert Dispatcher:
      - Telegram: Instant notifications via Telegram Bot API.
      - Email: SMTP alert delivery (Gmail, Outlook, custom mail servers).
      - WhatsApp: Alerts via CallMeBot or Webhook.
      - JARVIS HUD & Voice: Live activity log and on-screen telemetry banner.
"""
from __future__ import annotations

import concurrent.futures
import email.mime.text
import email.mime.multipart
import json
import os
import re
import smtplib
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse

import psutil
import requests

from core import outcome

_BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _BASE_DIR / "config" / "homelab_config.json"
_CONFIG_LOCK = threading.RLock()


# ── Configuration Management ──────────────────────────────────────────────────

def _load_config() -> dict:
    """Load homelab configuration safely."""
    with _CONFIG_LOCK:
        if not _CONFIG_FILE.exists():
            return {
                "servers": {},
                "alert_channels": {
                    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
                    "email": {"enabled": False, "smtp_server": "smtp.gmail.com", "smtp_port": 587, "use_tls": True, "username": "", "password": "", "recipient": ""},
                    "whatsapp": {"enabled": False, "phone": "", "apikey": ""},
                },
                "thresholds": {"cpu_percent": 90, "ram_percent": 90, "disk_percent": 90},
            }
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[Homelab] Warning: Could not read {_CONFIG_FILE.name}: {e}")
            return {"servers": {}, "alert_channels": {}, "thresholds": {}}


def _save_config(data: dict) -> None:
    """Save homelab configuration atomically."""
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


# ── Alert Senders: Telegram, Email, WhatsApp ──────────────────────────────────

def _send_telegram(bot_token: str, chat_id: str, title: str, message: str) -> tuple[bool, str]:
    """Send an instant alert via Telegram Bot API."""
    if not bot_token or not chat_id:
        return False, "Telegram alert failed: bot_token and chat_id must be configured."
    try:
        text = f"🚨 *{title}*\n\n{message}\n\n_— JARVIS Homelab Sentinel_"
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=6.0)
        if resp.status_code == 200:
            return True, "Telegram alert sent successfully."
        return False, f"Telegram API error {resp.status_code}: {resp.text[:120]}"
    except Exception as e:
        return False, f"Telegram connection error: {e}"


def _send_email(smtp_cfg: dict, title: str, message: str) -> tuple[bool, str]:
    """Send an alert via SMTP Email."""
    server_host = smtp_cfg.get("smtp_server") or "smtp.gmail.com"
    port = int(smtp_cfg.get("smtp_port") or 587)
    user = smtp_cfg.get("username", "")
    pwd = smtp_cfg.get("password", "")
    recipient = smtp_cfg.get("recipient") or user
    use_tls = smtp_cfg.get("use_tls", True)

    if not user or not pwd or not recipient:
        return False, "Email alert failed: username, password, and recipient are required."

    try:
        msg = email.mime.multipart.MIMEMultipart()
        msg["From"] = f"JARVIS Homelab Sentinel <{user}>"
        msg["To"] = recipient
        msg["Subject"] = f"🚨 JARVIS Alert: {title}"

        body = (
            f"JARVIS HOMELAB ALERT REPORT\n"
            f"===========================\n"
            f"Event: {title}\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Details:\n{message}\n\n"
            f"Sent automatically by JARVIS Homelab Monitor."
        )
        msg.attach(email.mime.text.MIMEText(body, "plain"))

        server = smtplib.SMTP(server_host, port, timeout=8.0)
        if use_tls:
            server.starttls()
        server.login(user, pwd)
        server.sendmail(user, [recipient], msg.as_string())
        server.quit()
        return True, f"Email sent to {recipient}"
    except Exception as e:
        return False, f"SMTP email failed: {e}"


def _send_whatsapp(phone: str, apikey: str, title: str, message: str) -> tuple[bool, str]:
    """Send a WhatsApp message via CallMeBot gateway."""
    if not phone or not apikey:
        return False, "WhatsApp alert failed: phone number and CallMeBot API key are required."
    try:
        clean_phone = re.sub(r"[^\d+]", "", phone)
        text = f"🚨 *{title}*\n{message}\n_— JARVIS Sentinel_"
        url = f"https://api.callmebot.com/whatsapp.php?phone={quote_plus(clean_phone)}&text={quote_plus(text)}&apikey={apikey}"
        resp = requests.get(url, timeout=7.0)
        if resp.status_code == 200 and "error" not in resp.text.lower():
            return True, "WhatsApp alert dispatched successfully."
        return False, f"WhatsApp gateway response: {resp.text[:120]}"
    except Exception as e:
        return False, f"WhatsApp connection error: {e}"


def dispatch_alert(title: str, message: str, channels: list[str] | None = None, player=None) -> dict[str, str]:
    """Dispatch an alert to all configured channels (Telegram, Email, WhatsApp, HUD)."""
    cfg = _load_config()
    alert_cfg = cfg.get("alert_channels", {})
    results = {}

    target_channels = [c.lower() for c in (channels or ["telegram", "email", "whatsapp", "hud"])]

    # 1. Telegram
    if "telegram" in target_channels:
        tg = alert_cfg.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            ok, msg = _send_telegram(tg["bot_token"], tg["chat_id"], title, message)
            results["telegram"] = "sent" if ok else f"failed: {msg}"

    # 2. Email
    if "email" in target_channels:
        em = alert_cfg.get("email", {})
        if em.get("enabled") and em.get("username") and em.get("password"):
            ok, msg = _send_email(em, title, message)
            results["email"] = "sent" if ok else f"failed: {msg}"

    # 3. WhatsApp
    if "whatsapp" in target_channels:
        wa = alert_cfg.get("whatsapp", {})
        if wa.get("enabled") and wa.get("phone") and wa.get("apikey"):
            ok, msg = _send_whatsapp(wa["phone"], wa["apikey"], title, message)
            results["whatsapp"] = "sent" if ok else f"failed: {msg}"

    # 4. HUD / Player
    _log(f"ALERT: {title} — {message}", player)
    _show_panel(f"🚨 {title}", f"{message}\n\nDispatched: {json.dumps(results)}", player)

    return results


# ── Docker Inspection ─────────────────────────────────────────────────────────

def check_docker_containers(docker_url: str = "") -> list[dict[str, Any]]:
    """Query Docker engine via HTTP API or local socket to detect crashed or unhealthy containers."""
    issues = []
    
    # Try HTTP Docker API (e.g. http://192.168.1.100:2375 or http://localhost:2375)
    endpoints = [docker_url] if docker_url else [
        "http://127.0.0.1:2375",
        "http://localhost:2375",
    ]
    endpoints = [ep for ep in endpoints if ep]

    for ep in endpoints:
        api_url = ep.rstrip("/") + "/containers/json?all=true"
        try:
            r = requests.get(api_url, timeout=3.5)
            if r.status_code == 200:
                containers = r.json()
                for c in containers:
                    name = ", ".join(c.get("Names", ["unknown"])).lstrip("/")
                    state = c.get("State", "").lower()
                    status = c.get("Status", "")
                    
                    # Problematic states: exited with non-zero, dead, restarting
                    if state in ("dead", "restarting") or ("exited" in state or "exited" in status.lower() and not "exited (0)" in status.lower()):
                        # Try to fetch last lines of error logs
                        cid = c.get("Id", "")
                        log_tail = ""
                        try:
                            l_res = requests.get(f"{ep.rstrip('/')}/containers/{cid}/logs?stderr=true&tail=15", timeout=2.0)
                            log_tail = l_res.text.strip()[:200]
                        except Exception:
                            pass

                        issues.append({
                            "container": name,
                            "state": state,
                            "status": status,
                            "docker_url": ep,
                            "log_tail": log_tail,
                            "issue": f"Container '{name}' is {state} ({status})",
                        })
                return issues
        except Exception:
            continue

    return issues


# ── Server Health & Busy Check ────────────────────────────────────────────────

def check_server_saturation(thresholds: dict | None = None) -> dict[str, Any]:
    """Check CPU, RAM, Disk, and load to determine if the server is busy/saturated."""
    th = thresholds or _load_config().get("thresholds", {})
    cpu_limit = float(th.get("cpu_percent", 90))
    ram_limit = float(th.get("ram_percent", 90))
    disk_limit = float(th.get("disk_percent", 90))

    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/" if os.name != "nt" else "C:\\").percent

    busy_reasons = []
    if cpu >= cpu_limit:
        busy_reasons.append(f"CPU at {cpu:.1f}% (threshold {cpu_limit}%)")
    if ram >= ram_limit:
        busy_reasons.append(f"RAM at {ram:.1f}% (threshold {ram_limit}%)")
    if disk >= disk_limit:
        busy_reasons.append(f"Disk at {disk:.1f}% (threshold {disk_limit}%)")

    return {
        "busy": len(busy_reasons) > 0,
        "cpu_percent": cpu,
        "ram_percent": ram,
        "disk_percent": disk,
        "reasons": busy_reasons,
        "summary": f"CPU: {cpu:.0f}%, RAM: {ram:.0f}%, Disk: {disk:.0f}%" + (f" ⚠️ High load: {', '.join(busy_reasons)}" if busy_reasons else " ✓ Normal"),
    }


# ── Service Healthcheck ───────────────────────────────────────────────────────

def check_services(services: list[dict]) -> list[dict[str, Any]]:
    """Probe configured homelab web services or APIs."""
    failed = []
    for s in services:
        name = s.get("name", "Service")
        url = s.get("url", "")
        if not url:
            continue
        try:
            r = requests.get(url, timeout=3.0)
            if r.status_code >= 500:
                failed.append({"service": name, "url": url, "error": f"HTTP {r.status_code}"})
        except requests.exceptions.Timeout:
            failed.append({"service": name, "url": url, "error": "Connection timed out"})
        except requests.exceptions.ConnectionError:
            failed.append({"service": name, "url": url, "error": "Connection refused / Service down"})
        except Exception as e:
            failed.append({"service": name, "url": url, "error": str(e)})
    return failed


# ── Full Homelab Scan & Alert Automation ──────────────────────────────────────

def run_homelab_healthcheck(auto_alert: bool = True, player=None) -> dict[str, Any]:
    """Execute complete homelab healthcheck: Server load, Docker containers, and Services."""
    cfg = _load_config()
    servers = cfg.get("servers", {})
    all_issues = []

    # 1. Server Saturation Check
    host_stats = check_server_saturation(cfg.get("thresholds"))
    if host_stats["busy"]:
        all_issues.append(f"Server is busy: {', '.join(host_stats['reasons'])}")

    # 2. Docker & Service checks across configured servers
    docker_crashes = []
    service_failures = []

    for s_id, s in servers.items():
        s_name = s.get("name", s_id)
        d_url = s.get("docker_url", "")
        if d_url:
            crashes = check_docker_containers(d_url)
            for c in crashes:
                c["server"] = s_name
                docker_crashes.append(c)
                all_issues.append(f"Docker crash on {s_name}: {c['issue']}")

        services = s.get("services", [])
        if services:
            fails = check_services(services)
            for f in fails:
                f["server"] = s_name
                service_failures.append(f)
                all_issues.append(f"Service down on {s_name}: {f['service']} ({f['error']})")

    has_problems = len(all_issues) > 0
    report_title = "Homelab Anomaly Detected" if has_problems else "Homelab Operational"
    report_body = "\n".join(f"• {issue}" for issue in all_issues) if all_issues else "All systems, Docker containers, and services are healthy."

    alert_results = {}
    if has_problems and auto_alert:
        alert_results = dispatch_alert(report_title, report_body, player=player)

    return {
        "ok": not has_problems,
        "issues_count": len(all_issues),
        "issues": all_issues,
        "host_stats": host_stats,
        "docker_crashes": docker_crashes,
        "service_failures": service_failures,
        "summary": report_body,
        "alert_dispatched": alert_results,
    }


# ── Helpers for Logging & UI ──────────────────────────────────────────────────

def _log(message: str, player=None) -> None:
    print(f"[Homelab] {message}")
    if player:
        try:
            player.write_log(f"JARVIS [Homelab]: {message}")
        except Exception:
            pass


def _show_panel(title: str, text: str, player=None) -> None:
    if player and hasattr(player, "show_content"):
        try:
            player.show_content(title, text)
        except Exception:
            pass


# ── Main Action Handler ───────────────────────────────────────────────────────

def homelab_monitor(parameters: dict, player=None, session_memory=None) -> str:
    """Voice & Tool handler for homelab server, docker, and multi-channel alerting."""
    params = dict(parameters or {})
    action = str(params.get("action") or "check_status").strip().lower()
    target_arg = str(params.get("target") or params.get("server") or "").strip()
    channel_arg = str(params.get("channel") or "").strip().lower()
    msg_arg = str(params.get("message") or "").strip()
    title_arg = str(params.get("title") or "Homelab Notification").strip()

    config = _load_config()

    try:
        # 1. Full status check
        if action in ("check_status", "status", "healthcheck", "check"):
            res = run_homelab_healthcheck(auto_alert=True, player=player)
            if res["ok"]:
                _log("Homelab healthcheck: All systems, Docker and services operational.", player)
                _show_panel("Homelab Health: Normal", res["host_stats"]["summary"], player)
                return outcome.done("check_status", health="healthy", details=res["host_stats"]["summary"])
            else:
                _log(f"Homelab Anomaly: {res['issues_count']} issue(s) detected.", player)
                return outcome.done("check_status", health="alert", issues=res["issues_count"], summary=res["summary"])

        # 2. Docker container check
        if action in ("check_docker", "docker", "containers"):
            docker_url = target_arg or "http://127.0.0.1:2375"
            crashes = check_docker_containers(docker_url)
            if not crashes:
                return outcome.done("check_docker", status="healthy", message="All Docker containers are running normally.")
            c_names = ", ".join(c["container"] for c in crashes)
            alert_msg = f"Crashed or exited containers: {c_names}"
            dispatch_alert("Docker Crash Detected", alert_msg, player=player)
            return outcome.done("check_docker", status="crashed", count=len(crashes), containers=c_names)

        # 3. Server busy check
        if action in ("check_server", "busy", "server_load", "system_load"):
            stats = check_server_saturation()
            state = "busy" if stats["busy"] else "normal"
            if stats["busy"]:
                dispatch_alert("High Server Load", stats["summary"], player=player)
            return outcome.done("check_server", state=state, cpu=f"{stats['cpu_percent']}%", ram=f"{stats['ram_percent']}%", disk=f"{stats['disk_percent']}%", summary=stats["summary"])

        # 4. Manual alert dispatch
        if action in ("send_alert", "alert", "notify"):
            channels = [channel_arg] if channel_arg else ["telegram", "email", "whatsapp"]
            disp = dispatch_alert(title_arg, msg_arg or "Manual Homelab Alert", channels=channels, player=player)
            return outcome.done("send_alert", title=title_arg, channels=", ".join(channels), dispatched=json.dumps(disp))

        # 5. Test alerts
        if action in ("test_alert", "test_telegram", "test_email", "test_whatsapp"):
            ch = "telegram" if "telegram" in action else ("email" if "email" in action else ("whatsapp" if "whatsapp" in action else channel_arg or "telegram"))
            test_msg = f"This is a test notification from JARVIS Homelab Monitor at {time.strftime('%H:%M:%S')}."
            disp = dispatch_alert("Test Alert", test_msg, channels=[ch], player=player)
            return outcome.done("test_alert", channel=ch, result=json.dumps(disp))

        # 6. Configure alert channels
        if action in ("configure_alerts", "set_alert", "config_alerts"):
            alert_cfg = config.setdefault("alert_channels", {})
            updated = []
            if "bot_token" in params or "chat_id" in params:
                tg = alert_cfg.setdefault("telegram", {})
                if "bot_token" in params: tg["bot_token"] = str(params["bot_token"]).strip()
                if "chat_id" in params: tg["chat_id"] = str(params["chat_id"]).strip()
                tg["enabled"] = True
                updated.append("telegram")
            if "email" in params or "smtp_username" in params or "smtp_password" in params:
                em = alert_cfg.setdefault("email", {})
                if "smtp_server" in params: em["smtp_server"] = str(params["smtp_server"]).strip()
                if "smtp_port" in params: em["smtp_port"] = int(params["smtp_port"])
                if "smtp_username" in params: em["username"] = str(params["smtp_username"]).strip()
                if "smtp_password" in params: em["password"] = str(params["smtp_password"]).strip()
                if "recipient" in params: em["recipient"] = str(params["recipient"]).strip()
                em["enabled"] = True
                updated.append("email")
            if "whatsapp_phone" in params or "whatsapp_apikey" in params:
                wa = alert_cfg.setdefault("whatsapp", {})
                if "whatsapp_phone" in params: wa["phone"] = str(params["whatsapp_phone"]).strip()
                if "whatsapp_apikey" in params: wa["apikey"] = str(params["whatsapp_apikey"]).strip()
                wa["enabled"] = True
                updated.append("whatsapp")
            _save_config(config)
            return outcome.done("configure_alerts", updated=", ".join(updated) if updated else "none")

        # 7. List targets
        if action in ("list_targets", "list_servers"):
            servers = config.get("servers", {})
            s_list = [f"{s_id} ({s.get('name', s_id)})" for s_id, s in servers.items()]
            return outcome.done("list_targets", count=len(servers), servers=", ".join(s_list))

        return outcome.failed("homelab_monitor", f"Unknown homelab action '{action}'. Choose from check_status, check_docker, check_server, send_alert, test_alert, configure_alerts, list_targets.")

    except Exception as e:
        _log(f"Error handling homelab action '{action}': {e}", player)
        return outcome.failed(action or "homelab_monitor", f"Homelab monitoring error: {e}")


# ── Tool Declaration (auto-discovered by core/action_loader.py) ──────────────

TOOL = {
    "name": "homelab_monitor",
    "description": (
        "Monitors homelab servers, Docker containers, web services, and host resource saturation. "
        "Sends instant alerts via Telegram, Email, and WhatsApp if a container crashes, a service goes down, "
        "or the server is busy/overloaded (high CPU, RAM, or Disk). "
        "Use this tool to: "
        "- check homelab health ('check_status', 'check_docker', 'check_server'); "
        "- test or send alerts to Telegram, Email, or WhatsApp ('send_alert', 'test_alert'); "
        "- configure alert credentials ('configure_alerts'); "
        "- list monitored servers and services ('list_targets')."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Action to perform: "
                    "'check_status' (full homelab healthcheck across Docker, CPU/RAM, and services), "
                    "'check_docker' (check for crashed or exited Docker containers), "
                    "'check_server' (check if the server is busy with high CPU/RAM/Disk), "
                    "'send_alert' (dispatch an alert message to Telegram/Email/WhatsApp), "
                    "'test_alert' (send a test message to verify Telegram, Email, or WhatsApp), "
                    "'configure_alerts' (update Telegram bot token, SMTP email, or WhatsApp credentials), "
                    "'list_targets' (list monitored servers and docker targets)."
                ),
            },
            "channel": {
                "type": "STRING",
                "description": "Alert channel: 'telegram', 'email', 'whatsapp', or 'all'.",
            },
            "server": {
                "type": "STRING",
                "description": "Specific server name or Docker URL to inspect (optional).",
            },
            "title": {
                "type": "STRING",
                "description": "Alert subject / title (e.g. 'Docker Crash Alert', 'High Memory Warning').",
            },
            "message": {
                "type": "STRING",
                "description": "Detailed alert body or custom notification text.",
            },
            "bot_token": {
                "type": "STRING",
                "description": "Telegram Bot API token (for 'configure_alerts').",
            },
            "chat_id": {
                "type": "STRING",
                "description": "Telegram Chat ID (for 'configure_alerts').",
            },
            "recipient": {
                "type": "STRING",
                "description": "Recipient email address for SMTP alerts (for 'configure_alerts').",
            },
            "whatsapp_phone": {
                "type": "STRING",
                "description": "Phone number with country code for WhatsApp alerts (for 'configure_alerts').",
            },
            "whatsapp_apikey": {
                "type": "STRING",
                "description": "CallMeBot API key for WhatsApp alerts (for 'configure_alerts').",
            },
        },
        "required": ["action"],
    },
    "handler": homelab_monitor,
}
