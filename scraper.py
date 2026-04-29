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
    r"name\s+change\b"
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

# ── Deduplicate open offer updates: one alert per company per run ──────────────
# Tracks (company, "open_offer") pairs within a single run_check() call
_SEEN_OPEN_OFFER_THIS_RUN: set = set()


# ══════════════════════════════════════════════════════════════════════════════
# Cache
# ══════════════════════════════════════════════════════════════════════════════

CACHE_FILE = Path("seen_ids.json")

def load_cache() -> set:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def save_cache(cache: set):
    CACHE_FILE.write_text(json.dumps(list(cache)))


# ══════════════════════════════════════════════════════════════════════════════
# Letterhead stripper
# ══════════════════════════════════════════════════════════════════════════════

_CONTENT_START_RE = re.compile(
    r"^(pursuant to|we wish to inform|we would like to inform|this is to inform|"
    r"this is in continuation|this is in furtherance|this is with reference|"
    r"in continuation|in furtherance|in compliance with|further to our|"
    r"with reference to our|with reference to the|we are pleased to inform|"
    r"we hereby inform|we refer to our|kindly note that|please be informed|"
    r"the board of directors|the company is pleased|the company has received|"
    r"the company has filed|the company wishes|members of the exchange)",
    re.IGNORECASE,
)
_FOOTER_RE = re.compile(
    r"^(thanking you|yours (faithfully|sincerely|truly)|for and on behalf|"
    r"authoris(ed|ed) signatory|authoriz(ed|ed) signatory|company secretary|"
    r"kindly take the (above|same) on|please take the (above|same) on)",
    re.IGNORECASE,
)

def strip_letterhead(text: str) -> str:
    if not text:
        return text
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _CONTENT_START_RE.match(line.strip()):
            start = i
            break
    if start is not None:
        lines = lines[start:]
    for i, line in enumerate(lines):
        if _FOOTER_RE.match(line.strip()):
            lines = lines[:i]
            break
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines).strip()
    return result if len(result) >= 80 or len(text) <= 200 else text.strip()


# ══════════════════════════════════════════════════════════════════════════════
# Status / action badges
# ══════════════════════════════════════════════════════════════════════════════

_STATUS_PATTERNS = [
    (r"\bhas become effective\b",                          "✅ Effective"),
    (r"\bscheme.{0,40}effective from\b",                   "✅ Effective"),
    (r"\beffective date.{0,30}is\b",                       "✅ Effective"),
    (r"\bstands dissolved\b",                              "✅ Effective"),
    (r"\bnclt.{0,80}(sanctioned|approved|pronounced)\b",   "⚖️ NCLT Approved"),
    (r"\b(sanctioned|approved).{0,60}nclt\b",              "⚖️ NCLT Approved"),
    (r"\bregional director.{0,80}(sanctioned|approved)\b", "⚖️ RD Approved"),
    (r"\b(approved|sanctioned).{0,60}regional director\b", "⚖️ RD Approved"),
    (r"\bno adverse observations\b",                       "📋 NOC Received"),
    (r"\bno objection\b",                                  "📋 NOC Received"),
    (r"\bobservation letter\b",                            "📋 Observation Letter"),
    (r"\bnoc from.{0,30}(rbi|sebi|reserve bank)\b",        "📋 RBI/SEBI NOC"),
    (r"\bcci.{0,60}approv\b",                              "📋 CCI Approved"),
    (r"\bopen offer.{0,60}(triggered|announced|made)\b",   "📢 Open Offer"),
    (r"\bopen offer\b",                                    "📢 Open Offer"),
    (r"\bslump sale\b",                                    "💼 Slump Sale"),
    (r"\bfiled.{0,50}(nclt|tribunal)\b",                   "📁 Filed with NCLT"),
    (r"\bboard.{0,60}approved\b",                          "🏛️ Board Approved"),
    (r"\bin.?principle approval\b",                        "🏛️ In-Principle Approved"),
    (r"\bplan of merger\b",                                "🏛️ Plan Approved"),
    (r"\bextension of timeline\b",                         "⏳ Timeline Extended"),
    (r"\bsuspended\b",                                     "⛔ Trading Suspended"),
]

_ACTION_PATTERNS = [
    (r"\bcomposite scheme\b",      "Composite Scheme"),
    (r"\bdemerger\b|de-merger",    "Demerger"),
    (r"\bspin.?off\b|hive.?off\b", "Spin-off / Hive-off"),
    (r"\bslump sale\b",            "Slump Sale"),
    (r"\bopen offer\b",            "Open Offer"),
    (r"\bamalgamation\b",          "Amalgamation"),
    (r"\bmerger\b",                "Merger"),
    (r"\brestructur",              "Restructuring"),
    (r"\breorgani[sz]",            "Reorganisation"),
]

_DATE_RE = re.compile(
    r"\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
    r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+20\d{2})\b",
    re.IGNORECASE,
)

def _get_status(t: str) -> str:
    for p, l in _STATUS_PATTERNS:
        if re.search(p, t, re.I): return l
    return "📄 Update"

def _get_action(t: str) -> str:
    for p, l in _ACTION_PATTERNS:
        if re.search(p, t, re.I): return l
    return "Scheme"

def _get_best_date(t: str) -> str:
    dates = _DATE_RE.findall(t)
    recent = [d for d in dates if re.search(r"202[5-9]", d)]
    return recent[-1] if recent else (dates[-1] if dates else "")


# ══════════════════════════════════════════════════════════════════════════════
# Body cleaner
# ══════════════════════════════════════════════════════════════════════════════

_NOISE_ONLY_RE = re.compile(
    r"^(tel|fax|ph|email|cin|pan|gst|isin)\s*[:\+]|"
    r"(phiroze jeejeebhoy|bandra.kurla|exchange plaza|dalal street|"
    r"shantigram|vaishno devi|adani corporate|registered office\s*:)|"
    r"(yours (faithfully|sincerely|truly)|for and on behalf|"
    r"authoris.d signatory|authoriz.d signatory|thanking you|"
    r"kindly take.*on record|please take.*on record|"
    r"dear sir|dear madam|members of the exchange)|"
    r"^(we wish to inform|we would like to inform|this is to inform|"
    r"we hereby inform|we are pleased to inform|"
    r"the above information will|this disclosure will|"
    r"copy of the.*available on|available on the website of|"
    r"will also be made available|hosted on.*website)",
    re.IGNORECASE,
)

_SUBSTANCE_RE = re.compile(
    r"(?:limited|ltd|private|pvt|llp|inc)\b|"
    r"(?:rs\.?|inr|₹)\s*[\d,]+|"
    r"\b\d{1,2}[\s\-]\w+[\s\-]20\d{2}\b|"
    r"\b(merger|amalgamation|demerger|spin.off|"
    r"open offer|slump sale|capital reduction|dissolution|"
    r"transferor|transferee|appointed date|effective date|"
    r"share swap|exchange ratio|record date|resulting company|"
    r"vesting|restructur|reorganis|reorganiz)\b",
    re.IGNORECASE,
)

def clean_body(text: str) -> str:
    if not text:
        return ""
    sentences = re.split(r'(?<=[.;])\s+(?=[A-Z\(\"])', text)
    kept = []
    for s in sentences:
        s = s.strip()
        if not s or len(s) < 15:
            continue
        if _SUBSTANCE_RE.search(s):
            kept.append(s)
            continue
        if _NOISE_ONLY_RE.search(s):
            continue
        kept.append(s)
    return " ".join(kept).strip()


# ══════════════════════════════════════════════════════════════════════════════
# AI summary via DeepSeek
# ══════════════════════════════════════════════════════════════════════════════

_AI_MIN_CHARS = 60

_SUMMARY_PROMPT = """\
Summarise this corporate announcement in 2-3 clear, factual sentences.
Rules:
- State what is happening (merger / demerger / amalgamation / scheme / restructuring) and which companies are involved
- Include key milestones completed (e.g. board approval, NCLT sanction, filing, effective date) and the current status
- Mention any pending steps or next actions if mentioned
- Do NOT include addresses, regulatory boilerplate, or courtesy phrases
- Write in plain business English — no "pursuant to", no formal legalese
- Output plain text only, no bullet points, no bold, no headings

COMPANY: {company}
HEADLINE: {headline}

TEXT:
{text}

SUMMARY:"""


def _ai_summarise(raw_body: str, company: str = "", headline: str = "") -> str:
    api_key = (
        getattr(Config, "DEEPSEEK_API_KEY", "")
        or os.getenv("DEEPSEEK_API_KEY", "")
        or ""
    ).strip()
    if not api_key:
        return ""

    cleaned = clean_body(raw_body or "").strip()
    if len(cleaned) >= _AI_MIN_CHARS:
        body = cleaned[:3000]
    elif len((raw_body or "").strip()) >= _AI_MIN_CHARS:
        body = (raw_body or "").strip()[:3000]
    elif headline:
        body = headline
    else:
        return ""

    prompt = _SUMMARY_PROMPT.format(
        company=company or "Unknown",
        headline=headline or "Corporate announcement",
        text=body,
    )
    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "max_tokens": 400,
                "temperature": 0.3,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        r.raise_for_status()
        out = r.json()["choices"][0]["message"]["content"].strip()
        out = re.sub(r"\*\*(.*?)\*\*", r"\1", out)
        out = re.sub(r"^\s*[-*•]\s+", "", out, flags=re.MULTILINE)
        return out
    except Exception as e:
        log.warning("DeepSeek failed for %s: %s", company, e)
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# Fallback: smart one-liner
# ══════════════════════════════════════════════════════════════════════════════

def _fallback_sentences(clean: str, company: str = "", headline: str = "") -> str:
    combined = ((clean or "") + " " + (headline or "")).lower()

    if re.search(r"de.?merger|demerge", combined):         action = "demerger"
    elif re.search(r"spin.?off|hive.?off", combined):      action = "spin-off"
    elif re.search(r"open offer", combined):               action = "open offer"
    elif re.search(r"slump sale", combined):               action = "slump sale"
    elif re.search(r"transfer\s+of\s+(business|undertaking)", combined): action = "business transfer"
    elif re.search(r"amalgamation|amalgamate", combined):  action = "amalgamation"
    elif re.search(r"\bmerger\b|\bmerge\b", combined):     action = "merger"
    elif re.search(r"restructur", combined):               action = "restructuring"
    elif re.search(r"reorgani[sz]", combined):             action = "reorganisation"
    else:                                                   action = "scheme"

    if re.search(r"has become effective|made effective|scheme.*effective", combined):
        status = "effective"
    elif re.search(r"cci.{0,60}approv", combined):
        status = "CCI approved"
    elif re.search(r"nclt.{0,60}(sanction|approv|order|pronounc)", combined):
        status = "NCLT approved"
    elif re.search(r"regional director.{0,60}(sanction|approv)", combined):
        status = "RD approved"
    elif re.search(r"board.{0,60}approv", combined):
        status = "board approved"
    elif re.search(r"no adverse observation|observation letter|no objection", combined):
        status = "NOC received"
    elif re.search(r"filed.{0,30}(nclt|tribunal)", combined):
        status = "filed with NCLT"
    elif re.search(r"in.?principle approval", combined):
        status = "in-principle approved"
    else:
        status = ""

    partner = ""
    for pat in [
        r'(?:amalgamation of|merger of|demerger of|between)\s+"?([A-Z][A-Za-z &()\'\-\.]{3,60}(?:Limited|Ltd\.?|Private|Pvt\.?|LLP|Inc\.?))',
        r'(?:Transferor Compan(?:y|ies)[^"]{0,20}"?)([A-Z][A-Za-z &()\'\-\.]{3,60}(?:Limited|Ltd\.?|Private|Pvt\.?|LLP|Inc\.?))',
        r'(?:with|into|of)\s+([A-Z][A-Za-z &()\'\-\.]{3,60}(?:Limited|Ltd\.?|Private|Pvt\.?|LLP|Inc\.?))',
    ]:
        m = re.search(pat, clean or "")
        if m:
            candidate = m.group(1).strip().rstrip(",(;")
            if company and candidate.lower()[:12] != (company or "")[:12].lower():
                partner = candidate
                break

    c = (company or "").strip()
    parts = [c, action]
    if partner:
        parts += ["with", partner]
    if status:
        parts += ["—", status]

    result = " ".join(parts).strip()
    if result:
        result = result[0].upper() + result[1:]
        if not result.endswith("."):
            result += "."
        return result

    flat = re.sub(r"\s+", " ", (clean or "").replace("\n", " ")).strip()
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"])", flat):
        s = s.strip()
        if len(s) > 40 and not _NOISE_ONLY_RE.search(s):
            return s
    return headline or ""


# ══════════════════════════════════════════════════════════════════════════════
# Summary builders
# ══════════════════════════════════════════════════════════════════════════════

def build_summary(body: str, company: str = "", headline: str = "") -> str:
    combined_raw = (body or "") + " " + (headline or "")
    flat = re.sub(r"\s+", " ", combined_raw.replace("\n", " ")).strip()

    status = _get_status(flat)
    action = _get_action(flat)
    date   = _get_best_date(flat)

    badge_parts = [f"<b>{status}</b>", f"<b>{action}</b>"]
    if date:
        badge_parts.append(f"📅 {date}")
    badge = " &nbsp;·&nbsp; ".join(badge_parts)

    para = _ai_summarise(body, company=company, headline=headline)
    if not para:
        para = _fallback_sentences(clean_body(body), company=company, headline=headline)

    if para:
        para = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            f'{badge}<br>'
            f'<span style="color:#444;font-weight:normal;line-height:1.7">{para}</span>'
        )
    return badge


# ══════════════════════════════════════════════════════════════════════════════
# PDF extraction
# ══════════════════════════════════════════════════════════════════════════════

MIN_BODY_CHARS = 300

def _body_is_weak(text: str) -> bool:
    return len((text or "").strip()) < MIN_BODY_CHARS

def _headline_looks_relevant(headline: str) -> bool:
    if not headline:
        return False
    hl = headline.lower()
    return any(hint in hl for hint in HEADLINE_HINTS)

def _extract_pdf_text(url: str, session: requests.Session) -> str:
    if not url:
        return ""
    is_pdf_ext = url.lower().split("?")[0].endswith(".pdf")
    if not is_pdf_ext:
        try:
            head = session.head(url, timeout=10, allow_redirects=True)
            ct = head.headers.get("Content-Type", "")
            if "pdf" not in ct.lower():
                return ""
        except Exception:
            return ""
    try:
        r = session.get(url, timeout=25, stream=True)
        r.raise_for_status()
        pages_text = []
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages[:6]:
                t = page.extract_text()
                if t:
                    pages_text.append(t.strip())
        return "\n\n".join(pages_text)
    except Exception as e:
        log.debug("PDF extraction failed for %s: %s", url, e)
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# NSE fetch
# ══════════════════════════════════════════════════════════════════════════════

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
    lookback_days = 10 if not cache else 2
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

    # Layer 1: Hard exclude
    if _HARD_EXCLUDE.search(combined):
        log.debug("Skipped (hard-exclude): %s | %s", ann.get("company"), ann.get("headline"))
        return False

    # Layer 2: Must match at least one keyword
    if not any(kw in combined for kw in KEYWORDS):
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
                company=company,
                headline=headline,
            )

        var1 = _wa_var(f"{status} | {action}")
        var2 = _wa_var(f"{company} ({scrip})" if scrip else company)
        var3 = _wa_var(date_str)
        var4 = _wa_var(f"{headline} - {para}" if para else headline, limit=900)
        var5 = _wa_var(url)

        payload = {
            "messaging_product": "whatsapp",
            "type": "template",
            "template": {
                "name": "alerts",
                "language": {"code": "en"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": var1},
                            {"type": "text", "text": var2},
                            {"type": "text", "text": var3},
                            {"type": "text", "text": var4},
                            {"type": "text", "text": var5},
                        ],
                    }
                ],
            },
        }

        for to_num in Config.WHATSAPP_TO:
            to_clean = re.sub(r"[^\d]", "", str(to_num))
            if not to_clean:
                log.warning("WhatsApp: skipping invalid number '%s'", to_num)
                continue
            payload["to"] = to_clean
            try:
                r = requests.post(api_url, headers=headers, json=payload, timeout=15)
                if r.status_code == 200:
                    msg_id = r.json().get("messages", [{}])[0].get("id", "?")
                    log.info("WhatsApp sent to %s: %s (msg_id=%s)", to_clean, company, msg_id)
                else:
                    log.error("WhatsApp FAILED %s → %s: HTTP %d — %s",
                              company, to_clean, r.status_code, r.text)
            except requests.exceptions.Timeout:
                log.error("WhatsApp timeout: %s → %s", company, to_clean)
            except Exception as e:
                log.error("WhatsApp error: %s → %s: %s", company, to_clean, e)


# ══════════════════════════════════════════════════════════════════════════════
# Market Cap filter  (Layer 5 — applied after keyword + relevance filters)
# ══════════════════════════════════════════════════════════════════════════════

MARKET_CAP_MIN_CR: float = getattr(Config, "MARKET_CAP_MIN_CR", 1000)


def get_market_cap_cr(scrip: str, session: requests.Session) -> float | None:
    if not scrip:
        return None
    symbol = scrip.strip().upper()

    # ── Try NSE API first ─────────────────────────────────────────────────
    try:
        old_ref = session.headers.get("Referer", "")
        session.headers["Referer"] = f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}"
        try:
            r = session.get(
                "https://www.nseindia.com/api/quote-equity",
                params={"symbol": symbol}, timeout=15,
            )
            r.raise_for_status()
            data   = r.json()
            issued = (data.get("securityInfo") or {}).get("issuedSize")
            price  = (data.get("priceInfo")    or {}).get("lastPrice")
            if issued and price:
                mcap = (float(issued) * float(price)) / 1e7
                log.debug("MCap NSE %s: ₹%.2f Cr", symbol, mcap)
                return mcap
        finally:
            session.headers["Referer"] = old_ref
    except Exception as e:
        log.debug("NSE mcap failed for %s: %s — trying Yahoo", symbol, e)

    # ── Fallback: Yahoo Finance ───────────────────────────────────────────
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{symbol}.NS")
        info   = ticker.fast_info          # faster than .info
        mcap   = getattr(info, "market_cap", None)
        if mcap:
            mcap_cr = mcap / 1e7           # ₹ → Crore
            log.debug("MCap Yahoo %s: ₹%.2f Cr", symbol, mcap_cr)
            return mcap_cr
    except Exception as e:
        log.debug("Yahoo mcap failed for %s: %s", symbol, e)

    return None

def passes_market_cap_filter(ann: dict, session: requests.Session) -> bool:
    """
    Returns True  → market cap ≥ MARKET_CAP_MIN_CR  (alert allowed)
    Returns True  → market cap data unavailable      (fail-open, don't miss alerts)
    Returns False → market cap < MARKET_CAP_MIN_CR   (alert suppressed)
    """
    if MARKET_CAP_MIN_CR <= 0:
        return True  # filter disabled

    scrip = (ann.get("scrip") or "").strip()
    company = ann.get("company", "?")

    mcap = get_market_cap_cr(scrip, session)

    if mcap is None:
        log.info("Market cap unavailable for %s (%s) — excluding by default", company, scrip)
        return False  # fail-open: exclude rather than risk missing a real alert

    if mcap >= MARKET_CAP_MIN_CR:
        log.info("✅ Market cap OK  : %-12s  ₹%.2f Cr", scrip, mcap)
        return True

    log.info("❌ Market cap LOW : %-12s  ₹%.2f Cr  (min ₹%g Cr — skipped)", scrip, mcap, MARKET_CAP_MIN_CR)
    return False


# ══════════════════════════════════════════════════════════════════════════════
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
        send_email(new_relevant)
        send_whatsapp(new_relevant)

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
