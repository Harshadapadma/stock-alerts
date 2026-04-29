"""
Run this on your machine to see exactly what NSE returns for market cap.

Usage:
    python test_mcap.py              # tests FIVESTAR
    python test_mcap.py RELIANCE     # tests any symbol
"""

import sys, json, time, requests

SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else "FIVESTAR"

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={SYMBOL}",
})

print(f"[1] Warming up session (homepage)...")
session.get("https://www.nseindia.com/", timeout=15)
time.sleep(1)

print(f"[2] Visiting quote page for {SYMBOL}...")
session.get(
    f"https://www.nseindia.com/get-quotes/equity?symbol={SYMBOL}",
    timeout=15,
)
time.sleep(1)

print(f"[3] Calling quote-equity API...")
r = session.get(
    "https://www.nseindia.com/api/quote-equity",
    params={"symbol": SYMBOL},
    timeout=20,
)
print(f"    HTTP status: {r.status_code}")

if r.status_code != 200:
    print("    ERROR:", r.text[:500])
    sys.exit(1)

data = r.json()

print(f"\n{'='*60}")
print(f"  NSE quote-equity response for {SYMBOL}")
print(f"{'='*60}")
print(f"\nTop-level keys: {list(data.keys())}\n")

for section, content in data.items():
    if isinstance(content, dict):
        print(f"  [{section}]")
        for k, v in content.items():
            print(f"    {k}: {v}")
        print()

# ── Specifically probe where market cap lives ─────────────────────────────
print("=" * 60)
print("  Market Cap probe")
print("=" * 60)

checks = [
    ("tradeInfo",    "totalMarketCap"),
    ("tradeInfo",    "ffmc"),
    ("tradeInfo",    "totalTradedValue"),
    ("metadata",     "totalMarketCap"),
    ("metadata",     "marketCap"),
    ("priceInfo",    "marketCap"),
    ("industryInfo", "totalMarketCap"),
]
for section, field in checks:
    val = (data.get(section) or {}).get(field)
    print(f"  data['{section}']['{field}'] = {val}")

# Derived from issued size × price
issued = (data.get("securityInfo") or {}).get("issuedSize")
price  = (data.get("priceInfo")    or {}).get("lastPrice")
if issued and price:
    derived = (float(issued) * float(price)) / 1e7
    print(f"\n  Derived (issuedSize × lastPrice / 1e7) = ₹{derived:,.2f} Cr")
    print(f"  (issuedSize={issued}, lastPrice={price})")
else:
    print(f"\n  Derived: issuedSize={issued}, lastPrice={price} — cannot compute")
