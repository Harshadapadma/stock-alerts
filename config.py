"""
Configuration — works both locally and on GitHub Actions.


Local use:
    Create a .env file (never commit it) or export variables in your terminal:
        export EMAIL_FROM="yourmail@gmail.com"
        export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"
        export EMAIL_TO="a@example.com,b@example.com"

GitHub Actions:
    Set these in repo → Settings → Secrets and variables → Actions.
=======
Local use:    edit the default values directly below.
GitHub Actions: set Repository Secrets (Settings → Secrets → Actions):
                EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO (comma-separated)

"""

import os


class Config:
<<<<<<< HEAD

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
=======
    # ──────────────────────────────────────────
    # Email  (Gmail SMTP)
    # ──────────────────────────────────────────
    EMAIL_ENABLED  = True
    DEEPSEEK_API_KEY=os.getenv("DEEPSEEK_API_KEY", "sk-7a9891420d304a62b5d90e5371417b30")

    EMAIL_FROM     = os.getenv("EMAIL_FROM",     "powerbiwork0@gmail.com")
    EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "lpyg rgpb wgek qvfw")

    # EMAIL_TO: comma-separated in env var, or list here for local use
    _to_env = os.getenv("EMAIL_TO", "")
    EMAIL_TO = (
        [e.strip() for e in _to_env.split(",") if e.strip()]
        if _to_env else [
            "powerbiwork0@gmail.com"
          
        ]
    )

>>>>>>> b78a495 (dashboard)

    # ── WhatsApp via Meta Cloud API ───────────────────────────────────────
    # Enable / disable WhatsApp alerts
    WHATSAPP_ENABLED = True
<<<<<<< HEAD

    # From Meta Developer Console → WhatsApp → API Setup
    # Phone Number ID  (numeric string, e.g. "123456789012345")
    META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")

    # Permanent System User token or temporary access token
    META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")

    # Recipient number(s) — country code + number, NO leading +
    # e.g. "919876543210"  (91 = India, then 10-digit mobile)
    # Multiple numbers: "919876543210,919123456789"
    _wa_env    = os.getenv("WHATSAPP_TO", "")
=======
 
    # From Meta Developer Console → WhatsApp → API Setup
    # Phone Number ID  (numeric string, e.g. "123456789012345")
    META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "976285592245194")
 
    # Permanent System User token or temporary access token
    META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "EAAhJJssD99IBRPztPIYBHfsOottFSWu4ETXkq2f7pSgGyzQ5W30NOpMR17JIUZB9Mwk7BuQ9kkLYxhhiywuaRdO89HzXXLI9mXA6cSgzGV3fivxnSyDEHIZAqMNAFgaIjZBEtZAFRlE2rBfWcDPESQi8qIgPqRrJ333p8MaFKeciCvOqNhP218xP95MSNyAMZCAZDZD")
 
    # Recipient number(s) — country code + number, NO leading +
    # e.g. "919876543210"  (91 = India, then 10-digit mobile)
    # Multiple numbers: "919876543210,919123456789"
    _wa_env    = os.getenv("WHATSAPP_TO", "919769792864")
>>>>>>> b78a495 (dashboard)
    WHATSAPP_TO = (
        [n.strip() for n in _wa_env.split(",") if n.strip()]
        if _wa_env else []
    )
<<<<<<< HEAD

    # ── DeepSeek AI (optional — summaries degrade to regex fallback) ──────
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

    # ── Filter behaviour ──────────────────────────────────────────────────
    SKIP_PROCEDURAL = True
=======
 
    # ── Filter behaviour ──────────────────────────────────────────────────
    SKIP_PROCEDURAL = True

    # ── Market Cap filter ─────────────────────────────────────────────────
    # Only send alerts for companies whose Total Market Cap >= this value (₹ Crore).
    # Set to 0 to disable the filter entirely.
    MARKET_CAP_MIN_CR = 1000  # ₹ 1,000 Crore
>>>>>>> b78a495 (dashboard)
