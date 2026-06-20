import smtplib
from email.message import EmailMessage

import requests

from database import add_alerta, get_config, log_notification


EVENT_CONFIG_KEYS = {
    "unknown_attempt": "notify_unknown_enabled",
    "access_granted": "notify_access_granted_enabled",
    "camera_down": "notify_camera_down_enabled",
    "manual_relay": "notify_manual_relay_enabled",
}


def _enabled(value) -> bool:
    return str(value or "0").strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def _cfg(key: str, default: str = "") -> str:
    return str(get_config(key, default) or "").strip()


def _is_event_enabled(event_type: str) -> bool:
    key = EVENT_CONFIG_KEYS.get(event_type)
    return _enabled(_cfg(key, "1")) if key else True


def _send_telegram(text: str) -> tuple[bool, str]:
    token = _cfg("telegram_bot_token")
    chat_id = _cfg("telegram_chat_id")
    if not token or not chat_id:
        return False, "Faltan telegram_bot_token o telegram_chat_id"

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    return response.ok, response.text[:500]


def _send_whatsapp(text: str) -> tuple[bool, str]:
    token = _cfg("whatsapp_token")
    phone_number_id = _cfg("whatsapp_phone_number_id")
    to = _cfg("whatsapp_to")
    if not token or not phone_number_id or not to:
        return False, "Faltan whatsapp_token, whatsapp_phone_number_id o whatsapp_to"

    response = requests.post(
        f"https://graph.facebook.com/v20.0/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        },
        timeout=10,
    )
    return response.ok, response.text[:500]


def _send_email(subject: str, text: str) -> tuple[bool, str]:
    host = _cfg("smtp_host")
    port = int(_cfg("smtp_port", "587") or "587")
    user = _cfg("smtp_user")
    password = _cfg("smtp_password")
    sender = _cfg("smtp_from") or user
    recipients = [item.strip() for item in _cfg("smtp_to").split(",") if item.strip()]
    if not host or not sender or not recipients:
        return False, "Faltan smtp_host, smtp_from/smtp_user o smtp_to"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)

    with smtplib.SMTP(host, port, timeout=10) as server:
        server.starttls()
        if user:
            server.login(user, password)
        server.send_message(message)
    return True, "Correo enviado"


def _send_mobile_webhook(event_type: str, title: str, text: str, payload: dict) -> tuple[bool, str]:
    url = _cfg("mobile_webhook_url")
    if not url:
        return False, "Falta mobile_webhook_url"

    response = requests.post(
        url,
        json={"event_type": event_type, "title": title, "message": text, "payload": payload},
        timeout=10,
    )
    return response.ok, response.text[:500]


def send_notification(event_type: str, title: str, message: str, payload: dict | None = None):
    payload = payload or {}
    if not _is_event_enabled(event_type):
        return []

    text = f"{title}\n{message}"
    add_alerta(event_type, message)

    channels = [
        ("telegram", _enabled(_cfg("telegram_enabled")), lambda: _send_telegram(text)),
        ("whatsapp", _enabled(_cfg("whatsapp_enabled")), lambda: _send_whatsapp(text)),
        ("email", _enabled(_cfg("email_enabled")), lambda: _send_email(title, text)),
        (
            "mobile",
            _enabled(_cfg("mobile_enabled")),
            lambda: _send_mobile_webhook(event_type, title, text, payload),
        ),
    ]

    results = []
    for channel, enabled, sender in channels:
        if not enabled:
            continue
        try:
            ok, response = sender()
        except Exception as exc:
            ok, response = False, str(exc)
        log_notification(event_type, channel, ok, message, response)
        results.append({"channel": channel, "ok": ok, "response": response})
    return results
