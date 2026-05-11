"""
Fetches full announcement history for one NSE symbol (2005–today)
and saves to data/{symbol}.json. Called by GitHub Actions.
"""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests

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
    "hive off", "hive-off", "slump sale", "business transfer",
    "transfer of business", "transfer of undertaking",
    "nclt", "appointed date",
    "resulting company", "transferor company", "transferee company",
    "share exchange ratio", "swap ratio",
    "restructuring", "reorganisation", "reorganization", "open offer",
]

_HARD_EXCLUDE = re.compile(
    r"(?:disclosure under sebi takeover|sebi\s*\(substantial acquisition|"
    r"pledg|encumbr|inter.?se transfer|stock\s+split|share\s+split|"
    r"sub.?division\s+of\s+(?:equity|shares?)|"
    r"acquisition\s+of\s+(?:shares?|equity|stake)|"
    r"(?:quarterly|q[1-4]|half.?year|annual)\s+(?:results?|financial)|"
    r"standalone\s+(?:and\s+consolidated\s+)?financial\s+results?|"
    r"\bdividend\b|buy.?back|rights\s+issue|bonus\s+(?:shares?|issue)|"
    r"postal\s+ballot|scrutinizer|notice\s+of\s+(?:agm|annual\s+general)|"
    r"appointment\s+of\s+(?:independent\s+)?(?:director|auditor|cfo|ceo|md\b)|"
    r"resignation\s+of\s+(?:director|auditor|cfo|ceo|md\b)|"
    r"credit\s+rating|non.?convertible\s+debenture|\bncd\b|"
    r"trading\s+window|insider\s+trading|analyst\s+(?:meet|call|conference)|"
    r"investor\s+(?:meet|call|conference)|name\s+change\b|change\s+(?:in\s+)?company\s+name)",
    re.IGNORECASE,
)

def is_relevant(headline, body=""):
    combined = (headline + " " + body).lower()
    if _HARD_EXCLUDE.search(combined):
        return False
    return any(kw in combined for kw in KEYWORDS)

def nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
    })
    s.get("https://www.nseindia.com/", timeout=15)
    time.sleep(1)
    return s

def fetch_range(symbol, from_date, to_date, session):
    try:
        r = session.get(
            "https://www.nseindia.com/api/corporate-announcements",
            params={"index": "equities", "from_date": from_date, "to_date": to_date, "symbol": symbol},
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return [row for row in data if row.get("symbol", "").upper() == symbol.upper()]
    except Exception:
        pass
    return []

def run(symbol):
    symbol = symbol.upper()
    session = nse_session()
    today = datetime.now()

    ranges = []
    yr = 2005
    while yr <= today.year:
        fr = datetime(yr, 1, 1)
        to = min(datetime(yr + 2, 1, 1) - timedelta(days=1), today)
        ranges.append((fr.strftime("%d-%m-%Y"), to.strftime("%d-%m-%Y")))
        yr += 2

    all_rows, seen = [], set()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_range, symbol, fr, to, session): (fr, to) for fr, to in ranges}
        for f in as_completed(futures):
            for row in f.result():
                sid = str(row.get("seq_id", ""))
                if sid and sid not in seen:
                    seen.add(sid)
                    all_rows.append(row)

    result = []
    for row in all_rows:
        headline = (row.get("desc") or "").strip()
        body = (row.get("attchmntText") or "").strip()
        if not is_relevant(headline, body):
            continue
        date_str = (row.get("an_dt") or "").strip()
        result.append({
            "id":       f"NSE_{row.get('seq_id', '')}",
            "company":  row.get("sm_name") or row.get("symbol", ""),
            "scrip":    row.get("symbol", ""),
            "headline": headline,
            "body":     body,
            "date":     date_str,
            "url":      (row.get("attchmntFile") or "").strip(),
            "source":   "NSE",
        })

    result.sort(key=lambda x: x["date"], reverse=True)

    Path("data").mkdir(exist_ok=True)
    out = Path(f"data/{symbol}.json")
    out.write_text(json.dumps({"symbol": symbol, "fetched_at": today.isoformat(), "announcements": result}))
    print(f"Saved {len(result)} announcements for {symbol} → {out}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fetch_company.py SYMBOL")
        sys.exit(1)
    run(sys.argv[1])
