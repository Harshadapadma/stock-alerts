"""
Configuration — all sensitive values come from environment variables.

Local use:
    Create a .env file (never commit it) or export variables in terminal:
        export EMAIL_FROM="yourmail@gmail.com"
        export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"
        export EMAIL_TO="a@example.com,b@example.com"

GitHub Actions:
    Set these in repo → Settings → Secrets and variables → Actions:
        EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO
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

    # Email  (Gmail SMTP)

    EMAIL_ENABLED  = True
    EMAIL_FROM     = _require("EMAIL_FROM")
    EMAIL_PASSWORD = _require("EMAIL_PASSWORD")

    _to_env = _require("EMAIL_TO")
    EMAIL_TO = [e.strip() for e in _to_env.split(",") if e.strip()]


    # Telegram  (optional)

    TELEGRAM_ENABLED   = False
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")


    # Filter behaviour

    SKIP_PROCEDURAL = True