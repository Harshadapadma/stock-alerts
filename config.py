"""
Configuration — all sensitive values come from environment variables.

Local use:
    Create a .env file (never commit it) or export variables in your terminal:
        export EMAIL_FROM="yourmail@gmail.com"
        export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"
        export EMAIL_TO="a@example.com,b@example.com"

GitHub Actions:
    Set these in repo → Settings → Secrets and variables → Actions.
"""

import os


def _require(name: str) -> str:
    val = os.getenv(name, "")
    if not val:
        raise EnvironmentError(
            f"Missing required environment variable: {name}\n"
            f"Set it in your terminal or GitHub Actions secrets."
        )
    return val


class Config:

    # ── Email (Gmail SMTP) ─────────────────────────────────────────────────
    EMAIL_ENABLED  = True
    EMAIL_FROM     = _require("EMAIL_FROM")
    EMAIL_PASSWORD = _require("EMAIL_PASSWORD")
    _to_env        = _require("EMAIL_TO")
    EMAIL_TO       = [e.strip() for e in _to_env.split(",") if e.strip()]

    # ── Telegram (optional) ───────────────────────────────────────────────
    TELEGRAM_ENABLED   = True
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

    # ── WhatsApp via Meta Cloud API ───────────────────────────────────────
    # Enable / disable WhatsApp alerts
    WHATSAPP_ENABLED = True

    # From Meta Developer Console → WhatsApp → API Setup
    # Phone Number ID  (numeric string, e.g. "123456789012345")
    META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")

    # Permanent System User token or temporary access token
    META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")

    # Recipient number(s) — country code + number, NO leading +
    # e.g. "919876543210"  (91 = India, then 10-digit mobile)
    # Multiple numbers: "919876543210,919123456789"
    _wa_env    = os.getenv("WHATSAPP_TO", "")
    WHATSAPP_TO = (
        [n.strip() for n in _wa_env.split(",") if n.strip()]
        if _wa_env else []
    )

    # ── DeepSeek AI (optional — summaries degrade to regex fallback) ──────
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

    # ── Filter behaviour ──────────────────────────────────────────────────
    SKIP_PROCEDURAL = True
