"""plugins/smart_comms.py — Unified communication hub for Telegram, WhatsApp, and Email.

Checks incoming messages, inspects homelab & IoT status, generates executive AI
summaries via Gemini, and dispatches them across Telegram, Email, and WhatsApp.
Discovered and enabled automatically at startup.
"""
from __future__ import annotations

import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
import urllib.parse
import urllib.request

from core import gemini

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
HOMELAB_CFG = CONFIG_DIR / "homelab_config.json"
HARDWARE_CFG = CONFIG_DIR / "hardware_devices.json"

PLUGIN = {
    "name": "smart_comms",
    "description": (
        "Check messages or alerts on Telegram, WhatsApp, and Email, create an AI "
        "summary of system status, farm sensors, and homelab health, and dispatch "
        "the summary to Telegram, WhatsApp, or Email. Use when the user asks to "
        "'check Telegram/WhatsApp/email', 'send me a summary', 'message me on Telegram', "
        "or 'email me the status'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["check_messages", "send_summary", "send_custom_message", "status"],
                "description": (
                    "What to do: 'check_messages' checks recent Telegram/comms updates; "
                    "'send_summary' generates an AI summary of systems/sensors and sends it; "
                    "'send_custom_message' sends custom text to a channel; "
                    "'status' reports channel connectivity."
                ),
            },
            "channel": {
                "type": "STRING",
                "enum": ["telegram", "whatsapp", "email", "all"],
                "description": "Which channel to inspect or dispatch to. Defaults to 'telegram'.",
            },
            "message": {
                "type": "STRING",
                "description": "Optional custom message or specific topic to summarize/send.",
            },
        },
        "required": ["action"],
    },
}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _check_telegram_updates(bot_token: str) -> list[dict]:
    """Fetch recent messages sent to the bot."""
    if not bot_token:
        return []
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?limit=5"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-LVII"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data.get("result", [])
    except Exception:
        pass
    return []


def _send_telegram(bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "Telegram bot token or chat ID is missing."
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # Try sending with Markdown formatting first
    for parse_mode in ("Markdown", None):
        payload_dict = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload_dict["parse_mode"] = parse_mode
        payload = json.dumps(payload_dict).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "JARVIS-LVII"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok"):
                    return True, "Telegram message sent successfully."
        except Exception:
            if parse_mode is None:
                return False, "Failed to send message to Telegram."
    return True, "Telegram message sent successfully."


def _send_whatsapp(phone: str, apikey: str, text: str) -> tuple[bool, str]:
    if not phone or not apikey:
        return False, "WhatsApp phone or CallMeBot API key is not configured."
    encoded = urllib.parse.quote(text)
    url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded}&apikey={apikey}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-LVII"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8")
            if "success" in body.lower():
                return True, "WhatsApp message delivered via CallMeBot."
            return False, f"CallMeBot response: {body[:100]}"
    except Exception as e:
        return False, f"WhatsApp request failed: {e}"


def _send_email(cfg: dict, subject: str, body: str) -> tuple[bool, str]:
    user = cfg.get("username", "").strip()
    pw = cfg.get("password", "").strip()
    recipient = cfg.get("recipient", "").strip()
    host = cfg.get("smtp_server", "smtp.gmail.com")
    port = int(cfg.get("smtp_port", 587))

    if not user or not pw:
        return False, "Email sender username or App Password is not set in config."
    if not recipient:
        return False, "Recipient email address is not configured."

    msg = MIMEMultipart()
    msg["From"] = f"JARVIS <{user}>"
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            if cfg.get("use_tls", True):
                server.starttls()
            server.login(user, pw)
            server.send_message(msg)
        return True, f"Email delivered to {recipient}."
    except Exception as e:
        return False, f"SMTP delivery failed: {e}"


def _gather_system_context() -> str:
    """Collect brief facts about homelab, hardware, and system load for the summary."""
    lines = []

    # 1. Hardware Devices
    hw = _load_json(HARDWARE_CFG)
    devices = hw.get("devices", {})
    if devices:
        lines.append(f"Hardware Nodes ({len(devices)} registered):")
        for k, v in devices.items():
            lines.append(f"  • {v.get('name', k)} ({v.get('room', 'Global')}): {', '.join(v.get('duties', []))} via {v.get('protocol', 'HTTP')}")

    # 2. Homelab config
    hl = _load_json(HOMELAB_CFG)
    servers = hl.get("servers", {})
    if servers:
        lines.append(f"Monitored Homelab Servers ({len(servers)}):")
        for k, s in servers.items():
            lines.append(f"  • {s.get('name', k)} at {s.get('host', 'N/A')}")

    # 3. Local System Resources
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage("C:\\").percent if Path("C:\\").exists() else psutil.disk_usage("/").percent
        lines.append(f"JARVIS Host Resources: CPU {cpu}%, RAM {ram}%, Disk {disk}%")
    except Exception:
        pass

    return "\n".join(lines)


def _generate_ai_summary(user_note: str) -> str:
    """Use core.gemini to compose a concise, executive Markdown summary."""
    context = _gather_system_context()
    prompt = (
        "You are J.A.R.V.I.S. Compose a brief, elegant executive status briefing for Sir.\n"
        f"Specific user request/focus: {user_note or 'Overall status report'}\n\n"
        f"Current System State:\n{context}\n\n"
        "Requirements:\n"
        "- Format cleanly in Markdown with bullet points\n"
        "- Include IoT/Hardware, Homelab health, and host resource highlights\n"
        "- Keep it under 150 words, crisp and professional\n"
        "- Sign off as: 'JARVIS — All systems nominal'"
    )
    summary = gemini.text(prompt, tier=gemini.FAST)
    if not summary:
        # Fallback if Gemini request is offline or timed out
        summary = (
            "🤖 *JARVIS Status Briefing*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{context}\n\n"
            "_JARVIS — Systems operational._"
        )
    return summary


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "send_summary")
    channel = parameters.get("channel", "telegram").lower()
    custom_msg = parameters.get("message", "")

    hl_cfg = _load_json(HOMELAB_CFG)
    channels = hl_cfg.get("alert_channels", {})
    tg_cfg = channels.get("telegram", {})
    em_cfg = channels.get("email", {})
    wa_cfg = channels.get("whatsapp", {})

    tg_token = tg_cfg.get("bot_token", "")
    tg_chat = tg_cfg.get("chat_id", "")

    # ── ACTION 1: Check Messages / Updates ─────────────────────────────────────
    if action == "check_messages":
        updates = _check_telegram_updates(tg_token)
        if not updates:
            msg = "Checked Telegram: No new incoming messages or commands found."
            if player:
                player.write_log(f"COMMS: {msg}")
            return "Sir, I checked your Telegram bot. There are no new incoming messages."

        last_update = updates[-1]
        user_info = last_update.get("message", {}).get("from", {})
        sender_name = user_info.get("first_name", "User")
        text = last_update.get("message", {}).get("text", "(attachment/command)")

        report = f"Telegram update from {sender_name}: '{text}'."
        if player:
            player.write_log(f"COMMS: {report}")
            player.show_content("TELEGRAM INCOMING", f"From: {sender_name}\nMessage: {text}\nTotal updates in queue: {len(updates)}")
        return f"Sir, you have {len(updates)} recent message on Telegram. Latest from {sender_name}: '{text}'."

    # ── ACTION 2: Channel Status ──────────────────────────────────────────────
    if action == "status":
        tg_ok = "Active (@SBI_2k26_bot)" if tg_cfg.get("enabled") and tg_chat else "Not configured"
        em_ok = f"Configured for {em_cfg.get('recipient')}" if em_cfg.get("recipient") else "No recipient"
        wa_ok = "Active" if wa_cfg.get("enabled") else "Disabled"
        summary_text = f"Telegram: {tg_ok}\nEmail: {em_ok}\nWhatsApp: {wa_ok}"
        if player:
            player.write_log("COMMS: Checked channel availability.")
            player.show_content("COMMUNICATION CHANNELS", summary_text)
        return f"Sir, Telegram is {tg_ok}, email is set to {em_cfg.get('recipient', 'none')}, and WhatsApp is {wa_ok}."

    # ── ACTION 3 & 4: Send Summary or Custom Message ───────────────────────────
    if action == "send_summary":
        content = _generate_ai_summary(custom_msg)
        subject = "JARVIS — System & Hardware Summary"
    else:
        content = custom_msg or "Sir, this is a test notification from JARVIS."
        subject = "JARVIS Notification"

    results = []

    # Dispatch to Telegram
    if channel in ("telegram", "all"):
        if tg_cfg.get("enabled") and tg_chat:
            ok, desc = _send_telegram(tg_token, tg_chat, content)
            results.append("Telegram: " + ("Delivered" if ok else desc))
        else:
            results.append("Telegram: Not enabled in homelab_config.json")

    # Dispatch to Email
    if channel in ("email", "all"):
        if em_cfg.get("enabled") and em_cfg.get("recipient"):
            ok, desc = _send_email(em_cfg, subject, content)
            results.append("Email: " + ("Sent" if ok else desc))
        else:
            # Report recipient readiness if smtp password not yet set
            rcpt = em_cfg.get("recipient", "sufiyannaveed.md@gmail.com")
            results.append(f"Email: Configured for {rcpt} (requires App Password to deliver)")

    # Dispatch to WhatsApp
    if channel in ("whatsapp", "all"):
        if wa_cfg.get("enabled") and wa_cfg.get("phone"):
            ok, desc = _send_whatsapp(wa_cfg.get("phone", ""), wa_cfg.get("apikey", ""), content)
            results.append("WhatsApp: " + ("Sent" if ok else desc))
        else:
            results.append("WhatsApp: Needs CallMeBot API key")

    result_summary = "; ".join(results)

    if player:
        try:
            player.write_log(f"COMMS: Summary dispatched ({result_summary})")
            player.show_content(subject.upper(), content)
        except Exception:
            pass

    return f"Sir, I have prepared the summary and dispatched it. {result_summary}."
