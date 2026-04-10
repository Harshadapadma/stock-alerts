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
# WHAT WE WANT (exhaustive)
# ══════════════════════════════════════════════════════════════════════════════
#   ✅ Mergers, demergers, amalgamations
#   ✅ All scheme-of-arrangement variants
#   ✅ Corporate restructuring / reorganisation / realignment / consolidation
#   ✅ Spin-offs, hive-offs, slump sales, business/undertaking transfers
#   ✅ NCLT orders, scheme approvals, appointed dates, effective dates
#   ✅ Open offers (real takeover bids only)
#
# WHAT WE DO NOT WANT
#   ❌ Stock splits, share splits, sub-division of equity
#   ❌ Acquisitions (share purchases, stake buys)
#   ❌ "takeover" bare keyword — matches SEBI Takeover Regs pledge filings daily
#   ❌ SAST / pledge / encumbrance disclosures
#   ❌ Promoter shareholding changes
#   ❌ Quarterly results, buybacks, dividends, rights issues, bonus shares
#   ❌ Scrutinizer reports, postal ballot, AGM/EGM notices
#   ❌ Auditor / KMP / director appointments / resignations
#   ❌ Credit ratings, NCDs, debentures
# ══════════════════════════════════════════════════════════════════════════════


# ── KEYWORDS ─────────────────────────────────────────────────────────────────
KEYWORDS = [
    # Core M&A verbs
    "merger", "amalgamation", "amalgamate",
    "demerger", "de-merger", "demerge",

    # Scheme phrases
    "scheme of arrangement", "scheme of amalgamation",
    "scheme of demerger", "scheme of merger",
    "scheme of reconstruction",
    "composite scheme", "composite scheme of arrangement",
    "arrangement between", "arrangement amongst",
    "draft scheme", "final scheme", "proposed scheme",
    "revised scheme", "modified scheme",
    "filing of scheme", "scheme approved", "scheme sanctioned",
    "approval of scheme", "sanction of scheme",
    "pursuant to scheme", "under the scheme", "as per scheme",
    "in terms of scheme", "implementation of scheme",
    "effective date of scheme", "effectiveness of scheme",
    "coming into effect", "becomes effective",

    # Restructuring / reorganisation
    "restructuring", "re-structuring",
    "reorganisation", "reorganization",
    "corporate restructuring", "group restructuring",
    "internal restructuring", "business restructuring",
    "consolidation", "realignment", "re-alignment",

    # Spin-off / hive-off
    "spin-off", "spinoff", "spin off",
    "hive off", "hive-off",

    # Business / undertaking transfer
    "slump sale", "business transfer",
    "transfer of business", "transfer of undertaking",
    "undertaking transfer", "transfer of division",
    "sale of undertaking", "undertaking sale",
    "transfer of assets", "transfer of liabilities",
    "transfer and vesting", "vesting of undertaking",
    "vesting of business", "assets and liabilities transfer",
    "demerged undertaking",

    # NCLT / tribunal
    "nclt", "national company law tribunal",
    "order of nclt", "nclt order",
    "approved by nclt", "sanctioned by nclt",

    # Key scheme milestones / roles
    "appointed date", "effective date",
    "record date for demerger",
    "resulting company", "transferor company", "transferee company",
    "share exchange ratio", "swap ratio",

    # Open offer (specific — not bare "takeover")
    "open offer",
]

# Lighter hint list used in headline pre-scan
HEADLINE_HINTS = [
    "merger", "demerger", "amalgam", "demerge",
    "scheme of", "composite scheme", "arrangement",
    "restructur", "reorganis", "reorganiz", "realign", "consolidat",
    "spin-off", "spinoff", "spin off", "hive off", "hive-off",
    "slump sale", "open offer",
    "nclt", "transferor", "transferee", "resulting company",
    "appointed date", "effective date",
    "vesting", "demerged undertaking",
]


# ── HARD EXCLUDE — drop immediately, before any keyword check ─────────────────
_HARD_EXCLUDE = re.compile(
    r"(?:"
    # ── SAST / pledge / takeover-regs filings ─────────────────────────────
    r"disclosure under sebi takeover|"
    r"sebi\s*\(substantial acquisition|"
    r"sebi\s*\(substential acquisition|"
    r"substantial acquisition of shares|"
    r"regulation 29\b|regulation 31\b|"
    r"pledg|encumbr|"
    r"inter.?se transfer|creeping acquisition|"
    r"promoter(?:s)?\s+(?:and promoter group\s+)?(?:have|has)\s+(?:acquired|sold|purchased|disposed)|"

    # ── Stock splits / sub-division ────────────────────────────────────────
    r"stock\s+split|share\s+split|"
    r"sub.?division\s+of\s+(?:equity|shares?)|"
    r"sub.?divided\s+(?:equity|shares?)|"
    r"face\s+value\s+(?:split|reduct)|"

    # ── Acquisitions (share purchases, stake buys) ─────────────────────────
    r"acquisition\s+of\s+(?:shares?|equity|stake|securities)|"
    r"acquired?\s+(?:\d[\d,]+\s+)?(?:equity\s+)?shares?|"
    r"purchase\s+of\s+(?:shares?|equity|stake)|"
    r"open\s+market\s+(?:purchase|sale|acquisition)|"
    r"block\s+deal|bulk\s+deal|"
    r"creeping\s+acquisition|"

    # ── Quarterly / financial results ──────────────────────────────────────
    r"(?:quarterly|q[1-4]|half.?year|annual)\s+(?:results?|financial\s+results?)|"
    r"standalone\s+(?:and\s+consolidated\s+)?financial\s+results?|"
    r"unaudited\s+financial\s+results?|"

    # ── Dividends / buybacks / rights / bonus ─────────────────────────────
    r"\bdividend\b|"
    r"buy.?back|"
    r"rights\s+issue|"
    r"bonus\s+(?:shares?|issue)|"

    # ── Meetings that are purely notices (no scheme content) ───────────────
    r"postal\s+ballot\b|"
    r"scrutinizer.{0,30}report|"
    r"notice\s+of\s+(?:agm|egm|annual\s+general|extraordinary\s+general)|"
    r"\bappointment\s+of\s+scrutinizer\b|"

    # ── Auditor / KMP / director changes ──────────────────────────────────
    r"appointment\s+of\s+(?:independent\s+)?(?:director|auditor|cfo|ceo|md|kmp)|"
    r"resignation\s+of\s+(?:director|auditor|cfo|ceo|md|kmp)|"

    # ── Debt / credit instruments ──────────────────────────────────────────
    r"credit\s+rating|"
    r"non.?convertible\s+debenture|"
    r"\bncd\b|"

    # ── Misc noise ─────────────────────────────────────────────────────────
    r"trading\s+window|"
    r"insider\s+trading|"
    r"price\s+sensitive\b|"
    r"analyst\s+(?:meet|call|conference)|"
    r"investor\s+(?:meet|call|conference|presentation|day)|"
    r"earnings\s+call"
    r")",
    re.IGNORECASE,
)

# ══════════════════════════════════════════════════════════════════════════════
# SCORING SYSTEM
# ══════════════════════════════════════════════════════════════════════════════
# Every announcement surviving _HARD_EXCLUDE is scored on headline + body.
# The score drives three decisions:
#
#   score == 0             → skip entirely
#   1 <= score < FETCH_PDF → skip (too weak, not worth a PDF fetch)
#   score >= FETCH_PDF AND body weak AND has URL → fetch PDF, then re-score
#   score >= CANDIDATE     → direct candidate, straight to is_relevant()
#
# PDF fetches are sorted by score descending and capped at PDF_FETCH_CAP.
# ══════════════════════════════════════════════════════════════════════════════

# Tier 1 (+10): unambiguous scheme terms — one hit = strong candidate
_SCORE_T1 = [
    "merger", "amalgamation", "amalgamate",
    "demerger", "de-merger", "demerge",
    "scheme of arrangement", "scheme of amalgamation",
    "scheme of demerger", "scheme of merger",
    "scheme of reconstruction", "composite scheme",
    "nclt", "national company law tribunal",
    "appointed date", "effective date",
    "transferor company", "transferee company", "resulting company",
    "share exchange ratio", "swap ratio",
    "open offer", "slump sale",
    "hive off", "hive-off", "spin-off", "spinoff", "spin off",
    "vesting of undertaking", "vesting of business",
    "transfer and vesting", "demerged undertaking",
    "transferor", "transferee",
]

# Tier 2 (+5): scheme-adjacent — likely relevant in context
_SCORE_T2 = [
    "restructuring", "re-structuring",
    "reorganisation", "reorganization",
    "consolidation", "realignment", "re-alignment",
    "business transfer", "transfer of business",
    "transfer of undertaking", "undertaking transfer",
    "arrangement between", "arrangement amongst",
    "draft scheme", "final scheme", "proposed scheme",
    "revised scheme", "modified scheme",
    "scheme approved", "scheme sanctioned",
    "approval of scheme", "sanction of scheme",
    "filing of scheme", "pursuant to scheme",
    "under the scheme", "as per scheme",
    "in terms of scheme", "implementation of scheme",
    "record date for demerger",
    "assets and liabilities transfer",
    "transfer of assets", "transfer of liabilities",
    "order of nclt", "nclt order",
    "approved by nclt", "sanctioned by nclt",
]

# Tier 3 (+2): weak hints — raise score enough to trigger a PDF fetch
_SCORE_T3 = [
    "scheme",           # alone may mean incentive scheme, but PDF worth checking
    "arrangement",      # alone weak but raises score for PDF fetch
    "restructur",       # partial — catches restructuring/restructured
    "reorgani",         # partial
    "consolidat",       # partial
    "outcome of board", # NSE headline: board approved something — fetch PDF
    "regulation 30",    # NSE Reg 30 material event — may contain scheme news
    "reg 30",
    "material event",
    "material information",
    "updates",          # NSE uses this for scheme progress updates
    "general updates",
    "corporate action",
]

SCORE_FETCH_PDF = 2    # minimum to attempt PDF fetch (weak body)
SCORE_CANDIDATE = 8    # minimum to pass to is_relevant()
PDF_FETCH_CAP   = 50   # max PDF fetches per run (highest-scored first)


def score_ann(headline: str, body: str) -> int:
    """Score an announcement by keyword tier matches."""
    text = ((headline or "") + " " + (body or "")).lower()
    s = 0
    for kw in _SCORE_T1:
        if kw in text:
            s += 10
    for kw in _SCORE_T2:
        if kw in text:
            s += 5
    for kw in _SCORE_T3:
        if kw in text:
            s += 2
    return s

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
    r"transferor\s+compan|transferee\s+compan|"
    r"slump\s+sale|hive.?off|spin.?off|"
    r"restructur|reorgani[sz]|consolidat|"
    r"open\s+offer|nclt|appointed\s+date|effective\s+date",
    re.IGNORECASE,
)


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
    (r"\bcomposite scheme\b",                         "Composite Scheme"),
    (r"\bdemerger\b|de-merger",                       "Demerger"),
    (r"\bspin.?off\b|hive.?off\b",                    "Spin-off / Hive-off"),
    (r"\bslump sale\b",                               "Slump Sale"),
    (r"\btransfer of (?:business|undertaking)\b",     "Business Transfer"),
    (r"\bopen offer\b",                               "Open Offer"),
    (r"\bamalgamation\b",                             "Amalgamation"),
    (r"\bmerger\b",                                   "Merger"),
    (r"\brestructur",                                 "Restructuring"),
    (r"\breorgani[sz]",                               "Reorganisation"),
    (r"\bconsolidat",                                 "Consolidation"),
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
    r"vesting|restructur|reorganis|reorganiz|consolidat)\b",
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
    elif re.search(r"consolidat", combined):               action = "consolidation"
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
    elif re.search(r"open offer.{0,30}(trigger|announc|made)", combined):
        status = "open offer triggered"
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
    lookback_days = 1 if not cache else 2
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
    Layer 1 — Hard exclude: pledge/SAST filings, splits, acquisitions,
                             results, buybacks, dividends, routine notices.
    Layer 2 — Must match at least one scheme/merger/restructuring keyword.
    Layer 3 — Procedural meeting gate: a bare creditor/shareholder meeting
                             notice with NO scheme language anywhere is dropped.
                             If the headline already names a scheme, it passes.
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

    # Layer 3: Procedural meeting gate
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
# WhatsApp via Meta Cloud API — template: alerts (5 variables)
#
# Template body registered in Meta Business Manager:
#   🔍 Stock Market Alert
#   Alert Type: {{1}}
#   Company:    {{2}}
#   Date:       {{3}}
#   Details:    {{4}}
#   More info:  {{5}}
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
# Main job
# ══════════════════════════════════════════════════════════════════════════════

def run_check():
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

    # ── Stage 1: Hard-exclude everything first (fast, zero network calls) ────
    # Kills the high-volume daily noise (results, pledges, splits, etc.)
    # before we waste any time on PDFs.
    not_excluded = []
    for a in unseen:
        combined = (
            (a.get("headline", "") or "") + " " + (a.get("body", "") or "")
        ).lower()
        if not _HARD_EXCLUDE.search(combined):
            not_excluded.append(a)
    log.info("After hard-exclude: %d", len(not_excluded))

    # ── Stage 2: Score every surviving announcement ──────────────────────────
    # Tier 1 (+10): unambiguous scheme terms
    # Tier 2 (+5):  scheme-adjacent terms
    # Tier 3 (+2):  weak hints / common NSE headline patterns
    scored = []
    for a in not_excluded:
        s = score_ann(a.get("headline", ""), a.get("body", ""))
        if s > 0:
            scored.append((s, a))
    scored.sort(key=lambda x: x[0], reverse=True)
    log.info("Non-zero score: %d", len(scored))

    # ── Stage 3: Direct candidates (score >= CANDIDATE, body already rich) ───
    direct = [a for s, a in scored if s >= SCORE_CANDIDATE and not _body_is_weak(a.get("body", "") or "")]

    # ── Stage 4: PDF fetch queue (score >= FETCH_PDF, body weak, has URL) ────
    # Sorted by score descending so we fetch the most promising ones first.
    # Capped at PDF_FETCH_CAP to prevent runaway fetching.
    direct_ids = {a["id"] for a in direct}
    pdf_queue = [
        a for s, a in scored
        if a["id"] not in direct_ids
        and s >= SCORE_FETCH_PDF
        and _body_is_weak(a.get("body", "") or "")
        and a.get("url")
    ][:PDF_FETCH_CAP]
    log.info(
        "Direct candidates: %d | PDF fetch queue: %d",
        len(direct), len(pdf_queue),
    )

    # Enrich PDF queue, then re-score and keep those that now reach CANDIDATE
    pdf_queue = enrich_with_pdf(pdf_queue, session)
    pdf_promoted = [
        a for a in pdf_queue
        if score_ann(a.get("headline", ""), a.get("body", "")) >= SCORE_CANDIDATE
    ]
    log.info("PDF-promoted candidates: %d", len(pdf_promoted))

    # Also enrich any direct candidates whose body is still weak after API text
    candidates = enrich_with_pdf(direct, session) + pdf_promoted
    t2 = time.time()

    new_relevant = []
    for ann in candidates:
        if is_relevant(ann):
            new_relevant.append(ann)
            cache.add(ann["id"])

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
