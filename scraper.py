"""
BSE / NSE Corporate Announcement Filter
Monitors for merger, demerger, scheme of arrangement announcements.
AI-powered summary via DeepSeek (falls back to pattern-based if no API key).
WhatsApp alerts via Meta WhatsApp Cloud API — template: alerts (5 variables)
"""

import os
import io
import re
import json
import time
import logging
import smtplib
import requests
import schedule
import pdfplumber
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("alerts.log")],
)
log = logging.getLogger(__name__)
logging.getLogger("pdfminer").setLevel(logging.ERROR)


# ══════════════════════════════════════════════════════════════════════════════
# WHAT WE WANT
#   ✅ Mergers, demergers, amalgamations
#   ✅ All scheme-of-arrangement variants
#   ✅ Spin-offs, hive-offs, slump sales, business/undertaking transfers
#   ✅ Restructuring / reorganisation (with scheme context)
#   ✅ Open offers ONLY when body also mentions merger/demerger/scheme
#
# WHAT WE DO NOT WANT
#   ❌ Standalone open offer updates (daily "no shares tendered" updates)
#   ❌ Stock splits, share splits, sub-division
#   ❌ Acquisitions (share purchases, stake buys)
#   ❌ SEBI Takeover Regulations disclosures (pledge/encumbrance)
#   ❌ Promoter shareholding changes / SAST filings
#   ❌ Quarterly results, buybacks, dividends, rights issues, bonus
#   ❌ Scrutinizer reports, postal ballot, AGM/EGM notices (unless scheme)
#   ❌ Auditor/KMP/director appointments/resignations
#   ❌ Insolvency petitions, legal disputes (unless scheme is central)
#   ❌ Name change, trading window, analyst calls
# ══════════════════════════════════════════════════════════════════════════════

# ── KEYWORDS (must appear in headline or body) ────────────────────────────────
KEYWORDS = [
    "merger", "amalgamation", "amalgamate",
    "demerger", "de-merger", "demerge",
    "scheme of arrangement", "scheme of amalgamation",
    "scheme of demerger", "scheme of merger",
    "scheme of reconstruction", "composite scheme",
    "arrangement between", "arrangement amongst",
    "draft scheme", "proposed scheme", "revised scheme",
    "scheme approved", "scheme sanctioned",
    "approval of scheme", "sanction of scheme",
    "spin-off", "spinoff", "spin off",
    "hive off", "hive-off",
    "slump sale", "business transfer",
    "transfer of business", "transfer of undertaking",
    "demerged undertaking", "vesting of undertaking",
    "transfer and vesting",
    "nclt", "national company law tribunal",
    "appointed date", "effective date of scheme",
    "resulting company", "transferor company", "transferee company",
    "share exchange ratio", "swap ratio",
    "restructuring", "reorganisation", "reorganization",
    "consolidation of business",
    # Open offer ONLY kept as a keyword — but gated separately in is_relevant
    "open offer",
]

HEADLINE_HINTS = [
    "merger", "demerger", "amalgam", "demerge",
    "scheme of", "composite scheme", "arrangement",
    "restructur", "reorganis", "reorganiz",
    "spin-off", "spinoff", "spin off", "hive off", "hive-off",
    "slump sale", "open offer",
    "nclt", "transferor", "transferee", "resulting company",
    "appointed date", "effective date",
    "vesting", "demerged undertaking",
]

# ── HARD EXCLUDE — drop immediately ───────────────────────────────────────────
_HARD_EXCLUDE = re.compile(
    r"(?:"
    # SEBI Takeover Regulations pledge/encumbrance — match the HEADLINE directly
    r"disclosure under sebi takeover|"
    r"disclosure under\s+reg(?:ulation)?\s+3[01]\b|"
    r"sebi\s*\(substantial acquisition|"
    r"sebi\s*\(substential acquisition|"
    r"substantial acquisition of shares|"
    r"regulation 29\b|regulation 31\b|"
    r"pledg|encumbr|"
    r"inter.?se transfer|creeping acquisition|"

    # Stock splits / sub-division
    r"stock\s+split|share\s+split|"
    r"sub.?division\s+of\s+(?:equity|shares?)|"
    r"face\s+value\s+(?:split|reduct)|"

    # Acquisitions (share purchases, not corporate deals)
    r"acquisition\s+of\s+(?:shares?|equity|stake|securities)|"
    r"acquired?\s+\d[\d,]+\s+(?:equity\s+)?shares?|"
    r"purchase\s+of\s+(?:shares?|equity|stake)|"
    r"open\s+market\s+(?:purchase|sale)|"
    r"block\s+deal|bulk\s+deal|"

    # Financial results
    r"(?:quarterly|q[1-4]|half.?year|annual)\s+(?:results?|financial\s+results?)|"
    r"standalone\s+(?:and\s+consolidated\s+)?financial\s+results?|"
    r"unaudited\s+financial\s+results?|"

    # Dividends / buybacks / rights / bonus
    r"\bdividend\b|buy.?back|rights\s+issue|bonus\s+(?:shares?|issue)|"

    # Purely procedural — no scheme context
    r"postal\s+ballot\b|scrutinizer.{0,30}report|"
    r"notice\s+of\s+(?:agm|annual\s+general)|"

    # Director / auditor / KMP changes
    r"appointment\s+of\s+(?:independent\s+)?(?:director|auditor|cfo|ceo|md\b|kmp)|"
    r"resignation\s+of\s+(?:director|auditor|cfo|ceo|md\b|kmp)|"

    # Debt instruments
    r"credit\s+rating|non.?convertible\s+debenture|\bncd\b|"

    # Pure legal/insolvency with no scheme link
    r"insolvency\s+(?:petition|resolution|proceeding)|"
    r"corporate\s+insolvency\s+resolution|"

    # Misc noise
    r"trading\s+window|insider\s+trading|"
    r"analyst\s+(?:meet|call|conference)|"
    r"investor\s+(?:meet|call|conference|presentation)|"
    r"earnings\s+call|"
    r"change\s+(?:in\s+)?(?:name\s+of\s+(?:company|director)|company\s+name)|"
    r"name\s+change\b|"

    # Strategic/technical/marketing tie-up (NSE category for JV/alliances, not schemes)
    r"arrangements?\s+for\s+strategic,?\s+technical|"
    r"strategic,?\s+technical,?\s+manufacturing"
    r")",
    re.IGNORECASE,
)

# ── Open offer gate: only allow if body ALSO mentions merger/demerger/scheme ──
_OPEN_OFFER_SCHEME_RE = re.compile(
    r"merger|amalgamation|demerger|scheme\s+of\s+(?:arrangement|amalgamation|demerger)|"
    r"composite\s+scheme|restructuring|reorgani[sz]|hive.?off|slump\s+sale",
    re.IGNORECASE,
)

# ── Procedural meeting gate ────────────────────────────────────────────────────
_PROCEDURAL_MEETING = re.compile(
    r"meeting of the (?:unsecured|secured) creditors|"
    r"meeting of the (?:equity|preference) shareholders|"
    r"court convened meeting|nclt convened meeting",
    re.IGNORECASE,
)

_NAMED_SCHEME = re.compile(
    r"(?:composite\s+)?scheme\s+of\s+(?:arrangement|amalgamation|demerger|merger|reconstruction)|"
    r"amalgamation\s+of|demerger\s+of|merger\s+(?:of|between)|"
    r"transferor\s+compan|transferee\s+compan|resulting\s+compan|"
    r"slump\s+sale|hive.?off|spin.?off|"
    r"nclt|appointed\s+date|effective\s+date",
    re.IGNORECASE,
)

# Patterns in the BODY that indicate the announcement is NOT about the company's
# own corporate scheme — applied with a strong-scheme safety override below.
# RTA (Registrar & Transfer Agent) service-change noise patterns.
# RTA mergers are small-company fast-track deals approved by the Regional Director
# under Section 233 — they use the same M&A terminology (transferor/transferee
# company, scheme of amalgamation, appointed date) as real corporate schemes,
# so _SCHEME_STRONG can’t serve as a safe override. Use NCLT presence instead.
_RTA_NOISE = re.compile(
    r"(?:"
    # "merger/amalgamation of ... registrar / RTA" (any words up to 80 chars between)
    r"(?:merger|amalgamation)\s+of\s+(?:.{0,80}\s)?(?:registrar\s+and\s+(?:share\s+)?transfer\s+agent|\brta\b)|"
    # "registrar and share transfer agent ... merg..." (merged / merger / amalgamated within 200 chars)
    r"registrar\s+and\s+(?:share\s+)?transfer\s+agent.{0,200}(?:merg\w*|amalgamat\w*)|"
    # RTA labeled as transferor/transferee company
    r"\brta\b.{0,200}(?:transferor|transferee)\s+compan|"
    # "intimation for/regarding merger/amalgamation of registrar/RTA"
    r"intimation\s+(?:for|regarding)\s+(?:the\s+)?(?:merger|amalgamation)\s+of\s+(?:registrar|\brta\b)|"
    # Known RTA companies in the current wave (CB Management Services → MUFG Intime)
    r"cb\s+management\s+services.{0,300}(?:registrar|transfer\s+agent|\brta\b)|"
    r"(?:registrar|transfer\s+agent|\brta\b).{0,300}cb\s+management\s+services|"
    r"cb\s+management\s+services.{0,300}mufg\s+intime|"
    r"mufg\s+intime.{0,300}cb\s+management\s+services"
    r")",
    re.IGNORECASE,
)

# Real listed-company schemes always go through NCLT (not Regional Director).
# Presence of NCLT in the announcement overrides _RTA_NOISE.
_NCLT_RE = re.compile(
    r"\bnclt\b|national\s+company\s+law\s+tribunal",
    re.IGNORECASE,
)

# WOS/subsidiary formation — purely administrative, not a scheme.
_WOS_NOISE = re.compile(
    r"incorporation\s+of\s+(?:a\s+)?(?:new\s+)?(?:wholly\s+owned\s+subsidiary|\bwos\b)",
    re.IGNORECASE,
)

# Broad scheme indicator — used only as override for WOS gate.
_SCHEME_STRONG = re.compile(
    r"scheme\s+of\s+(?:arrangement|amalgamation|demerger|merger|reconstruction)|"
    r"composite\s+scheme|"
    r"\bnclt\b|national\s+company\s+law\s+tribunal|"
    r"appointed\s+date|"
    r"transferor\s+compan|transferee\s+compan|resulting\s+compan",
    re.IGNORECASE,
)

# ── Deduplicate open offer updates: one alert per company per run ──────────────
# Tracks (company, "open_offer") pairs within a single run_check() call
_SEEN_OPEN_OFFER_THIS_RUN: set = set()


# ══════════════════════════════════════════════════════════════════════════════
# Helper utilities
# ══════════════════════════════════════════════════════════════════════════════

def _headline_looks_relevant(headline: str) -> bool:
    h = (headline or "").lower()
    return any(hint in h for hint in HEADLINE_HINTS)

def _body_is_weak(body: str) -> bool:
    return len((body or "").strip()) < 200

def _extract_pdf_text(url: str, session: requests.Session) -> str:
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            return "\n".join(
                page.extract_text() or "" for page in pdf.pages[:10]
            ).strip()
    except Exception as e:
        log.debug("PDF extract failed for %s: %s", url, e)
        return ""

def _get_status(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["approved", "sanctioned", "effective"]):
        return "Approved"
    if any(w in t for w in ["filed", "application"]):
        return "Filed"
    if any(w in t for w in ["proposed", "draft"]):
        return "Proposed"
    if any(w in t for w in ["pending", "hearing"]):
        return "Pending"
    return "Update"

def _get_action(text: str) -> str:
    t = text.lower()
    if "demerger" in t or "de-merger" in t:
        return "Demerger"
    if "amalgamat" in t:
        return "Amalgamation"
    if "merger" in t:
        return "Merger"
    if "slump sale" in t:
        return "Slump Sale"
    if "hive" in t:
        return "Hive-Off"
    if "spin" in t:
        return "Spin-Off"
    if "open offer" in t:
        return "Open Offer"
    return "Scheme"

_DATE_RE = re.compile(
    r"\b(\d{1,2}[\s\-/](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-/]\d{2,4})"
    r"|\b(\d{4}[\-/]\d{2}[\-/]\d{2})\b",
    re.IGNORECASE,
)

def _get_best_date(text: str) -> str:
    m = _DATE_RE.search(text)
    return m.group(0) if m else ""

def clean_body(body: str) -> str:
    return re.sub(r"\s+", " ", (body or "").replace("\n", " ").replace("\t", " ")).strip()

# Marks where actual announcement content begins — everything before this is
# letter-header boilerplate (exchange addresses, scrip codes, "Dear Sir/Madam", "Sub:").
_CONTENT_START_RE = re.compile(
    r"(?:we\s+(?:wish\s+to|hereby|would\s+like\s+to)\s+inform|"
    r"this\s+is\s+to\s+inform|"
    r"pursuant\s+to\s+(?:the\s+)?(?:regulation|sebi|provision)|"
    r"with\s+reference\s+to\s+the\s+(?:captioned|above|subject)|"
    r"in\s+continuation\s+of|further\s+to\s+our|"
    r"as\s+required\s+under|in\s+terms\s+of\s+(?:the\s+)?(?:sebi|regulation)|"
    r"in\s+compliance\s+with|in\s+accordance\s+with\s+(?:the\s+)?(?:sebi|regulation)|"
    r"kindly\s+note\s+that|please\s+note\s+that|"
    r"we\s+(?:are\s+pleased\s+to|regret\s+to)\s+inform)",
    re.IGNORECASE,
)

def _strip_preamble(body: str) -> str:
    """Remove exchange addresses, scrip codes, Dear Sir/Madam, Sub: header."""
    m = _CONTENT_START_RE.search(body)
    return body[m.start():] if m else body

def _ai_summarise(body: str, company: str = "", headline: str = "") -> str:
    api_key = getattr(Config, "DEEPSEEK_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key or not body:
        return ""
    try:
        content = _strip_preamble(body)
        prompt = (
            f"Summarise this corporate announcement in 2-3 concise sentences. "
            f"Company: {company}. Headline: {headline}.\n\nAnnouncement:\n{content[:4000]}"
        )
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.3,
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.debug("AI summarise failed: %s", e)
        return ""

def _fallback_sentences(body: str, headline: str = "") -> str:
    if not body:
        return headline or ""
    content = _strip_preamble(body)
    sentences = re.split(r"(?<=[.!?])\s+", content.strip())
    # Skip fragments under 40 chars — these are typically address lines or headers
    relevant = [
        s for s in sentences
        if len(s) > 40 and any(kw in s.lower() for kw in KEYWORDS)
    ]
    if not relevant:
        # No keyword match in content — take the first substantive sentences
        relevant = [s for s in sentences if len(s) > 60]
    chosen = relevant[:3] if relevant else sentences[:3]
    return " ".join(chosen)[:500]

def build_summary(body: str, company: str = "", headline: str = "") -> str:
    plain = _ai_summarise(body, company=company, headline=headline)
    if not plain:
        plain = _fallback_sentences(clean_body(body), headline=headline)
    return plain or headline or ""

_MCAP_CACHE: dict = {}

def passes_market_cap_filter(ann: dict, session: requests.Session) -> bool:
    min_cr = getattr(Config, "MARKET_CAP_MIN_CR", 0)
    if not min_cr:
        return True
    scrip = ann.get("scrip", "") or ""
    if not scrip:
        return True
    if scrip in _MCAP_CACHE:
        mcap = _MCAP_CACHE[scrip]
    else:
        try:
            r = session.get(
                f"https://www.nseindia.com/api/quote-equity?symbol={scrip}",
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            mcap = (
                data.get("marketDeptOrderBook", {})
                    .get("tradeInfo", {})
                    .get("totalMarketCap", 0)
            )
            _MCAP_CACHE[scrip] = mcap
        except Exception as e:
            log.debug("Market cap fetch failed for %s: %s", scrip, e)
            return True
    if mcap and mcap < min_cr:
        log.info("Skipped (market cap ₹%.0f Cr < ₹%d Cr): %s", mcap, min_cr, ann.get("company"))
        return False
    return True

_DB_FILE = Path("announcements.json")

_DB_DAYS = 30  # keep entries from the last N days

def save_to_announcements_db(ann: dict, summary: str):
    try:
        db = json.loads(_DB_FILE.read_text()) if _DB_FILE.exists() else []
        if any(r.get("id") == ann.get("id") for r in db):
            return  # already saved
        db.append({
            "id":       ann.get("id"),
            "source":   ann.get("source"),
            "company":  ann.get("company"),
            "scrip":    ann.get("scrip"),
            "headline": ann.get("headline"),
            "date":     ann.get("date"),
            "url":      ann.get("url"),
            "summary":  summary,
            "saved_at": datetime.now().isoformat(),
        })
        # Drop entries older than _DB_DAYS so the file stays lean
        cutoff = (datetime.now() - timedelta(days=_DB_DAYS)).isoformat()
        db = [r for r in db if r.get("saved_at", "9999") >= cutoff]
        _DB_FILE.write_text(json.dumps(db, indent=2))
    except Exception as e:
        log.debug("DB save failed: %s", e)




# ══════════════════════════════════════════════════════════════════════════════
# Cache
# ══════════════════════════════════════════════════════════════════════════════

CACHE_FILE = Path("seen_ids.json")

def load_cache() -> set:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def save_cache(cache: set):
    CACHE_FILE.write_text(json.dumps(list(cache)))


# ──────────────────────────────────────────────
# NSE fetch  (equities + SME in one session)
# ──────────────────────────────────────────────
def _nse_session() -> requests.Session:
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
            params=params, timeout=25,
        )
        r.raise_for_status()
        results = []
        for row in r.json():
            results.append({
                "source":   source_label,
                "id":       f"{id_prefix}_{row.get('seq_id', '')}",
                "company":  row.get("sm_name", row.get("symbol", "Unknown")),
                "headline": row.get("desc", "") or "",
                "body":     row.get("attchmntText", "") or "",
                "date":     row.get("an_dt", ""),
                "url":      row.get("attchmntFile", "") or "",
                "scrip":    row.get("symbol", ""),
            })
        log.info("%s: fetched %d announcements (last %d days)", source_label, len(results), lookback_days)
        return results
    except Exception as e:
        log.warning("%s fetch failed: %s", source_label, e)
        return []

def fetch_all_nse() -> tuple[list[dict], requests.Session]:
    cache = load_cache()
    lookback_days = 3 if not cache else 2
    log.info("NSE: fetching last %d days across equities + SME segments", lookback_days)
    session  = _nse_session()
    equities = _fetch_nse_index(session, "equities", lookback_days)
    time.sleep(0.5)
    sme      = _fetch_nse_index(session, "sme", lookback_days)
    return equities + sme, session


# ══════════════════════════════════════════════════════════════════════════════
# Relevance filter
# ══════════════════════════════════════════════════════════════════════════════

def is_relevant(ann: dict) -> bool:
    """
    Layer 1 — Hard exclude: SEBI Takeover pledge filings (matched by headline
                             pattern), splits, results, buybacks, noise.
    Layer 2 — Must match at least one scheme/merger/demerger keyword.
    Layer 3 — Open offer gate: open offer is only relevant if the body ALSO
                             contains merger/demerger/scheme language. Daily
                             "no shares tendered" status updates are dropped.
                             One open-offer alert per company per run.
    Layer 4 — Procedural meeting gate: bare creditor/shareholder meeting
                             notices dropped unless a named scheme is present.
    """
    headline = (ann.get("headline", "") or "").lower()
    body     = (ann.get("body",     "") or "").lower()
    combined = headline + " " + body

    # Layer 1: Hard exclude — applied to headline only.
    # The body is PDF text after enrichment; scheme documents routinely contain
    # words like "dividend", "acquisition of equity shares", "rights issue" etc.
    # as part of their legal description, which would falsely exclude them if we
    # searched the full body.
    if _HARD_EXCLUDE.search(headline):
        log.debug("Skipped (hard-exclude): %s | %s", ann.get("company"), ann.get("headline"))
        return False

    # Layer 2: Must match at least one keyword
    if not any(kw in combined for kw in KEYWORDS):
        return False

    # Layer 2b: RTA noise gate
    # RTA mergers use scheme terminology (transferor/transferee, appointed date,
    # scheme of amalgamation) but are approved by Regional Director, not NCLT.
    # Genuine listed-company schemes always mention NCLT — use that as the override.
    if _RTA_NOISE.search(body) and not _NCLT_RE.search(combined):
        log.debug("Skipped (RTA merger notification, no NCLT): %s | %s", ann.get("company"), ann.get("headline"))
        return False

    # Layer 2c: WOS incorporation gate
    if _WOS_NOISE.search(body) and not _SCHEME_STRONG.search(combined):
        log.debug("Skipped (WOS incorporation, no scheme context): %s | %s", ann.get("company"), ann.get("headline"))
        return False

    # Layer 3: Open offer gate
    if "open offer" in combined:
        # Must also have scheme/merger/demerger context
        if not _OPEN_OFFER_SCHEME_RE.search(combined):
            log.debug("Skipped (open offer without scheme context): %s", ann.get("company"))
            return False
        # Deduplicate: one alert per company per run
        company_key = (ann.get("company", "") or "").lower()
        if company_key in _SEEN_OPEN_OFFER_THIS_RUN:
            log.debug("Skipped (duplicate open offer this run): %s", ann.get("company"))
            return False
        _SEEN_OPEN_OFFER_THIS_RUN.add(company_key)

    # Layer 4: Procedural meeting gate
    if _PROCEDURAL_MEETING.search(combined):
        if not _NAMED_SCHEME.search(combined):
            log.debug("Skipped (bare meeting notice, no scheme context): %s", ann.get("company"))
            return False

    return True


# ══════════════════════════════════════════════════════════════════════════════
# PDF enrichment (parallel)
# ══════════════════════════════════════════════════════════════════════════════

def enrich_with_pdf(candidates: list[dict], session: requests.Session) -> list[dict]:
    needs_pdf = [
        a for a in candidates
        if _body_is_weak(a.get("body", "") or "") and a.get("url")
    ]
    if not needs_pdf:
        return candidates
    log.info("Fetching PDFs for %d candidates...", len(needs_pdf))

    def fetch_one(ann):
        text = _extract_pdf_text(ann["url"], session)
        if text and len(text) > len(ann.get("body", "") or ""):
            ann["body"] = text
        return ann

    with ThreadPoolExecutor(max_workers=5) as pool:
        for f in as_completed({pool.submit(fetch_one, a): a for a in needs_pdf}):
            try:
                f.result()
            except Exception as e:
                log.debug("PDF error: %s", e)
    return candidates


# ══════════════════════════════════════════════════════════════════════════════
# Email
# ══════════════════════════════════════════════════════════════════════════════

def send_email(announcements: list[dict]):
    if not Config.EMAIL_ENABLED or not announcements:
        return

    subject = f"{len(announcements)} Corporate Announcements — {datetime.today().strftime('%d %b %Y')}"
    html_rows = ""
    for a in announcements:
        summary = build_summary(
            a.get("body", "") or "",
            company=a.get("company", ""),
            headline=a.get("headline", ""),
        )
        html_rows += f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;font-weight:600">{a['source']}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{a['company']} ({a.get('scrip','')})</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{a['headline']}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{a['date']}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">
            <a href="{a['url']}" style="color:#0052cc">View</a>
          </td>
        </tr>
        <tr>
          <td colspan="5" style="padding:6px 10px 14px 10px;border-bottom:2px solid #ddd;
              font-size:12px;color:#333;background:#f9f9f9;line-height:1.6">
            {summary}
          </td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
    <h2 style="color:#000">Corporate Announcements</h2>
    <p>The following corporate announcements are relevant:</p>
    <table style="border-collapse:collapse;width:100%">
      <thead><tr style="background:#f4f4f4">
        <th style="padding:8px 10px;text-align:left">Exchange</th>
        <th style="padding:8px 10px;text-align:left">Company</th>
        <th style="padding:8px 10px;text-align:left">Headline</th>
        <th style="padding:8px 10px;text-align:left">Date</th>
        <th style="padding:8px 10px;text-align:left">Link</th>
      </tr></thead>
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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(Config.EMAIL_FROM, Config.EMAIL_PASSWORD)
            s.sendmail(Config.EMAIL_FROM, Config.EMAIL_TO, msg.as_string())
        log.info("Email sent to %s", Config.EMAIL_TO)
    except Exception as e:
        log.error("Email send failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# WhatsApp via Meta Cloud API
# ══════════════════════════════════════════════════════════════════════════════

_WA_MAX_LEN = 1024

def clean_wa_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\n", " ").replace("\t", " ")).strip()

def _wa_var(value: str, limit: int = _WA_MAX_LEN) -> str:
    v = clean_wa_text(value)
    if len(v) > limit:
        v = v[:limit - 1] + "…"
    return v or "-"

def send_whatsapp(announcements: list[dict]):
    if not Config.WHATSAPP_ENABLED or not announcements:
        return

    phone_number_id = (getattr(Config, "META_PHONE_NUMBER_ID", "") or "").strip()
    access_token    = (getattr(Config, "META_ACCESS_TOKEN",    "") or "").strip()

    if not phone_number_id or not access_token:
        log.warning("WhatsApp: META_PHONE_NUMBER_ID or META_ACCESS_TOKEN not set — skipping")
        return

    if not Config.WHATSAPP_TO:
        log.warning("WhatsApp: WHATSAPP_TO list is empty — skipping")
        return

    api_url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
    }

    for a in announcements:
        combined_raw = (a.get("body", "") or "") + " " + (a.get("headline", "") or "")
        flat = re.sub(r"\s+", " ", combined_raw.replace("\n", " ")).strip()

        status   = _get_status(flat)
        action   = _get_action(flat)
        company  = a.get("company", "Unknown")
        scrip    = a.get("scrip", "")
        headline = a.get("headline", "")
        date_str = a.get("date", "") or _get_best_date(flat) or "-"
        url      = a.get("url", "-") or "-"

        para = _ai_summarise(a.get("body", "") or "", company=company, headline=headline)
        if not para:
            para = _fallback_sentences(
                clean_body(a.get("body", "") or ""),
                headline=headline,
            )

        var1 = _wa_var(f"{status} | {action}")
        var2 = _wa_var(f"{company} ({scrip})" if scrip else company)
        var3 = _wa_var(date_str)
        var4 = _wa_var(f"{headline} - {para}" if para else headline, limit=900)
        var5 = _wa_var(url)

        for recipient in Config.WHATSAPP_TO:
            payload = {
                "messaging_product": "whatsapp",
                "to":   recipient,
                "type": "template",
                "template": {
                    "name":     "alerts",
                    "language": {"code": "en"},
                    "components": [{
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": var1},
                            {"type": "text", "text": var2},
                            {"type": "text", "text": var3},
                            {"type": "text", "text": var4},
                            {"type": "text", "text": var5},
                        ],
                    }],
                },
            }
            try:
                r = requests.post(api_url, headers=headers, json=payload, timeout=10)
                r.raise_for_status()
                log.info("WhatsApp alert sent to %s for: %s", recipient, a["company"])
            except Exception as e:
                log.error("WhatsApp send failed for %s: %s", recipient, e)


def send_telegram(announcements: list[dict]):
    if not Config.TELEGRAM_ENABLED or not announcements:
        return
    bot_token = (getattr(Config, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id   = (getattr(Config, "TELEGRAM_CHAT_ID",   "") or "").strip()
    if not bot_token or not chat_id:
        log.warning("Telegram: BOT_TOKEN or CHAT_ID not set — skipping")
        return
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for a in announcements:
        plain = _ai_summarise(a.get("body", "") or "", company=a.get("company", ""), headline=a.get("headline", ""))
        if not plain:
            plain = _fallback_sentences(clean_body(a.get("body", "") or ""), headline=a.get("headline", ""))
        text = (
            f"*{a.get('source', '')} | {a.get('company', '')}*\n"
            f"{a.get('headline', '')}\n"
            f"Date: {a.get('date', '')}\n"
            f"{plain}\n"
            f"[View]({a.get('url', '')})"
        )
        try:
            r = requests.post(api_url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
            r.raise_for_status()
            log.info("Telegram alert sent for: %s", a["company"])
        except Exception as e:
            log.error("Telegram send failed: %s", e)


# Main job
# ══════════════════════════════════════════════════════════════════════════════

def run_check():
    # Reset open offer deduplication for this run
    _SEEN_OPEN_OFFER_THIS_RUN.clear()

    t0 = time.time()
    log.info("DeepSeek key present: %s", bool(
        getattr(Config, "DEEPSEEK_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
    ))
    log.info("═══ Starting announcement check ═══")
    cache = load_cache()

    anns, session = fetch_all_nse()
    t1 = time.time()

    unseen = [a for a in anns if a["id"] not in cache]
    log.info("Unseen: %d", len(unseen))

    # Pre-filter: headline or body must contain a keyword
    candidates = [
        a for a in unseen
        if _headline_looks_relevant(a.get("headline", ""))
        or any(kw in (a.get("body", "") or "").lower() for kw in KEYWORDS)
        or any(kw in (a.get("headline", "") or "").lower() for kw in KEYWORDS)
    ]
    log.info("Candidates (pre-filter): %d", len(candidates))
    candidates = enrich_with_pdf(candidates, session)
    t2 = time.time()

    new_relevant = []
    for ann in candidates:
        if is_relevant(ann):
            cache.add(ann["id"])  # mark seen regardless of market cap check
            if passes_market_cap_filter(ann, session):
                new_relevant.append(ann)

    for ann in unseen:
        cache.add(ann["id"])

    log.info("Relevant (post-filter): %d", len(new_relevant))
    t3 = time.time()

    if new_relevant:
        for ann in new_relevant:
            plain = _ai_summarise(ann.get("body", "") or "", company=ann.get("company", ""), headline=ann.get("headline", ""))
            if not plain:
                plain = _fallback_sentences(clean_body(ann.get("body", "") or ""), headline=ann.get("headline", ""))
            save_to_announcements_db(ann, plain)
        send_email(new_relevant)
        send_whatsapp(new_relevant)
        send_telegram(new_relevant)

    save_cache(cache)

    log.info(
        "⏱  fetch: %.1fs | enrich: %.1fs | notify: %.1fs | TOTAL: %.1fs",
        t1 - t0, t2 - t1, time.time() - t3, time.time() - t0,
    )
    log.info("═══ Check complete ═══\n")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", type=int, metavar="DAYS", default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.backfill:
        log.info("Backfill mode: last %d days", args.backfill)
        orig = load_cache()
        CACHE_FILE.rename(CACHE_FILE.with_suffix(".bak")) if CACHE_FILE.exists() else None
        run_check()
        bak = CACHE_FILE.with_suffix(".bak")
        if bak.exists():
            save_cache(orig | load_cache())
            bak.unlink()
        log.info("Backfill complete.")
    elif args.once:
        log.info("Single run")
        run_check()
    else:
        log.info("Starting — runs every hour")
        run_check()
        schedule.every(60).minutes.do(run_check)
        while True:
            schedule.run_pending()
            time.sleep(1)
