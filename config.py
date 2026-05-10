"""
Configuration — works both locally and on GitHub Actions.

Local use:    edit the default values directly below.
GitHub Actions: set Repository Secrets (Settings → Secrets → Actions):
                EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO (comma-separated)
"""

import os


class Config:

    # ── Email (Gmail SMTP) ─────────────────────────────────────────────────
    EMAIL_ENABLED  = True
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-7a9891420d304a62b5d90e5371417b30")

    EMAIL_FROM     = os.getenv("EMAIL_FROM",     "powerbiwork0@gmail.com")
    EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "lpyg rgpb wgek qvfw")

    _to_env = os.getenv("EMAIL_TO", "")
    EMAIL_TO = (
        [e.strip() for e in _to_env.split(",") if e.strip()]
        if _to_env else [
            "powerbiwork0@gmail.com"
        ]
    )

    # ── Telegram (optional) ───────────────────────────────────────────────
    TELEGRAM_ENABLED   = True
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

    # ── WhatsApp via Meta Cloud API ───────────────────────────────────────
    WHATSAPP_ENABLED = True

    META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "976285592245194")

    META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "EAAhJJssD99IBRPztPIYBHfsOottFSWu4ETXkq2f7pSgGyzQ5W30NOpMR17JIUZB9Mwk7BuQ9kkLYxhhiywuaRdO89HzXXLI9mXA6cSgzGV3fivxnSyDEHIZAqMNAFgaIjZBEtZAFRlE2rBfWcDPESQi8qIgPqRrJ333p8MaFKeciCvOqNhP218xP95MSNyAMZCAZDZD")

    _wa_env = os.getenv("WHATSAPP_TO", "919769792864")
    WHATSAPP_TO = (
        [n.strip() for n in _wa_env.split(",") if n.strip()]
        if _wa_env else []
    )

    # ── Filter behaviour ──────────────────────────────────────────────────
    SKIP_PROCEDURAL = True

    # ── Market Cap filter ─────────────────────────────────────────────────
    # Only send alerts for companies whose Total Market Cap >= this value (₹ Crore).
    # Set to 0 to disable the filter entirely.
    MARKET_CAP_MIN_CR = 1000  # ₹ 1,000 Crore
