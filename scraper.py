"""
BSE / NSE Corporate Announcement Filter
Monitors for merger, demerger, split announcements and sends alerts.
"""

import os
import json
import time
import logging
import smtplib
import hashlib
import requests
import schedule
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from bs4 import BeautifulSoup
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("alerts.log"),
    ],
)
log = logging.getLogger(__name__)


# Keywords that trigger an alert

# Keywords matched against the FULL announcement body text (attchmntText / BSE body)
# These are specific enough that a match in body text = genuine corporate action
KEYWORDS = [
    # Demerger
    "demerger", "de-merger", "demerge",
    # Merger / amalgamation
    "merger", "amalgamation", "amalgamate",
    # Stock split / bonus
    "stock split", "share split", "sub-division of equity",
    "subdivision of equity", "face value split",
    # Spin-off / hive-off
    "spin-off", "spinoff", "hive off", "hive-off",
    # Composite scheme (only in body, not category label)
    "composite scheme of arrangement",
    "scheme of amalgamation",
    "scheme of demerger",
    "scheme of merger",
    "scheme of arrangement for",       # "for" weeds out bare category mentions
]

# NSE desc categories that look relevant but are too broad — skip if ONLY the
# category matches and the body text doesn't contain a specific keyword above.
# (Handled in is_relevant() below — body text must contain at least one KEYWORD)


# Seen-IDs cache  (persisted to disk)

CACHE_FILE = Path("seen_ids.json")


def load_cache() -> set:
    if CACHE_FILE.exists():
        return set(json.loads(CACHE_FILE.read_text()))
    return set()


def save_cache(cache: set):
    CACHE_FILE.write_text(json.dumps(list(cache)))


# ──────────────────────────────────────────────
# NSE fetch  (equities + SME in one session)
# ──────────────────────────────────────────────
def _nse_session() -> requests.Session:
    """Create a warmed-up NSE session (one warmup, reused for both indexes)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
    })
    try:
        session.get("https://www.nseindia.com/", timeout=15)
        time.sleep(0.5)
    except Exception:
        pass
    return session


def _fetch_nse_index(session: requests.Session, index: str, lookback_days: int) -> list[dict]:
    """Fetch one NSE index (equities or sme) and return normalised rows."""
    params = {
        "index":     index,
        "from_date": (datetime.today() - timedelta(days=lookback_days)).strftime("%d-%m-%Y"),
        "to_date":   datetime.today().strftime("%d-%m-%Y"),
    }
    source_label = "NSE" if index == "equities" else "NSE-SME"
    id_prefix    = "NSE" if index == "equities" else "NSESME"
    try:
        r = session.get(
            "https://www.nseindia.com/api/corporate-announcements",
            params=params, timeout=25
        )
        r.raise_for_status()
        rows = r.json()
        results = []
        for row in rows:
            full_text = row.get("attchmntText", "") or ""
            category  = row.get("desc", "") or ""
            seq_id    = row.get("seq_id", "")
            results.append({
                "source":   source_label,
                "id":       f"{id_prefix}_{seq_id}",
                "company":  row.get("sm_name", row.get("symbol", "Unknown")),
                "headline": category,
                "body":     full_text,
                "date":     row.get("an_dt", ""),
                "url":      row.get("attchmntFile", ""),
                "scrip":    row.get("symbol", ""),
            })
        log.info("%s: fetched %d announcements (last %d days)", source_label, len(results), lookback_days)
        return results
    except Exception as e:
        log.warning("%s fetch failed: %s", source_label, e)
        return []


def fetch_all_nse() -> list[dict]:
    """Fetch equities + SME from NSE in a single session. Covers virtually all listed companies."""
    cache = load_cache()
    lookback_days = 30 if not cache else 2
    log.info("NSE: fetching last %d days across equities + SME segments", lookback_days)

    session = _nse_session()
    equities = _fetch_nse_index(session, "equities", lookback_days)
    time.sleep(0.5)   # brief pause between the two calls
    sme      = _fetch_nse_index(session, "sme",      lookback_days)
    return equities + sme

# Noise phrases — if ANY of these appear in the body, it's a procedural
# update on an already-announced scheme, not a new announcement.
# We skip these to avoid duplicate alerts for the same corporate action.
# Set SKIP_PROCEDURAL = False in config.py if you want ALL updates.

PROCEDURAL_PHRASES = [
    # NCLT-convened meetings (creditors / shareholders) = mid-process filings
    "meeting of the unsecured creditors",
    "meeting of the secured creditors",
    "meeting of the equity shareholders",
    "meeting of the preference shareholders",
    "court convened meeting",
    "nclt convened meeting",
    # Exchange asking company to respond to news — not a company announcement
    "the exchange has sought clarification",
    "the response from the company is awaited",
    # Mere acknowledgement that scheme is pending, no new action
    "the scheme is pending",
    "pursuant to the scheme already",
]

# Filter

def is_relevant(ann: dict) -> bool:
    body = (ann.get("body", "") or "").lower()

    # Must contain at least one merger/demerger/split keyword
    if not any(kw in body for kw in KEYWORDS):
        return False

    # Skip procedural updates (NCLT meetings, exchange clarification requests, etc.)
    if getattr(Config, "SKIP_PROCEDURAL", True):
        if any(phrase in body for phrase in PROCEDURAL_PHRASES):
            log.debug("Skipped procedural filing: %s", ann.get("company"))
            return False

    return True

# Email notification

def send_email(announcements: list[dict]):
    if not Config.EMAIL_ENABLED:
        return
    if not announcements:
        return

    subject = f"{len(announcements)} Corporate Announcements — {datetime.today().strftime('%d %b %Y')}"

    html_rows = ""
    for a in announcements:
        html_rows += f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;font-weight:600">{a['source']}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{a['company']} ({a.get('scrip','')})</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{a['headline']}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{a['date']}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">
            <a href="{a['url']}" style="color:#0052cc">View</a>
          </td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
    <h2 style="color:#000">Corporate Announcements</h2>
    <p>The following corporate announcements are relevant:</p>
    <table style="border-collapse:collapse;width:100%">
      <thead>
        <tr style="background:#f4f4f4">
          <th style="padding:8px 10px;text-align:left">Exchange</th>
          <th style="padding:8px 10px;text-align:left">Company</th>
          <th style="padding:8px 10px;text-align:left">Headline</th>
          <th style="padding:8px 10px;text-align:left">Date</th>
          <th style="padding:8px 10px;text-align:left">Link</th>
        </tr>
      </thead>
      <tbody>{html_rows}</tbody>
    </table>
    <p style="color:#888;font-size:12px;margin-top:20px">
      Fetched at {datetime.now().strftime('%d %b %Y %H:%M')} IST
    </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = Config.EMAIL_FROM
    msg["To"]      = ", ".join(Config.EMAIL_TO)
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(Config.EMAIL_FROM, Config.EMAIL_PASSWORD)
            server.sendmail(Config.EMAIL_FROM, Config.EMAIL_TO, msg.as_string())
        log.info("Email sent to %s", Config.EMAIL_TO)
    except Exception as e:
        log.error("Email send failed: %s", e)


# Telegram notification

def send_telegram(announcements: list[dict]):
    if not Config.TELEGRAM_ENABLED:
        return
    if not announcements:
        return

    for a in announcements:
        text = (
            f"{a['source']}\n"
            f"*Company:* {a['company']} ({a.get('scrip','')})\n"
            f"*Headline:* {a['headline']}\n"
            f"*Date:* {a['date']}\n"
            f"[View Announcement]({a['url']})"
        )
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id":    Config.TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "Markdown",
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
            log.info("Telegram alert sent for: %s", a["company"])
        except Exception as e:
            log.error("Telegram send failed: %s", e)

# Main job

def run_check():
    t_total = time.time()
    log.info("═══ Starting announcement check ═══")
    cache = load_cache()

    t_fetch = time.time()
    all_announcements = fetch_all_nse()   # equities + SME segments
    fetch_secs = time.time() - t_fetch

    t_filter = time.time()
    new_relevant = []
    for ann in all_announcements:
        if ann["id"] in cache:
            continue
        if is_relevant(ann):
            new_relevant.append(ann)
            cache.add(ann["id"])
    filter_secs = time.time() - t_filter

    log.info("New relevant announcements found: %d", len(new_relevant))

    t_notify = time.time()
    if new_relevant:
        send_email(new_relevant)
        send_telegram(new_relevant)
    notify_secs = time.time() - t_notify

    save_cache(cache)

    total_secs = time.time() - t_total
    log.info(
        "⏱  Timing — fetch: %.1fs | filter: %.1fs | notify: %.1fs | TOTAL: %.1fs",
        fetch_secs, filter_secs, notify_secs, total_secs
    )
    log.info("═══ Check complete ═══\n")


# Entry point

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="BSE/NSE Merger-Demerger-Split Alert Monitor")
    parser.add_argument(
        "--backfill", type=int, metavar="DAYS", default=None,
        help="Force a one-time scan of the last N days (ignores cache for fetching, still deduplicates alerts)"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run once and exit (no hourly loop)"
    )
    args = parser.parse_args()

    if args.backfill:
        # Override lookback in both fetchers by temporarily emptying the cache file
        log.info("Backfill mode: scanning last %d days", args.backfill)
        original_cache = load_cache()
        # Temporarily delete cache so fetchers see empty cache → use backfill window
        CACHE_FILE.rename(CACHE_FILE.with_suffix(".bak")) if CACHE_FILE.exists() else None
        # Patch lookback days
        import scraper as _self
        _self._BACKFILL_DAYS = args.backfill
        run_check()
        # Restore original cache file
        bak = CACHE_FILE.with_suffix(".bak")
        if bak.exists():
            # Merge: keep old seen IDs + any new ones just added
            new_cache = load_cache()
            merged = original_cache | new_cache
            save_cache(merged)
            bak.unlink()
        log.info("Backfill complete.")
    elif args.once:
        log.info("BSE/NSE Alert Monitor — single run")
        run_check()
    else:
        log.info("BSE/NSE Alert Monitor starting — runs every hour")
        run_check()  # run immediately on start
        schedule.every(60).minutes.do(run_check)

        while True:
            schedule.run_pending()
            time.sleep(1)