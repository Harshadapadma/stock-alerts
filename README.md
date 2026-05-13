# Corporate Announcements Monitor

Monitors NSE corporate announcements every 10 minutes and sends alerts for mergers, demergers, amalgamations, and scheme-of-arrangement filings via **Email**, **Telegram**, and **WhatsApp**. Includes a web dashboard with AI-powered summaries to browse and search the full history of any listed company.

---

## What it does

| Component | What it does |
|---|---|
| `scraper.py` | Fetches NSE announcements every 10 min, filters for scheme-related filings, sends alerts |
| `dashboard.py` | Flask web app — browse recent alerts, search any NSE company's full history with AI summaries |
| `announcements.json` | Rolling 30-day database of matched announcements (auto-committed by CI) |
| GitHub Actions | Runs the scraper on a schedule — no server needed |

**Alerts on:** Mergers · Amalgamations · Demergers · Composite Schemes · NCLT orders · Spin-offs · Hive-offs · Slump Sales · Open Offers linked to schemes

**Filters out:** Results · Dividends · Buybacks · Stock splits · Director changes · RTA changes · Pledge disclosures · Companies below market cap threshold

---

## Step 1 — Fork the repository

1. Go to **[github.com/Harshadapadma/stock-alerts](https://github.com/Harshadapadma/stock-alerts)**
2. Click **Fork** (top right) → **Create fork**
3. You now have your own copy at `github.com/<your-username>/stock-alerts`

---

## Step 2 — Set up alert channels

You need **at least one** channel. All are optional individually.

---

### Email (Gmail)

1. Sign in to your Google account → go to **[myaccount.google.com/security](https://myaccount.google.com/security)**
2. Enable **2-Step Verification** if not already on
3. Search for **App Passwords** in the search bar → click it
4. Select app: **Mail** → Select device: **Other** → type any name → click **Generate**
5. Copy the 16-character password (e.g. `abcd efgh ijkl mnop`) — you will not see it again

You will need:
- `EMAIL_FROM` — your Gmail address (e.g. `yourname@gmail.com`)
- `EMAIL_PASSWORD` — the 16-character App Password above
- `EMAIL_TO` — comma-separated list of recipients (can be the same address)

---

### Telegram (optional)

1. Open Telegram → search for **[@BotFather](https://t.me/BotFather)** → send `/newbot`
2. Follow the prompts — choose a name and username for your bot
3. BotFather gives you a **token** like `7123456789:AAFxxxxxxxxxxxxxxxx` — copy it
4. Send any message to your new bot (e.g. "hello")
5. Open this URL in your browser (replace `TOKEN` with your actual token):
   ```
   https://api.telegram.org/botTOKEN/getUpdates
   ```
6. In the JSON response, find `"chat":{"id":XXXXXXX}` — that number is your **Chat ID**

You will need:
- `TELEGRAM_BOT_TOKEN` — the token from BotFather
- `TELEGRAM_CHAT_ID` — the chat ID from the URL above

---

### WhatsApp via Meta Cloud API (optional)

This requires a Meta developer account and a WhatsApp Business number. Follow these steps exactly:

#### A. Create a Meta Developer account and app

1. Go to **[developers.facebook.com](https://developers.facebook.com)** → click **Get Started** → log in with your Facebook account
2. Click **My Apps** → **Create App**
3. Select **Business** → click Next
4. Fill in app name (e.g. `StockAlerts`) and your email → click **Create App**

#### B. Add WhatsApp to your app

1. Inside your app dashboard, scroll down to find **WhatsApp** → click **Set up**
2. You'll be taken to the **WhatsApp Getting Started** page
3. Under **Step 1**, you'll see a **test phone number** already provided by Meta and a **Phone Number ID** — copy the Phone Number ID
4. Under **Step 2**, add your personal WhatsApp number as a **recipient**:
   - Click **Add phone number** → enter your number with country code (e.g. `+919876543210`)
   - You'll receive a WhatsApp OTP — enter it to verify

#### C. Get your access token

**Temporary token (for testing — expires in 24 hours):**
- On the Getting Started page, scroll to **Step 1** → click **Generate Token** — copy it

**Permanent token (recommended for production):**
1. Go to **[business.facebook.com](https://business.facebook.com)** → Settings → **System Users**
2. Click **Add** → name it (e.g. `StockAlertsBot`) → role: **Admin** → Save
3. Click **Generate New Token** on the system user → select your app → grant `whatsapp_business_messaging` permission → copy the token

#### D. Create a message template

The scraper uses a template named `alerts` with 5 variables. You must create this:

1. In your Meta App dashboard → **WhatsApp** → **Message Templates** → click **Create Template**
2. Fill in:
   - **Category:** Utility
   - **Name:** `alerts` (must be exactly this)
   - **Language:** English
3. In the body, paste exactly:
   ```
   *{{1}}*
   Company: {{2}}
   Date: {{3}}
   
   {{4}}
   
   Document: {{5}}
   ```
4. Click **Submit** — wait for approval (usually instant for Utility templates)

#### E. What you need

- `META_PHONE_NUMBER_ID` — the Phone Number ID from Step B
- `META_ACCESS_TOKEN` — the permanent token from Step C
- `WHATSAPP_TO` — comma-separated recipient numbers with country code, no `+` (e.g. `919876543210,918765432109`)

> **Note:** With a free Meta developer account, you can only message numbers you've verified. To message anyone, you need a paid WhatsApp Business API plan.

---

### DeepSeek AI summaries (optional but recommended)

1. Go to **[platform.deepseek.com](https://platform.deepseek.com)** → sign up
2. Go to **API Keys** → **Create API Key** → copy it
3. Add a small credit balance ($1–2 is enough for months of use)

- `DEEPSEEK_API_KEY` — your API key

Without this, the scraper and dashboard fall back to regex-based summaries.

---

## Step 3 — Add secrets to your forked repo

1. Go to your forked repo on GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
2. Add each secret below:

| Secret name | Required | Value |
|---|---|---|
| `EMAIL_FROM` | Yes (if using email) | your Gmail address |
| `EMAIL_PASSWORD` | Yes (if using email) | Gmail App Password |
| `EMAIL_TO` | Yes (if using email) | comma-separated recipient emails |
| `TELEGRAM_BOT_TOKEN` | No | token from BotFather |
| `TELEGRAM_CHAT_ID` | No | chat ID from getUpdates |
| `META_PHONE_NUMBER_ID` | No | from WhatsApp Getting Started page |
| `META_ACCESS_TOKEN` | No | permanent system user token |
| `WHATSAPP_TO` | No | comma-separated numbers with country code |
| `DEEPSEEK_API_KEY` | No | DeepSeek API key |

---

## Step 4 — Enable GitHub Actions

1. Go to your forked repo → click the **Actions** tab
2. Click **"I understand my workflows, go ahead and enable them"**
3. The **Stock Alert Monitor** workflow now runs automatically every 10 minutes
4. It fetches NSE announcements, sends alerts if any match, and commits new matches to `announcements.json`

To test it immediately: **Actions** → **Stock Alert Monitor** → **Run workflow** → **Run workflow**

---

## Step 5 — Deploy the dashboard (optional)

The dashboard is a separate read-only Flask app. The easiest way is Render's free tier.

### Render (free, recommended)

1. Go to **[render.com](https://render.com)** → sign up / log in with GitHub
2. Click **New** → **Web Service** → **Connect a repository** → select your fork
3. Render auto-detects `render.yaml` — click **Deploy**
4. Once deployed, go to **Environment** → **Environment Variables** and add:
   - `DEEPSEEK_API_KEY` = your DeepSeek key (for AI summaries in the dashboard)
5. Your dashboard is live at `https://your-app.onrender.com`

> **Free tier note:** Render spins down the app after 15 minutes of inactivity. First load after spin-down takes ~30 seconds.

### Run locally instead

```bash
git clone https://github.com/<your-username>/stock-alerts.git
cd stock-alerts
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python dashboard.py             # opens at http://localhost:8090
python dashboard.py 8080        # custom port
```

---

## Step 6 — Test everything

**Test the scraper locally:**
```bash
# Edit config.py with your credentials first
python scraper.py --once
```

**Backfill the last 7 days** (useful on first run to populate announcements.json):
```bash
python scraper.py --backfill 7
```

**Check the logs:**
```bash
cat alerts.log
```

---

## Project structure

```
stock-alerts/
├── scraper.py              # alert engine — fetch, filter, notify
├── dashboard.py            # Flask web dashboard with AI summaries
├── config.py               # credentials and settings (local use only)
├── requirements.txt
├── announcements.json      # rolling 30-day matched announcements DB
├── seen_ids.json           # dedup cache (auto-managed)
├── data/                   # per-company history cache
├── companies.json          # NSE symbol → company name map
├── Dockerfile
├── render.yaml
└── .github/workflows/
    ├── scraper.yml         # runs every 10 min, commits announcements.json
    └── fetch_company.yml   # on-demand company history fetch
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| No email received | Check spam; verify App Password is correct; check `alerts.log` |
| `SMTPAuthenticationError` | Re-generate the Gmail App Password |
| WhatsApp message not sent | Check the template name is exactly `alerts`; verify the recipient number is verified on your Meta account |
| WhatsApp `131030` error | Template not approved yet — wait a few minutes and retry |
| NSE fetch returns empty | NSE blocks non-browser requests intermittently — the scraper retries automatically |
| Dashboard shows no data | Run `python scraper.py --backfill 7` locally and commit `announcements.json` |
| AI summaries not showing | Set `DEEPSEEK_API_KEY` in Render Environment Variables; check your DeepSeek account has credit |
| GitHub Actions not running | Check the Actions tab is enabled; verify secrets are set correctly |
| Render dashboard slow | Free tier spins down after 15 min inactivity — first load takes ~30s |
