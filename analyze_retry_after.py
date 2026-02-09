#!/usr/bin/env python3
"""
Analyze HTTP 429 / Retry-After behavior for Roblox marketplace-sales resellers endpoint.

Goals:
- Resolve collectibleItemId from the first asset_id in snipe.py (uses .ROBLOSECURITY from snipe.py).
- Hit the /resellers endpoint with:
  - direct (no proxy)
  - each proxy from one or more proxy files
- Capture status code, Retry-After, and common rate-limit headers if present.

This script is read-only: it does not attempt purchases.
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

import requests


def _read_snipe_cookie_and_asset_id(path: str = "snipe.py") -> tuple[str, int]:
    text = Path(path).read_text(encoding="utf-8")
    # Match: '.ROBLOSECURITY': '...'
    cookie_match = re.search(r"'\.ROBLOSECURITY'\s*:\s*'([^']+)'", text)
    asset_match = re.search(r"'asset_id'\s*:\s*(\d+)", text)
    if not cookie_match:
        raise SystemExit("ROBLOSECURITY not found in snipe.py")
    if not asset_match:
        raise SystemExit("asset_id not found in snipe.py")
    return cookie_match.group(1), int(asset_match.group(1))


def _retry_after_seconds(resp: requests.Response | None) -> float | None:
    if resp is None:
        return None
    ra = resp.headers.get("Retry-After")
    if not ra:
        return None
    ra = str(ra).strip()
    if not ra:
        return None
    try:
        return max(0.0, float(ra))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(ra)
        ts = dt.timestamp()
        return max(0.0, ts - time.time())
    except Exception:
        return None


def _get_collectible_item_id(session: requests.Session, roblosecurity: str, asset_id: int) -> str:
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
    cid = data["data"][0].get("collectibleItemId")
    if not cid:
        raise SystemExit("collectibleItemId not found for asset")
    return str(cid)


def _load_proxies_from_files(paths: Iterable[str]) -> list[str]:
    proxies: list[str] = []
    for p in paths:
        if not p:
            continue
        path = Path(p)
        if not path.exists():
            raise SystemExit(f"Proxy file not found: {p}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Accept existing scheme URLs; otherwise assume http://
            if "://" not in line:
                # host:port:user:pass
                parts = line.split(":")
                if len(parts) == 4:
                    host, port, user, pw = parts
                    line = f"http://{user}:{pw}@{host}:{port}"
                else:
                    line = f"http://{line}"
            proxies.append(line)
    # Dedup preserving order
    out: list[str] = []
    seen: set[str] = set()
    for x in proxies:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


@dataclass
class AttemptResult:
    label: str
    proxy: str | None
    status: int | None
    retry_after_s: float | None
    headers_subset: dict[str, str]
    body_prefix: str | None
    elapsed_s: float | None
    error: str | None


def _subset_headers(resp: requests.Response | None) -> dict[str, str]:
    if resp is None:
        return {}
    wanted = [
        "Retry-After",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "X-Ratelimit-Limit",
        "X-Ratelimit-Remaining",
        "X-Ratelimit-Reset",
        "X-Roblox-Region",
        "X-Roblox-Edge",
        "Via",
        "Server",
        "CF-Ray",
    ]
    out: dict[str, str] = {}
    for k in wanted:
        v = resp.headers.get(k)
        if v is not None and str(v).strip() != "":
            out[k] = str(v)
    return out


def _try_resellers(
    session: requests.Session,
    url: str,
    *,
    proxy: str | None,
    ua: str | None,
    limit: int,
    timeout_s: float,
) -> AttemptResult:
    headers = {}
    label = "default-UA"
    if ua:
        headers["User-Agent"] = ua
        label = f"UA={ua}"

    req_kwargs = {"timeout": timeout_s}
    if headers:
        req_kwargs["headers"] = headers
    if proxy:
        req_kwargs["proxies"] = {"http": proxy, "https": proxy}

    t0 = time.perf_counter()
    resp = None
    try:
        resp = session.get(url, params={"limit": limit}, **req_kwargs)
        dt = time.perf_counter() - t0
        body_prefix = None
        try:
            txt = resp.text
            body_prefix = txt[:200].replace("\r", "\\r").replace("\n", "\\n")
        except Exception:
            body_prefix = None
        return AttemptResult(
            label=label,
            proxy=proxy,
            status=resp.status_code,
            retry_after_s=_retry_after_seconds(resp),
            headers_subset=_subset_headers(resp),
            body_prefix=body_prefix,
            elapsed_s=dt,
            error=None,
        )
    except requests.exceptions.RequestException as e:
        dt = time.perf_counter() - t0
        return AttemptResult(
            label=label,
            proxy=proxy,
            status=getattr(resp, "status_code", None),
            retry_after_s=_retry_after_seconds(resp),
            headers_subset=_subset_headers(resp),
            body_prefix=None,
            elapsed_s=dt,
            error=f"{type(e).__name__}: {e}",
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", action="append", default=[], help="Proxy file (can be repeated)")
    ap.add_argument("--limit", type=int, default=1, help="resellers?limit=...")
    ap.add_argument("--timeout", type=float, default=8.0, help="request timeout seconds")
    ap.add_argument("--sleep", type=float, default=0.35, help="sleep between attempts")
    ap.add_argument("--ua", action="append", default=[], help="User-Agent to test (can be repeated)")
    ap.add_argument("--include-direct", action="store_true", help="Also test direct (no proxy)")
    args = ap.parse_args()

    roblosecurity, asset_id = _read_snipe_cookie_and_asset_id()
    s = requests.Session()
    s.trust_env = False
    collectible_id = _get_collectible_item_id(s, roblosecurity, asset_id)
    url = f"https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resellers"

    proxies = _load_proxies_from_files(args.proxy_file) if args.proxy_file else []
    uas = args.ua[:] if args.ua else [None]

    print("asset_id:", asset_id)
    print("collectibleItemId:", collectible_id)
    print("endpoint:", url)
    print("proxies:", len(proxies))
    print("UAs:", [u or "(default)" for u in uas])
    print("-" * 60)

    tests: list[tuple[str | None, str]] = []
    if args.include_direct:
        tests.append((None, "direct"))
    for p in proxies:
        # redact credentials in the label, but keep the real URL in the request.
        redacted = p
        redacted = re.sub(r"://([^:@/]+):([^@/]+)@", "://***:***@", redacted)
        tests.append((p, f"proxy {redacted}"))

    results: list[AttemptResult] = []
    for proxy, desc in tests:
        for ua in uas:
            r = _try_resellers(
                s,
                url,
                proxy=proxy,
                ua=ua,
                limit=args.limit,
                timeout_s=args.timeout,
            )
            r.label = f"{desc} {r.label}"
            results.append(r)
            time.sleep(max(0.0, args.sleep))

    # Pretty print
    for r in results:
        ra = f"{r.retry_after_s:.2f}s" if r.retry_after_s is not None else "-"
        dt = f"{r.elapsed_s:.3f}s" if r.elapsed_s is not None else "-"
        status = r.status if r.status is not None else "-"
        err = f" err={r.error}" if r.error else ""
        hdrs = ", ".join([f"{k}={v}" for k, v in r.headers_subset.items()]) if r.headers_subset else ""
        if hdrs:
            hdrs = " " + hdrs
        print(f"{status} ra={ra} dt={dt} {r.label}{hdrs}{err}")

    # quick summary
    codes: dict[int, int] = {}
    for r in results:
        if isinstance(r.status, int):
            codes[r.status] = codes.get(r.status, 0) + 1
    if codes:
        print("-" * 60)
        print("status summary:", dict(sorted(codes.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
