"""
Run this to see exactly what text is being sent to DeepSeek for summarisation.
This helps diagnose why summaries are poor.
"""

import os, io, re, time, requests, pdfplumber
from pathlib import Path

# ── paste your DeepSeek key here just for testing ──
os.environ["DEEPSEEK_API_KEY"] = "your-deepseek-key-here"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.nseindia.com/",
    "Accept": "application/json",
})
session.get("https://www.nseindia.com/", timeout=15)
time.sleep(1)

# Fetch last 2 days of announcements
r = session.get(
    "https://www.nseindia.com/api/corporate-announcements",
    params={"index": "equities", "from_date": "01-04-2026", "to_date": "04-04-2026"},
    timeout=25,
)
rows = r.json()

KEYWORDS = ["merger", "demerger", "amalgamation", "split", "scheme", "demerge", "amalgamate"]
matches = [row for row in rows if any(kw in (row.get("attchmntText","") or "").lower() for kw in KEYWORDS)]

print(f"Total announcements: {len(rows)}")
print(f"Keyword matches: {len(matches)}\n")

for row in matches[:3]:   # show first 3 matches
    url = row.get("attchmntFile", "")
    company = row.get("sm_name", "")
    headline = row.get("desc", "")
    nse_text = row.get("attchmntText", "")

    print("=" * 70)
    print(f"Company  : {company}")
    print(f"Headline : {headline}")
    print(f"URL      : {url}")
    print(f"\n--- NSE attchmntText (what we have without PDF) ---")
    print(nse_text)

    if url and url.endswith(".pdf"):
        print(f"\n--- PDF text (first 2000 chars) ---")
        try:
            pr = session.get(url, timeout=20)
            pages = []
            with pdfplumber.open(io.BytesIO(pr.content)) as pdf:
                for page in pdf.pages[:4]:
                    t = page.extract_text()
                    if t: pages.append(t.strip())
            pdf_text = "\n\n".join(pages)
            print(pdf_text[:2000])
        except Exception as e:
            print(f"PDF failed: {e}")
    print()