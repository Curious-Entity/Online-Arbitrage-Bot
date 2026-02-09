#!/usr/bin/env python3
"""
Rate Limit Probe for Roblox marketplace-sales reseller endpoint.

This script helps you estimate the max safe requests-per-second (RPS)
for the reseller endpoint using your own .ROBLOSECURITY cookie.

How it works:
1) Reads .ROBLOSECURITY and first asset_id from snipe.py
2) Resolves collectibleItemId via catalog API
3) Sends short bursts at increasing RPS until HTTP 429 appears
4) Reports the highest burst rate without a 429
5) Suggests a POLL_INTERVAL based on item count

IMPORTANT:
- This is a *probe*. Keep it short to avoid temporary throttling.
- Limits can vary by endpoint, IP, and time of day.
- Always back off if you see 429.
"""

import re
import time
import requests
from pathlib import Path

# ---- Configuration ----
RPS_STEPS = [2, 5, 10, 12, 15, 18, 20]  # requests per second
BURST_SECONDS = 2                       # seconds per step
MAX_TOTAL_REQUESTS = 250                # hard cap
COOL_OFF_SECONDS = 0.5                  # short rest between steps
ITEM_COUNT = 1                          # set to how many items you plan to scan
RESELLER_LIMIT = 10                     # items returned per request (smaller = less payload)


def read_roblosecurity_and_asset_id():
    text = Path("snipe.py").read_text(encoding="utf-8")
    # Match: '.ROBLOSECURITY': '...'
    cookie_match = re.search(r"'\.ROBLOSECURITY'\s*:\s*'([^']+)'", text)
    asset_match = re.search(r"'asset_id'\s*:\s*(\d+)", text)
    if not cookie_match:
        raise SystemExit("ROBLOSECURITY not found in snipe.py")
    if not asset_match:
        raise SystemExit("asset_id not found in snipe.py")
    return cookie_match.group(1), int(asset_match.group(1))


def get_collectible_item_id(session, roblosecurity, asset_id):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://www.roblox.com",
        "Referer": f"https://www.roblox.com/catalog/{asset_id}",
        "Content-Type": "application/json",
    }
    cookies = {".ROBLOSECURITY": roblosecurity}

    # CSRF handshake
    r0 = session.post(
        "https://catalog.roblox.com/v1/catalog/items/details",
        json={"items": []},
        headers=headers,
        cookies=cookies,
        timeout=10,
    )
    token = r0.headers.get("x-csrf-token")
    if token:
        headers["x-csrf-token"] = token

    payload = {"items": [{"id": asset_id, "itemType": "Asset"}]}
    r1 = session.post(
        "https://catalog.roblox.com/v1/catalog/items/details",
        json=payload,
        headers=headers,
        cookies=cookies,
        timeout=10,
    )
    if r1.status_code != 200:
        raise SystemExit(f"catalog items/details failed: {r1.status_code} {r1.text[:200]}")
    data = r1.json()
    return data["data"][0].get("collectibleItemId")


def probe_rate_limit():
    roblosecurity, asset_id = read_roblosecurity_and_asset_id()
    session = requests.Session()
    collectible_id = get_collectible_item_id(session, roblosecurity, asset_id)
    if not collectible_id:
        raise SystemExit("collectibleItemId not found for asset")

    resellers_url = (
        f"https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resellers"
    )

    print("Testing asset_id:", asset_id)
    print("collectibleItemId:", collectible_id)
    print("Endpoint:", resellers_url)
    print("RPS steps:", RPS_STEPS)
    print("Burst seconds:", BURST_SECONDS)
    print("Max requests:", MAX_TOTAL_REQUESTS)
    print("-" * 60)

    results = []
    request_count = 0
    rate_limited = False

    for rps in RPS_STEPS:
        if rate_limited or request_count >= MAX_TOTAL_REQUESTS:
            break
        total = int(rps * BURST_SECONDS)
        interval = 1.0 / rps
        start = time.time()
        status_counts = {}

        for _ in range(total):
            if request_count >= MAX_TOTAL_REQUESTS:
                break
            t0 = time.time()
            resp = session.get(
                resellers_url,
                params={"limit": RESELLER_LIMIT},
                headers={"User-Agent": "Mozilla/5.0"},
                cookies={".ROBLOSECURITY": roblosecurity},
                timeout=10,
            )
            request_count += 1
            code = resp.status_code
            status_counts[code] = status_counts.get(code, 0) + 1
            if code == 429:
                rate_limited = True
                break
            elapsed = time.time() - t0
            sleep_for = max(0.0, interval - elapsed)
            time.sleep(sleep_for)

        elapsed_total = time.time() - start
        results.append((rps, total, elapsed_total, status_counts))
        if rate_limited:
            break
        time.sleep(COOL_OFF_SECONDS)

    print("\n--- Rate Probe Results ---")
    for rps, total, elapsed_total, counts in results:
        print(f"rps={rps} total={total} elapsed={elapsed_total:.2f}s status_counts={counts}")

    # determine safe rps
    safe_rps = None
    for rps, _, _, counts in results:
        if 429 not in counts:
            safe_rps = rps
    if safe_rps:
        # Recommend a conservative rps (80% of last safe step)
        recommended_rps = max(1, int(safe_rps * 0.8))
        poll_interval = max(0.05, ITEM_COUNT / recommended_rps)
        print("\nSuggested safe RPS:", recommended_rps)
        print(f"Suggested POLL_INTERVAL for {ITEM_COUNT} items: {poll_interval:.3f}s")
    else:
        print("\nNo safe RPS found before 429. Try lower steps.")

    if rate_limited:
        print("\nRate limit encountered (HTTP 429). Back off before retrying.")
    else:
        print("\nNo 429 encountered in this probe window.")
    print("Total requests sent:", request_count)


if __name__ == "__main__":
    probe_rate_limit()
