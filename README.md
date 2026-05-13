# stock-alerts

Monitors NSE corporate announcements every 10 minutes and sends alerts for mergers, demergers, amalgamations, and scheme-of-arrangement filings via **Email**, **Telegram**, and **WhatsApp**. Includes a web dashboard to browse and search the full history.

---

## How it works

| Component | What it does |
|---|---|
| `scraper.py` | Fetches NSE announcements, filters for scheme-related filings, sends alerts |
| `dashboard.py` | Flask web app — browse recent alerts, search any NSE company's full history |
| `announcements.json` | Rolling 30-day database of matched announcements (committed to repo by CI) |
| GitHub Actions | Runs `scraper.py` every 10 minutes on a free runner — no server needed |

---

## Prerequisites

- Python 3.11+
- A GitHub account (to fork the repo and set up Actions secrets)
- At least one alert channel configured — Email, Telegram, or WhatsApp (all optional individually, but you need at least one)

---

## 1. Fork and clone

```bash
git clone https://github.com/<your-username>/stock-alerts.git
cd stock-alerts
```

---

## 2. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. Configure credentials

Open `config.py` and fill in your values:

```python
class Config:
    # ── Email ───────────────────────────────────────────────────────────────────
    EMAIL_ENABLED  = True
    EMAIL_FROM     = "your-gmail@gmail.com"
    EMAIL_PASSWORD = "xxxx xxxx xxxx xxxx"   # Gmail App Password (not your login password)
    EMAIL_TO       = ["recipient1@gmail.com", "recipient2@gmail.com"]

    # ── Telegram (optional) ──────────────────────────────────────────────────────
    TELEGRAM_ENABLED   = False               # set True to enable
    TELEGRAM_BOT_TOKEN = ""                  # from @BotFather
    TELEGRAM_CHAT_ID   = ""                  # your chat/group ID

    # ── WhatsApp via Meta Cloud API (optional) ───────────────────────────────────
    WHATSAPP_ENABLED     = False             # set True to enable
    META_PHONE_NUMBER_ID = ""
    META_ACCESS_TOKEN    = ""
    WHATSAPP_TO          = ["91XXXXXXXXXX"]  # phone numbers with country code

    # ── AI summaries via DeepSeek (optional) ─────────────────────────────────────
    DEEPSEEK_API_KEY = ""                    # leave blank to use regex fallback

    # ── Filters ──────────────────────────────────────────────────────────────────
    MARKET_CAP_MIN_CR = 1000                 # only alert on companies ≥ ₹1000 Cr mcap
                                             # set to 0 to disable the filter
```

### Getting a Gmail App Password

1. Go to your Google Account → **Security** → **2-Step Verification** (must be on)
2. Search for **App Passwords** → create one for "Mail"
3. Copy the 16-character password into `EMAIL_PASSWORD`

### Getting a Telegram Chat ID

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token
2. Send any message to your bot, then open:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. The `chat.id` field in the response is your `TELEGRAM_CHAT_ID`

---

## 4. Run locally

**One-off check (test your setup):**
```bash
python scraper.py --once
```

**Continuous mode (checks every 60 minutes):**
```bash
python scraper.py
```

**Backfill the last N days:**
```bash
python scraper.py --backfill 7
```

**Dashboard:**
```bash
python dashboard.py          # opens at http://localhost:8090
python dashboard.py 8080     # custom port
```

---

## 5. Automated alerts via GitHub Actions (recommended)

The repo includes a workflow that runs the scraper every 10 minutes on GitHub's free runners — no server or cron required.

### Step 1 — Add secrets to your forked repo

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `EMAIL_FROM` | your Gmail address |
| `EMAIL_PASSWORD` | Gmail App Password |
| `EMAIL_TO` | comma-separated recipient addresses |
| `TELEGRAM_BOT_TOKEN` | *(optional)* |
| `TELEGRAM_CHAT_ID` | *(optional)* |
| `META_PHONE_NUMBER_ID` | *(optional)* |
| `META_ACCESS_TOKEN` | *(optional)* |
| `WHATSAPP_TO` | *(optional)* comma-separated phone numbers |
| `DEEPSEEK_API_KEY` | *(optional)* |

### Step 2 — Enable Actions

Go to **Actions** tab in your fork → click **"I understand my workflows, go ahead and enable them"**

The `Stock Alert Monitor` workflow will now run automatically every 10 minutes and commit new matches to `announcements.json`.

---

## 6. Deploy the dashboard (optional)

The dashboard is a separate read-only Flask app. Deploy it anywhere that runs Python.

### Render (free tier)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com)

1. Connect your GitHub repo on [render.com](https://render.com)
2. Render auto-detects `render.yaml` — just click **Apply**
3. The dashboard is live at `https://your-app.onrender.com`

### fly.io

```bash
fly launch          # first time — creates the app
fly deploy          # subsequent deploys
```

### Docker

```bash
docker build -t stock-alerts-dashboard .
docker run -p 8080:8080 stock-alerts-dashboard
```

---

## Project structure

```
stock-alerts/
├── scraper.py          # alert engine — fetch, filter, notify
├── dashboard.py        # Flask web dashboard
├── config.py           # credentials and settings
├── requirements.txt
├── announcements.json  # rolling 30-day matched announcements DB
├── seen_ids.json       # dedup cache (auto-managed by scraper)
├── data/               # per-company full history cache (populated by CI)
├── companies.json      # NSE symbol → company name map (for search)
├── Dockerfile
├── render.yaml
├── fly.toml
└── .github/workflows/
    ├── scraper.yml         # runs every 10 min, commits announcements.json
    └── fetch_company.yml   # on-demand company history fetch (triggered by dashboard)
```

---

## What gets alerted

**Included:**
- Mergers, amalgamations, demergers
- Scheme of arrangement / composite scheme filings
- NCLT orders (approval, sanction, effective date)
- Spin-offs, hive-offs, slump sales
- Open offers — only when linked to a merger/scheme

**Excluded:**
- Quarterly results, dividends, buybacks, rights issues
- Director/auditor appointments and resignations
- SEBI Takeover pledge disclosures
- RTA (registrar) service-provider changes
- WOS (subsidiary) incorporations
- Stock splits, share splits
- Companies below the market cap threshold

---

## Troubleshooting

| Problem | Fix |
|---|---|
| No email received | Check spam folder; verify App Password is correct; check `alerts.log` |
| `SMTPAuthenticationError` | Re-generate the Gmail App Password |
| NSE fetch returns empty | NSE blocks non-browser requests intermittently; the scraper retries automatically |
| Dashboard shows no data | `announcements.json` may be empty on a fresh fork — run `python scraper.py --backfill 7` locally and commit |
| GitHub Actions not running | Check Actions tab is enabled; verify secrets are set correctly |
