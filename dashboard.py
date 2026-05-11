"""
Corporate Announcements Dashboard
- Home : live NSE fetch, last 30 days, filtered (merger/demerger/scheme)
- Company page : full history from 2005–now fetched in parallel year chunks
- Search : NSE live autocomplete (all listed companies)

Run: python dashboard.py 8090
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache: dict = {}

def _get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < entry["ttl"]:
        return entry["data"]
    return None

def _set(key, data, ttl=1800):
    _cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}


# ── NSE session ───────────────────────────────────────────────────────────────
_nse: dict = {"s": None, "at": 0}

def nse_session() -> requests.Session:
    if _nse["s"] and time.time() - _nse["at"] < 600:
        return _nse["s"]
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
    })
    try:
        s.get("https://www.nseindia.com/", timeout=12)
        time.sleep(0.4)
    except Exception:
        pass
    _nse["s"] = s
    _nse["at"] = time.time()
    return s


# ══════════════════════════════════════════════════════════════════════════════
# Filter logic  (mirrors scraper.py)
# ══════════════════════════════════════════════════════════════════════════════

KEYWORDS = [
    "merger", "amalgamation", "amalgamate",
    "demerger", "de-merger", "demerge",
    "scheme of arrangement", "scheme of amalgamation",
    "scheme of demerger", "scheme of merger",
    "scheme of reconstruction", "composite scheme",
    "arrangement between", "arrangement amongst",
    "draft scheme", "proposed scheme", "revised scheme",
    "scheme approved", "scheme sanctioned",
    "spin-off", "spinoff", "spin off",
    "hive off", "hive-off",
    "slump sale", "business transfer",
    "transfer of business", "transfer of undertaking",
    "nclt", "appointed date",
    "resulting company", "transferor company", "transferee company",
    "share exchange ratio", "swap ratio",
    "restructuring", "reorganisation", "reorganization",
    "open offer",
]

_HARD_EXCLUDE = re.compile(
    r"(?:disclosure under sebi takeover|"
    r"sebi\s*\(substantial acquisition|"
    r"pledg|encumbr|inter.?se transfer|"
    r"stock\s+split|share\s+split|sub.?division\s+of\s+(?:equity|shares?)|"
    r"acquisition\s+of\s+(?:shares?|equity|stake)|"
    r"(?:quarterly|q[1-4]|half.?year|annual)\s+(?:results?|financial)|"
    r"standalone\s+(?:and\s+consolidated\s+)?financial\s+results?|"
    r"\bdividend\b|buy.?back|rights\s+issue|bonus\s+(?:shares?|issue)|"
    r"postal\s+ballot|scrutinizer|notice\s+of\s+(?:agm|annual\s+general)|"
    r"appointment\s+of\s+(?:independent\s+)?(?:director|auditor|cfo|ceo|md\b)|"
    r"resignation\s+of\s+(?:director|auditor|cfo|ceo|md\b)|"
    r"credit\s+rating|non.?convertible\s+debenture|\bncd\b|"
    r"trading\s+window|insider\s+trading|"
    r"analyst\s+(?:meet|call|conference)|"
    r"investor\s+(?:meet|call|conference)|"
    r"name\s+change\b|change\s+(?:in\s+)?company\s+name)",
    re.IGNORECASE,
)

_OPEN_OFFER_SCHEME = re.compile(
    r"merger|amalgamation|demerger|scheme\s+of|composite\s+scheme|"
    r"restructuring|reorgani[sz]|hive.?off|slump\s+sale",
    re.IGNORECASE,
)


def is_relevant(headline: str, body: str = "") -> bool:
    combined = (headline + " " + body).lower()
    if _HARD_EXCLUDE.search(combined):
        return False
    if not any(kw in combined for kw in KEYWORDS):
        return False
    if "open offer" in combined and not _OPEN_OFFER_SCHEME.search(combined):
        return False
    return True


# ── Badge helpers ─────────────────────────────────────────────────────────────

def action_badge(text: str) -> str:
    t = text.lower()
    if "composite scheme" in t:           return "Composite Scheme"
    if re.search(r"demerger|de-merger", t): return "Demerger"
    if re.search(r"spin.?off", t):        return "Spin-off"
    if re.search(r"hive.?off", t):        return "Hive-off"
    if "slump sale" in t:                 return "Slump Sale"
    if "open offer" in t:                 return "Open Offer"
    if "amalgamation" in t:               return "Amalgamation"
    if "merger" in t:                     return "Merger"
    if "restructur" in t:                 return "Restructuring"
    return "Scheme"


def status_badge(text: str) -> str:
    t = text.lower()
    if re.search(r"effective|stands dissolved", t):              return "✅ Effective"
    if re.search(r"nclt.{0,50}(sanction|approv|pronounc)", t):  return "⚖️ NCLT Approved"
    if re.search(r"regional director.{0,50}(sanction|approv)", t): return "⚖️ RD Approved"
    if re.search(r"board.{0,50}approv", t):                      return "🏛️ Board Approved"
    if re.search(r"no adverse|observation letter", t):           return "📋 NOC Received"
    if re.search(r"filed.{0,30}nclt", t):                        return "📁 Filed NCLT"
    if re.search(r"cci.{0,40}approv", t):                        return "📋 CCI Approved"
    if re.search(r"in.?principle", t):                           return "🏛️ In-Principle"
    return "📄 Update"


def card_color(text: str) -> str:
    t = text.lower()
    if re.search(r"effective|stands dissolved", t): return "#2dc653"
    if re.search(r"demerger|de-merger", t):         return "#e040fb"
    if "amalgamation" in t:                         return "#06d6a0"
    if re.search(r"spin.?off|hive.?off", t):        return "#ff6b6b"
    if "slump sale" in t:                           return "#f9a825"
    if "merger" in t:                               return "#00b4d8"
    return "#4361ee"


# ══════════════════════════════════════════════════════════════════════════════
# NSE fetch helpers
# ══════════════════════════════════════════════════════════════════════════════

def _nse_row_to_ann(row: dict, source: str = "NSE") -> dict:
    return {
        "id":       f"NSE_{row.get('seq_id', '')}",
        "company":  row.get("sm_name") or row.get("symbol", "Unknown"),
        "scrip":    row.get("symbol", ""),
        "headline": (row.get("desc") or "").strip(),
        "body":     (row.get("attchmntText") or "").strip(),
        "date":     (row.get("an_dt") or "").strip(),
        "url":      (row.get("attchmntFile") or "").strip(),
        "source":   source,
    }


def _parse_date(s: str) -> datetime | None:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip()[:20], fmt)
        except ValueError:
            pass
    m = re.search(r"\d{2}-[A-Za-z]{3}-\d{4}", s)
    if m:
        try:
            return datetime.strptime(m.group(), "%d-%b-%Y")
        except Exception:
            pass
    return None


def _fetch_range(index: str, from_date: str, to_date: str,
                 symbol: str, session: requests.Session) -> list[dict]:
    params: dict = {
        "index":     index,
        "from_date": from_date,
        "to_date":   to_date,
    }
    if symbol:
        params["symbol"] = symbol
    try:
        r = session.get(
            "https://www.nseindia.com/api/corporate-announcements",
            params=params, timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            # If symbol param is ignored server-side, filter locally
            if symbol:
                data = [row for row in data
                        if row.get("symbol", "").upper() == symbol.upper()]
            return data
    except Exception:
        pass
    return []


# ── JSON DB fallback (populated by scraper.py) ────────────────────────────────

_JSON_DB = Path("announcements.json")
_GITHUB_RAW = (
    "https://raw.githubusercontent.com/Harshadapadma/stock-alerts/main/announcements.json"
)

def _load_from_json_db(days: int = 30) -> list[dict]:
    # Try local file first (works locally), then fetch from GitHub (works on cloud)
    records = None
    if _JSON_DB.exists():
        try:
            records = json.loads(_JSON_DB.read_text())
        except Exception:
            pass
    if records is None:
        try:
            r = requests.get(_GITHUB_RAW, timeout=10)
            r.raise_for_status()
            records = r.json()
        except Exception:
            return []
    cutoff = datetime.now() - timedelta(days=days)
    today  = datetime.now()
    result = []
    for rec in records:
        ann = {
            "id":       rec.get("id", ""),
            "company":  rec.get("company", "Unknown"),
            "scrip":    rec.get("scrip", ""),
            "headline": rec.get("headline", ""),
            "body":     rec.get("summary", ""),
            "date":     rec.get("date", ""),
            "url":      rec.get("url", ""),
            "source":   rec.get("source", "NSE"),
        }
        d = _parse_date(ann["date"])
        if d and d < cutoff:
            continue
        combined    = ann["headline"] + " " + ann["body"]
        ann["action"] = action_badge(combined)
        ann["status"] = status_badge(combined)
        ann["color"]  = card_color(combined)
        ann["year"]   = d.year if d else today.year
        result.append(ann)
    result.sort(key=lambda x: _parse_date(x["date"]) or datetime.min, reverse=True)
    return result


# ── Recent (home page) ────────────────────────────────────────────────────────

def fetch_recent_filtered(days: int = 30) -> list[dict]:
    cached = _get("recent")
    if cached is not None:
        return cached

    session = nse_session()
    today   = datetime.now()
    from_dt = (today - timedelta(days=days)).strftime("%d-%m-%Y")
    to_dt   = today.strftime("%d-%m-%Y")

    raw: list[dict] = []
    for index in ("equities", "sme"):
        raw.extend(_fetch_range(index, from_dt, to_dt, "", session))
        time.sleep(0.3)

    if not raw:
        # NSE blocked (common on cloud IPs) — serve from scraper's saved JSON
        result = _load_from_json_db(days)
        _set("recent", result, ttl=300)
        return result

    seen: set = set()
    result: list[dict] = []
    for row in raw:
        sid = str(row.get("seq_id", ""))
        if sid in seen:
            continue
        seen.add(sid)
        ann = _nse_row_to_ann(row)
        if is_relevant(ann["headline"], ann["body"]):
            combined = ann["headline"] + " " + ann["body"]
            ann["action"] = action_badge(combined)
            ann["status"] = status_badge(combined)
            ann["color"]  = card_color(combined)
            d = _parse_date(ann["date"])
            ann["year"]   = d.year if d else today.year
            result.append(ann)

    result.sort(
        key=lambda x: _parse_date(x["date"]) or datetime.min,
        reverse=True,
    )
    _set("recent", result, ttl=1800)
    return result


# ── Company history (company page) ────────────────────────────────────────────

def fetch_company_history(symbol: str) -> list[dict]:
    key = f"hist_{symbol.upper()}"
    cached = _get(key)
    if cached is not None:
        return cached

    session = nse_session()
    today   = datetime.now()

    # Build 2-year chunks from 2005 to today
    ranges: list[tuple] = []
    yr = 2005
    while yr <= today.year:
        fr = datetime(yr, 1, 1)
        to = min(datetime(yr + 2, 1, 1) - timedelta(days=1), today)
        ranges.append((fr.strftime("%d-%m-%Y"), to.strftime("%d-%m-%Y")))
        yr += 2

    all_rows: list[dict] = []
    seen: set = set()

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_fetch_range, "equities", fr, to, symbol, session): (fr, to)
            for fr, to in ranges
        }
        for f in as_completed(futures):
            try:
                for row in f.result():
                    sid = str(row.get("seq_id", ""))
                    if sid and sid not in seen:
                        seen.add(sid)
                        all_rows.append(row)
            except Exception:
                pass

    result: list[dict] = []
    for row in all_rows:
        ann = _nse_row_to_ann(row)
        if not is_relevant(ann["headline"], ann["body"]):
            continue
        combined    = ann["headline"] + " " + ann["body"]
        ann["action"] = action_badge(combined)
        ann["status"] = status_badge(combined)
        ann["color"]  = card_color(combined)
        d = _parse_date(ann["date"])
        ann["year"] = d.year if d else today.year
        result.append(ann)

    result.sort(key=lambda x: _parse_date(x["date"]) or datetime.min, reverse=True)
    _set(key, result, ttl=3600)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Shared CSS + JS
# ══════════════════════════════════════════════════════════════════════════════

SHARED_CSS = """
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #f0f2f8; color: #1a1a2e; min-height: 100vh; }

/* Header */
header {
  background: linear-gradient(135deg, #0f3460, #16213e);
  color: white; padding: 13px 28px;
  display: flex; align-items: center; gap: 16px;
  box-shadow: 0 2px 12px rgba(0,0,0,.3);
  position: sticky; top: 0; z-index: 100;
}
.brand { display: flex; align-items: center; gap: 10px;
         color: white; text-decoration: none; white-space: nowrap; }
.brand h1 { font-size: 1.1rem; font-weight: 700; }
.back-btn { color: rgba(255,255,255,.7); text-decoration: none;
            font-size: .82rem; white-space: nowrap; }
.back-btn:hover { color: white; }

/* Search */
.search-wrap { position: relative; flex: 1; max-width: 460px; margin-left: auto; }
.search-wrap input {
  width: 100%; padding: 8px 16px; border-radius: 8px; border: none;
  font-size: .86rem; background: rgba(255,255,255,.13); color: white; outline: none;
}
.search-wrap input::placeholder { color: rgba(255,255,255,.4); }
.search-wrap input:focus { background: rgba(255,255,255,.22); }

.dropdown {
  position: absolute; top: calc(100% + 6px); left: 0; right: 0;
  background: white; border-radius: 10px;
  box-shadow: 0 8px 32px rgba(0,0,0,.2); z-index: 200;
  overflow: hidden; display: none; max-height: 360px; overflow-y: auto;
}
.dropdown.show { display: block; }
.drop-item {
  padding: 10px 16px; cursor: pointer; border-bottom: 1px solid #f5f5f5;
  display: flex; align-items: center; gap: 10px;
  color: #1a1a2e; text-decoration: none;
}
.drop-item:hover { background: #f4f6ff; }
.drop-sym  { font-size: .7rem; background: #eef1ff; color: #4361ee;
             padding: 2px 8px; border-radius: 12px; font-weight: 700; }
.drop-name { font-size: .84rem; flex: 1; }
.drop-msg  { padding: 12px 16px; color: #aaa; font-size: .82rem; text-align: center; }

/* Main */
main { max-width: 1400px; margin: 0 auto; padding: 28px 20px 60px; }

/* 3-column card grid */
.cards-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
  margin-bottom: 14px;
  align-items: start;
}
.cards-grid .ann-card { margin-bottom: 0; }
@media (max-width: 1000px) { .cards-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 640px)  { .cards-grid { grid-template-columns: 1fr; } }
.page-title { font-size: 1.35rem; font-weight: 800; color: #0f3460; margin-bottom: 3px; }
.page-sub   { color: #999; font-size: .82rem; margin-bottom: 22px; }

/* Cards */
.ann-card {
  background: white; border-radius: 12px; padding: 18px 22px;
  margin-bottom: 14px; border-left: 5px solid #4361ee;
  box-shadow: 0 1px 4px rgba(0,0,0,.07);
  transition: box-shadow .2s, transform .15s;
}
.ann-card:hover { box-shadow: 0 6px 24px rgba(0,0,0,.11); transform: translateY(-1px); }

.card-top { display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 10px; flex-wrap: wrap; gap: 6px; }
.company-link { font-weight: 700; font-size: .94rem; color: #0f3460; text-decoration: none; }
.company-link:hover { color: #4361ee; text-decoration: underline; }
.ann-date { font-size: .74rem; color: #bbb; white-space: nowrap; }

.badges { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.badge  { padding: 2px 10px; border-radius: 20px; font-size: .69rem; font-weight: 600; white-space: nowrap; }
.b-action { background: #eef1ff; color: #4361ee; }
.b-status { background: #e8f5e9; color: #2e7d32; }
.b-source { background: #fff3e0; color: #c36200; }

.ann-headline { font-weight: 600; font-size: .88rem; color: #1a1a2e;
                line-height: 1.45; margin-bottom: 8px; }
.ann-story {
  font-size: .83rem; color: #555; line-height: 1.7;
  background: #f8f9ff; border-radius: 6px; padding: 8px 12px; margin-bottom: 10px;
}
.story-yr { font-weight: 700; color: #4361ee; }

.doc-btn {
  display: inline-block; padding: 5px 14px; background: #4361ee;
  color: white; border-radius: 6px; font-size: .74rem; font-weight: 600;
  text-decoration: none; transition: background .15s;
}
.doc-btn:hover { background: #3451d1; }

/* Timeline (company page) */
.co-header { display: flex; align-items: baseline; gap: 12px;
             margin-bottom: 4px; flex-wrap: wrap; }
.co-title  { font-size: 1.45rem; font-weight: 800; color: #0f3460; }
.co-scrip  { background: #eef1ff; color: #4361ee; padding: 3px 12px;
             border-radius: 20px; font-size: .76rem; font-weight: 700; }
.co-sub    { color: #999; font-size: .82rem; margin-bottom: 26px; }

.stats-bar { display: flex; gap: 12px; margin-bottom: 26px; }
.stat      { background: white; border-radius: 10px; padding: 12px 16px;
             text-align: center; flex: 1; box-shadow: 0 1px 4px rgba(0,0,0,.07); }
.stat-val  { font-size: 1.5rem; font-weight: 800; color: #0f3460; }
.stat-lbl  { font-size: .67rem; color: #bbb; text-transform: uppercase; letter-spacing: .8px; }

.timeline { position: relative; padding-left: 26px; }
.timeline::before { content: ''; position: absolute; left: 7px; top: 14px; bottom: 14px;
                    width: 2px; background: linear-gradient(to bottom,#4361ee44,#e040fb44); }

.year-section { position: relative; margin-bottom: 30px; }
.year-marker  { display: flex; align-items: center; gap: 14px; margin-bottom: 12px; position: relative; }
.year-marker::before {
  content: ''; position: absolute; left: -26px;
  width: 11px; height: 11px; border-radius: 50%;
  background: #4361ee; border: 2.5px solid white;
  box-shadow: 0 0 0 2.5px #4361ee; top: 4px;
}
.year-badge { background: #0f3460; color: white; padding: 4px 18px;
              border-radius: 20px; font-size: .95rem; font-weight: 800; letter-spacing: 1px; }
.yr-line    { flex: 1; height: 1px; background: #e5e7eb; }

/* Filter bar */
.filter-bar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; align-items: center; }
.filter-label { font-size: .72rem; font-weight: 700; color: #aaa;
                text-transform: uppercase; letter-spacing: .8px; margin-right: 2px; }
.fc {
  padding: 5px 14px; border-radius: 20px; font-size: .75rem; font-weight: 600;
  cursor: pointer; border: 1.5px solid transparent; transition: all .15s; white-space: nowrap;
  user-select: none;
}
.fc.all   { background: #0f3460; color: white; border-color: #0f3460; }
.fc.type-action { background: #eef1ff; color: #4361ee; border-color: #c7d2fe; }
.fc.type-status { background: #e8f5e9; color: #2e7d32; border-color: #a5d6a7; }
.fc.active { box-shadow: 0 0 0 2px #4361ee; }
.fc.type-action.active { background: #4361ee; color: white; border-color: #4361ee; }
.fc.type-status.active { background: #2e7d32; color: white; border-color: #2e7d32; }
.fc-count { font-size: .68rem; margin-left: 4px; opacity: .75; }

/* Date range row */
.date-range-bar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-bottom: 20px; background: white; border-radius: 10px;
  padding: 10px 16px; box-shadow: 0 1px 4px rgba(0,0,0,.07);
}
.dr-label { font-size: .72rem; font-weight: 700; color: #aaa;
            text-transform: uppercase; letter-spacing: .8px; }
.dr-input {
  padding: 5px 10px; border: 1.5px solid #dde; border-radius: 7px;
  font-size: .82rem; color: #1a1a2e; outline: none; background: #fafafa;
  cursor: pointer;
}
.dr-input:focus { border-color: #4361ee; background: white; }
.dr-sep { color: #bbb; font-size: .8rem; }
.dr-apply {
  padding: 5px 16px; background: #4361ee; color: white; border: none;
  border-radius: 7px; font-size: .78rem; font-weight: 600; cursor: pointer;
  transition: background .15s;
}
.dr-apply:hover { background: #3451d1; }
.dr-clear {
  padding: 5px 12px; background: #f5f5f5; color: #888; border: none;
  border-radius: 7px; font-size: .78rem; cursor: pointer;
}
.dr-clear:hover { background: #ececec; }
.dr-hint { font-size: .72rem; color: #bbb; margin-left: 4px; }

.no-match { text-align: center; color: #bbb; padding: 32px; font-size: .88rem;
            background: white; border-radius: 10px; }

/* Loading + empty */
.loader {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 70px 20px; gap: 16px; color: #aaa;
}
.spinner {
  width: 38px; height: 38px; border: 3px solid #eee;
  border-top-color: #4361ee; border-radius: 50%;
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.empty { text-align: center; color: #ccc; padding: 60px 20px;
         background: white; border-radius: 12px; font-size: .9rem; }
</style>
"""

FILTER_JS = """
<script>
/* ── Filter state ── */
const ACTION_FILTERS = [
  'Merger','Demerger','Amalgamation','Composite Scheme',
  'Spin-off','Hive-off','Slump Sale','Open Offer','Restructuring','Scheme'
];
const STATUS_FILTERS = [
  '✅ Effective','⚖️ NCLT Approved','⚖️ RD Approved',
  '🏛️ Board Approved','📋 NOC Received','📁 Filed NCLT',
  '📋 CCI Approved','🏛️ In-Principle','📄 Update'
];

let _activeFilters = new Set();
let _dateFrom = null;   // Date object or null
let _dateTo   = null;

/* ── Parse the date string stored in data-date attr ── */
function _parseCardDate(str) {
  if (!str) return null;
  // formats: "15-Jan-2025 00:00:00", "15-Jan-2025", "2025-01-15"
  const m = str.match(/(\d{2})-([A-Za-z]{3})-(\d{4})/);
  if (m) return new Date(`${m[2]} ${m[1]} ${m[3]}`);
  const d = new Date(str);
  return isNaN(d) ? null : d;
}

/* ── Build filter chips + date range row ── */
function buildFilterBar(containerId, cards) {
  const container = document.getElementById(containerId);
  if (!container) return;

  // derive min/max dates from cards
  let minDate = null, maxDate = null;
  cards.forEach(c => {
    const d = _parseCardDate(c.dataset.date);
    if (!d) return;
    if (!minDate || d < minDate) minDate = d;
    if (!maxDate || d > maxDate) maxDate = d;
  });

  const fmt = d => d ? d.toISOString().slice(0,10) : '';

  // Count per filter
  const counts = {};
  cards.forEach(c => {
    const ab = c.querySelector('.b-action');
    const sb = c.querySelector('.b-status');
    if (ab) counts[ab.textContent.trim()] = (counts[ab.textContent.trim()]||0)+1;
    if (sb) counts[sb.textContent.trim()] = (counts[sb.textContent.trim()]||0)+1;
  });

  // Chips row
  let chips = '<span class="filter-label">Filter:</span>';
  chips += `<span class="fc all active" onclick="clearFilters()">All <span class="fc-count">${cards.length}</span></span>`;
  ACTION_FILTERS.forEach(f => {
    if (!counts[f]) return;
    chips += `<span class="fc type-action" data-f="${f}" onclick="toggleFilter(this,'${f}')">${f} <span class="fc-count">${counts[f]}</span></span>`;
  });
  STATUS_FILTERS.forEach(f => {
    if (!counts[f]) return;
    chips += `<span class="fc type-status" data-f="${f}" onclick="toggleFilter(this,'${f}')">${f} <span class="fc-count">${counts[f]}</span></span>`;
  });
  container.innerHTML = chips;

  // Date range row  (inject after filter-bar)
  let drBar = document.getElementById('dateRangeBar');
  if (!drBar) {
    drBar = document.createElement('div');
    drBar.id = 'dateRangeBar';
    drBar.className = 'date-range-bar';
    container.insertAdjacentElement('afterend', drBar);
  }
  drBar.innerHTML = `
    <span class="dr-label">Date Range:</span>
    <input class="dr-input" type="date" id="drFrom"
           value="${fmt(minDate)}" min="${fmt(minDate)}" max="${fmt(maxDate)}">
    <span class="dr-sep">→</span>
    <input class="dr-input" type="date" id="drTo"
           value="${fmt(maxDate)}" min="${fmt(minDate)}" max="${fmt(maxDate)}">
    <button class="dr-apply" onclick="applyDateRange()">Apply</button>
    <button class="dr-clear" onclick="clearDateRange()">Clear</button>
    <span class="dr-hint" id="drHint"></span>`;
}

/* ── Date range actions ── */
function applyDateRange() {
  const f = document.getElementById('drFrom')?.value;
  const t = document.getElementById('drTo')?.value;
  _dateFrom = f ? new Date(f) : null;
  _dateTo   = t ? new Date(t + 'T23:59:59') : null;
  applyFilters();
}

function clearDateRange() {
  _dateFrom = _dateTo = null;
  // reset inputs to original min/max
  const fi = document.getElementById('drFrom');
  const ti = document.getElementById('drTo');
  if (fi) fi.value = fi.min;
  if (ti) ti.value = ti.max;
  document.getElementById('drHint').textContent = '';
  applyFilters();
}

/* ── Chip actions ── */
function toggleFilter(el, value) {
  if (_activeFilters.has(value)) {
    _activeFilters.delete(value);
    el.classList.remove('active');
  } else {
    _activeFilters.add(value);
    el.classList.add('active');
  }
  document.querySelector('.fc.all')?.classList.toggle('active', _activeFilters.size === 0);
  applyFilters();
}

function clearFilters() {
  _activeFilters.clear();
  document.querySelectorAll('.fc').forEach(el => el.classList.remove('active'));
  document.querySelector('.fc.all')?.classList.add('active');
  applyFilters();
}

/* ── Master apply (chips + date range combined) ── */
function applyFilters() {
  const cards = document.querySelectorAll('.ann-card');
  let visible = 0;

  cards.forEach(c => {
    // chip check
    let chipOk = true;
    if (_activeFilters.size > 0) {
      const ab = c.querySelector('.b-action')?.textContent.trim() || '';
      const sb = c.querySelector('.b-status')?.textContent.trim() || '';
      chipOk = _activeFilters.has(ab) || _activeFilters.has(sb);
    }

    // date check
    let dateOk = true;
    if (_dateFrom || _dateTo) {
      const d = _parseCardDate(c.dataset.date);
      if (d) {
        if (_dateFrom && d < _dateFrom) dateOk = false;
        if (_dateTo   && d > _dateTo)   dateOk = false;
      }
    }

    const show = chipOk && dateOk;
    c.style.display = show ? '' : 'none';
    if (show) visible++;
  });

  // Hide empty year sections (company page)
  document.querySelectorAll('.year-section').forEach(sec => {
    const hasVisible = [...sec.querySelectorAll('.ann-card')].some(c => c.style.display !== 'none');
    sec.style.display = hasVisible ? '' : 'none';
  });

  // Update hint count
  const hint = document.getElementById('drHint');
  if (hint) hint.textContent = (_dateFrom || _dateTo) ? `${visible} result${visible!==1?'s':''}` : '';

  // No-match message
  let nm = document.getElementById('no-match');
  if (!nm) {
    nm = document.createElement('div');
    nm.id = 'no-match'; nm.className = 'no-match';
    nm.textContent = 'No announcements match the selected filters.';
    document.querySelector('main')?.appendChild(nm);
  }
  nm.style.display = visible === 0 ? '' : 'none';
}
</script>
"""

SEARCH_JS = """
<script>
let _t = null;
function searchNSE(q) {
  const dd = document.getElementById('dd');
  if (!q || q.length < 2) { dd.classList.remove('show'); return; }
  dd.innerHTML = '<div class="drop-msg">Searching NSE…</div>';
  dd.classList.add('show');
  clearTimeout(_t);
  _t = setTimeout(() => {
    fetch('/api/search?q=' + encodeURIComponent(q))
      .then(r => r.json())
      .then(data => {
        if (!data.length) { dd.innerHTML = '<div class="drop-msg">No results</div>'; return; }
        dd.innerHTML = data.map(d =>
          `<a class="drop-item" href="/company?scrip=${encodeURIComponent(d.symbol)}&name=${encodeURIComponent(d.name)}">
             <span class="drop-sym">${d.symbol||'—'}</span>
             <span class="drop-name">${d.name}</span>
           </a>`
        ).join('');
      })
      .catch(() => { dd.innerHTML = '<div class="drop-msg">Search unavailable</div>'; });
  }, 280);
}
document.addEventListener('click', e => {
  if (!e.target.closest('.search-wrap')) document.getElementById('dd').classList.remove('show');
});
</script>
"""

HEADER = """
<header>
  {back}
  <a class="brand" href="/">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
    </svg>
    <h1>Corporate Announcements</h1>
  </a>
  <div class="search-wrap">
    <input type="text" placeholder="Search any NSE company or symbol…"
           oninput="searchNSE(this.value)" autocomplete="off">
    <div class="dropdown" id="dd"></div>
  </div>
</header>
"""


def _header(back=False):
    b = '<a class="back-btn" href="/">← Home</a>' if back else ""
    return HEADER.replace("{back}", b)


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

HOME_TMPL = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Corporate Announcements</title>""" + SHARED_CSS + """</head><body>
""" + _header() + """
<main>
  <div class="page-title">Recent Announcements</div>
  <div class="page-sub" id="homeSub">Last 30 days &nbsp;·&nbsp; Mergers, Demergers &amp; Schemes &nbsp;·&nbsp;
    <span style="color:#4361ee">Click a company name to see its full history</span>
  </div>
  <div id="content">
    <div class="loader">
      <div class="spinner"></div>
      <div>Fetching latest announcements from NSE…</div>
    </div>
  </div>
</main>

<script>
fetch('/api/recent')
  .then(r => r.json())
  .then(data => renderHome(data))
  .catch(() => {
    document.getElementById('content').innerHTML =
      '<div class="empty">Could not fetch data from NSE. Please try again in a moment.</div>';
  });

function cardColorHome(text) {
  const t = text.toLowerCase();
  if (/effective|stands dissolved/.test(t)) return '#2dc653';
  if (/demerger|de-merger/.test(t))         return '#e040fb';
  if (/amalgamation/.test(t))               return '#06d6a0';
  if (/spin.?off|hive.?off/.test(t))        return '#ff6b6b';
  if (/slump sale/.test(t))                 return '#f9a825';
  if (/merger/.test(t))                     return '#00b4d8';
  return '#4361ee';
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function renderHome(anns) {
  const sub = document.getElementById('homeSub');
  if (!anns.length) {
    sub.innerHTML = 'Last 30 days &nbsp;·&nbsp; No filtered announcements found.';
    document.getElementById('content').innerHTML =
      '<div class="empty">No merger, demerger or scheme announcements in the last 30 days.<br><span style="font-size:.8rem;color:#bbb">NSE may be slow — try refreshing in a moment.</span></div>';
    return;
  }

  sub.innerHTML = `Last 30 days &nbsp;·&nbsp; Mergers, Demergers &amp; Schemes &nbsp;·&nbsp;
    ${anns.length} result${anns.length!==1?'s':''} &nbsp;·&nbsp;
    <span style="color:#4361ee">Click a company name to see its full history</span>`;

  let html = '<div class="filter-bar" id="filterBar"></div><div id="cardWrap" class="cards-grid">';
  anns.forEach(a => {
    const combined = (a.headline||'') + ' ' + (a.body||'');
    const color = cardColorHome(combined);
    const docBtn = a.url ? `<a class="doc-btn" href="${esc(a.url)}" target="_blank">Open Document →</a>` : '';
    const scrip  = a.scrip ? `<span style="color:#aaa;font-weight:400;font-size:.78rem">&nbsp;(${esc(a.scrip)})</span>` : '';
    html += `
      <div class="ann-card" style="border-left-color:${color}" data-date="${esc(a.date||'')}">
        <div class="card-top">
          <a class="company-link" href="/company?scrip=${encodeURIComponent(a.scrip||'')}&name=${encodeURIComponent(a.company||'')}">
            ${esc(a.company||'')}${scrip}
          </a>
          <span class="ann-date">${esc(a.date||'')}</span>
        </div>
        <div class="badges">
          <span class="badge b-action">${esc(a.action||'')}</span>
          <span class="badge b-status">${esc(a.status||'')}</span>
          <span class="badge b-source">${esc(a.source||'NSE')}</span>
        </div>
        <div class="ann-headline">${esc(a.headline||'')}</div>
        ${docBtn}
      </div>`;
  });
  html += '</div>';
  document.getElementById('content').innerHTML = html;
  buildFilterBar('filterBar', [...document.querySelectorAll('.ann-card')]);
}
</script>
""" + FILTER_JS + SEARCH_JS + """
</body></html>"""


@app.route("/")
def home():
    return render_template_string(HOME_TMPL)


# ── Company page — skeleton served immediately, data loaded via JS ────────────

CO_SHELL = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ name }} · Announcements</title>""" + SHARED_CSS + """</head><body>
""" + _header(back=True) + """
<main>
  <div class="co-header">
    <div class="co-title">{{ name }}</div>
    {% if scrip %}<span class="co-scrip">{{ scrip }}</span>{% endif %}
  </div>
  <div class="co-sub">Loading full history from NSE…</div>
  <div id="content">
    <div class="loader">
      <div class="spinner"></div>
      <div>Fetching announcements from 2005 to today — this takes ~15 seconds the first time</div>
    </div>
  </div>
</main>

<script>
function pollCompany(scrip, name, attempt) {
  fetch(`/api/company-history?scrip=${encodeURIComponent(scrip)}&name=${encodeURIComponent(name)}`)
    .then(r => r.json())
    .then(res => {
      if (res.status === 'ready') {
        renderTimeline(res.data);
      } else if (res.status === 'pending' && attempt < 20) {
        document.querySelector('.loader div:last-child').textContent =
          'NSE data is being fetched via GitHub… checking in 10 seconds (attempt ' + attempt + '/20)';
        setTimeout(() => pollCompany(scrip, name, attempt + 1), 10000);
      } else {
        document.getElementById('content').innerHTML =
          '<div class="empty">Could not fetch data. Please try again in a minute.</div>';
      }
    })
    .catch(() => {
      document.getElementById('content').innerHTML =
        '<div class="empty">Could not fetch data from NSE. Please try again.</div>';
    });
}
pollCompany('{{ scrip }}', '{{ name }}', 1);

function cardColor(text) {
  const t = text.toLowerCase();
  if (/effective|stands dissolved/.test(t)) return '#2dc653';
  if (/demerger|de-merger/.test(t))         return '#e040fb';
  if (/amalgamation/.test(t))               return '#06d6a0';
  if (/spin.?off|hive.?off/.test(t))        return '#ff6b6b';
  if (/slump sale/.test(t))                 return '#f9a825';
  if (/merger/.test(t))                     return '#00b4d8';
  return '#4361ee';
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function renderTimeline(anns) {
  const sub = document.querySelector('.co-sub');
  if (!anns.length) {
    sub.textContent = 'No filtered announcements (mergers / demergers / schemes) found.';
    document.getElementById('content').innerHTML =
      '<div class="empty">No merger, demerger or scheme announcements found for this company on NSE.</div>';
    return;
  }

  // group by year
  const byYear = {};
  anns.forEach(a => {
    const y = String(a.year || '?');
    (byYear[y] = byYear[y]||[]).push(a);
  });
  const years = Object.keys(byYear).sort((a,b)=>Number(a)-Number(b));
  const latest = years[years.length-1];
  const span = years.length > 1 ? years[0]+'–'+latest : years[0]||'—';

  sub.textContent = `${anns.length} announcement${anns.length!==1?'s':''} · ${span}`;

  let html = `
    <div class="filter-bar" id="filterBar"></div>
    <div class="stats-bar">
      <div class="stat"><div class="stat-val">${anns.length}</div><div class="stat-lbl">Total</div></div>
      <div class="stat"><div class="stat-val">${years.length}</div><div class="stat-lbl">Year${years.length!==1?'s':''}</div></div>
      <div class="stat"><div class="stat-val">${esc(latest||'—')}</div><div class="stat-lbl">Latest Year</div></div>
    </div>
    <div class="timeline">`;

  years.slice().reverse().forEach(year => {
    html += `<div class="year-section">
      <div class="year-marker">
        <div class="year-badge">${esc(year)}</div>
        <div class="yr-line"></div>
      </div>
      <div class="cards-grid">`;

    byYear[year].forEach(a => {
      const combined = (a.headline||'') + ' ' + (a.body||'');
      const color = cardColor(combined);
      const story = a.body
        ? `<div class="ann-story"><span class="story-yr">In ${esc(year)}</span>, ${esc(a.body.slice(0,280))}${a.body.length>280?'…':''}</div>`
        : '';
      const docBtn = a.url
        ? `<a class="doc-btn" href="${esc(a.url)}" target="_blank">Open Document →</a>` : '';

      html += `
        <div class="ann-card" style="border-left-color:${color}" data-date="${esc(a.date||'')}">
          <div class="card-top">
            <div class="badges" style="margin:0">
              <span class="badge b-action">${esc(a.action)}</span>
              <span class="badge b-status">${esc(a.status)}</span>
              <span class="badge b-source">${esc(a.source||'NSE')}</span>
            </div>
            <span class="ann-date">${esc(a.date||year)}</span>
          </div>
          <div class="ann-headline" style="margin-top:10px">${esc(a.headline)}</div>
          ${story}
          ${docBtn}
        </div>`;
    });
    html += `</div></div>`;
  });

  html += `</div>`;
  document.getElementById('content').innerHTML = html;
  // Build filter chips after cards are in the DOM
  buildFilterBar('filterBar', [...document.querySelectorAll('.ann-card')]);
}
</script>
""" + FILTER_JS + SEARCH_JS + "</body></html>"


@app.route("/company")
def company_page():
    scrip = request.args.get("scrip", "").strip().upper()
    name  = request.args.get("name", scrip).strip()
    return render_template_string(CO_SHELL, scrip=scrip, name=name)


_GITHUB_REPO    = "Harshadapadma/stock-alerts"
_GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
_COMPANIES_FILE = Path("companies.json")
_COMPANIES_RAW  = "https://raw.githubusercontent.com/Harshadapadma/stock-alerts/main/companies.json"
_DATA_RAW       = "https://raw.githubusercontent.com/Harshadapadma/stock-alerts/main/data/{symbol}.json"


def _load_companies() -> dict:
    cached = _get("companies")
    if cached is not None:
        return cached
    data = {}
    if _COMPANIES_FILE.exists():
        try:
            data = json.loads(_COMPANIES_FILE.read_text())
        except Exception:
            pass
    if not data:
        try:
            r = requests.get(_COMPANIES_RAW, timeout=10)
            data = r.json()
        except Exception:
            pass
    _set("companies", data, ttl=86400)
    return data


def _trigger_fetch(symbol: str):
    if not _GITHUB_TOKEN:
        return
    try:
        requests.post(
            f"https://api.github.com/repos/{_GITHUB_REPO}/actions/workflows/fetch_company.yml/dispatches",
            headers={"Authorization": f"token {_GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"},
            json={"ref": "main", "inputs": {"symbol": symbol}},
            timeout=10,
        )
    except Exception:
        pass


def _fetch_company_from_github(symbol: str) -> list[dict] | None:
    url = _DATA_RAW.format(symbol=symbol)
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 404:
            return None
        data = r.json()
        today = datetime.now()
        result = []
        for ann in data.get("announcements", []):
            combined = ann["headline"] + " " + ann.get("body", "")
            ann["action"] = action_badge(combined)
            ann["status"] = status_badge(combined)
            ann["color"]  = card_color(combined)
            d = _parse_date(ann["date"])
            ann["year"]   = d.year if d else today.year
            result.append(ann)
        return result
    except Exception:
        return None


@app.route("/api/company-history")
def api_company_history():
    scrip = request.args.get("scrip", "").strip().upper()
    if not scrip:
        return jsonify({"status": "error", "data": []})

    # 1. Try GitHub cached data first
    cached = _fetch_company_from_github(scrip)
    if cached is not None:
        return jsonify({"status": "ready", "data": cached})

    # 2. Not cached — trigger GitHub Actions to fetch it, try NSE directly while waiting
    _trigger_fetch(scrip)
    anns = fetch_company_history(scrip)
    if anns:
        return jsonify({"status": "ready", "data": anns})

    # 3. NSE also blocked — tell frontend to poll
    return jsonify({"status": "pending", "data": []})


@app.route("/api/recent")
def api_recent():
    return jsonify(fetch_recent_filtered())


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    companies = _load_companies()
    out = []
    # symbol prefix matches first, then name matches
    for sym, name in companies.items():
        if sym.lower().startswith(q):
            out.append({"symbol": sym, "name": name})
    for sym, name in companies.items():
        if q in name.lower() and not sym.lower().startswith(q):
            out.append({"symbol": sym, "name": name})
    return jsonify(out[:15])


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    print(f"\n  Dashboard → http://localhost:{port}\n")
    app.run(debug=False, port=port, host="0.0.0.0")
