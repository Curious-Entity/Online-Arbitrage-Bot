import time
import statistics
from urllib.parse import urlparse, unquote


def _pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return d0 + d1


def _summ(values):
    if not values:
        return None
    return {
        "n": len(values),
        "mean_ms": statistics.fmean(values) * 1000.0,
        "p50_ms": _pct(values, 50) * 1000.0,
        "p90_ms": _pct(values, 90) * 1000.0,
        "p99_ms": _pct(values, 99) * 1000.0,
        "min_ms": min(values) * 1000.0,
        "max_ms": max(values) * 1000.0,
    }


def _url_to_hostport_userpass(proxy_url):
    """
    Convert http://user:pass@host:port into (host, port, user, pass) for file output.
    """
    p = urlparse(proxy_url)
    host = p.hostname
    port = p.port
    user = unquote(p.username) if p.username else None
    pwd = unquote(p.password) if p.password else None
    return host, port, user, pwd


def _mask_proxy(proxy_url):
    host, port, user, pwd = _url_to_hostport_userpass(proxy_url)
    if not host or not port:
        return proxy_url
    if user and pwd:
        return f"{host}:{port}:{user}:{pwd[:2]}***"
    return f"{host}:{port}"


def main():
    import requests
    import snipe
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10, help="Number of fastest proxies to select")
    ap.add_argument("--min-ok-rate", type=float, default=0.80, help="Minimum success rate to consider")
    ap.add_argument("--max-p90-ms", type=float, default=220.0, help="Max p90 latency to consider")
    args = ap.parse_args()

    asset_id = snipe.ITEMS_TO_SNIPE[0]["asset_id"]

    # Resolve collectible id once (no proxy) to keep the benchmark focused on reseller endpoint behavior.
    snipe.set_thread_context()
    snipe.warm_purchase_session()
    if not snipe.get_cached_catalog_csrf():
        snipe.get_catalog_csrf()
    collectible_id = snipe.get_collectible_item_id(asset_id)
    if not collectible_id:
        raise SystemExit(f"Could not resolve collectibleItemId for asset_id={asset_id}")

    url = f"https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resellers"
    headers = {"User-Agent": snipe.USER_AGENT, "Accept": "application/json"}

    proxies = snipe.load_proxies("proxies.txt")
    if not proxies:
        raise SystemExit("No proxies loaded from proxies.txt")

    # One session per proxy to allow connection reuse and fair tail-latency measurement.
    sessions = {}
    for p in proxies:
        s = requests.Session()
        s.trust_env = False
        sessions[p] = s

    rounds = 15  # total samples per proxy (interleaved)
    sleep_between = 0.02
    max_exceptions = 4  # stop spending time on clearly broken proxies

    # Store latencies for successful (200) responses only, and counts for errors/429.
    data = {}
    for p in proxies:
        data[p] = {"ok": [], "http_429": 0, "http_other": 0, "exc": 0}

    print(f"Benchmarking {len(proxies)} proxies against reseller endpoint...")
    print(f"Asset: {asset_id} collectible: {collectible_id}")
    print(f"Samples per proxy: {rounds} (interleaved), timeout={snipe.RESELLER_TIMEOUT}")

    for r in range(rounds):
        for p in proxies:
            if data[p]["exc"] >= max_exceptions:
                time.sleep(sleep_between)
                continue
            sess = sessions[p]
            t0 = time.perf_counter()
            try:
                resp = sess.get(
                    url,
                    params={"limit": 1},
                    headers=headers,
                    proxies={"http": p, "https": p},
                    timeout=snipe.RESELLER_TIMEOUT,
                )
                dt = time.perf_counter() - t0
                if resp.status_code == 200:
                    data[p]["ok"].append(dt)
                elif resp.status_code == 429:
                    data[p]["http_429"] += 1
                else:
                    data[p]["http_other"] += 1
            except requests.exceptions.RequestException:
                data[p]["exc"] += 1
            time.sleep(sleep_between)

    # Compute per-proxy summaries.
    rows = []
    for p in proxies:
        ok = data[p]["ok"]
        summ = _summ(ok)
        total = rounds
        ok_n = len(ok)
        # We do 1 request per round (interleaved), so total attempts == rounds.
        rows.append(
            {
                "proxy": p,
                "mask": _mask_proxy(p),
                "ok_n": ok_n,
                "ok_rate": ok_n / total,
                "http_429": data[p]["http_429"],
                "http_other": data[p]["http_other"],
                "exc": data[p]["exc"],
                "mean_ms": summ["mean_ms"] if summ else 999999.0,
                "p90_ms": summ["p90_ms"] if summ else 999999.0,
                "p99_ms": summ["p99_ms"] if summ else 999999.0,
            }
        )

    # Pick the best credential per host:port (avoid duplicates that don't buy us more IP diversity).
    best_by_hostport = {}
    for row in rows:
        host, port, user, pwd = _url_to_hostport_userpass(row["proxy"])
        if not host or not port:
            continue
        key = f"{host}:{port}"
        prev = best_by_hostport.get(key)
        if prev is None:
            best_by_hostport[key] = row
            continue
        # Prefer higher success rate; then lower p90; then lower p99.
        if (row["ok_rate"], -row["p90_ms"], -row["p99_ms"]) > (prev["ok_rate"], -prev["p90_ms"], -prev["p99_ms"]):
            best_by_hostport[key] = row

    # Exclude proxies that never produced a successful response in this run (often SSL/dead endpoints).
    unique_rows = [r for r in best_by_hostport.values() if r["ok_n"] > 0]

    # Rank proxies for sniping:
    # Keep only reasonably stable + reasonably fast proxies, then optimize for tail latency (p90/p99).
    unique_rows = [r for r in unique_rows if r["ok_rate"] >= args.min_ok_rate]
    unique_rows = [r for r in unique_rows if r["p90_ms"] <= args.max_p90_ms]
    unique_rows.sort(
        key=lambda r: (
            r["p90_ms"],
            r["p99_ms"],
            r["mean_ms"],
            r["http_429"],
            r["exc"],
            -(r["ok_rate"]),
        )
    )

    top_n = max(1, args.top)
    selected = unique_rows[:top_n]
    if len(selected) < top_n:
        print(f"WARNING: only found {len(selected)} unique host:port proxies to select")

    # Write proxies_fast.txt in the same host:port:user:pass format your loader supports.
    out_lines = [
        "# Auto-selected fastest proxies (ranked by stability + tail latency to reseller endpoint).",
        "# Format: host:port:username:password",
        "",
    ]
    for row in selected:
        host, port, user, pwd = _url_to_hostport_userpass(row["proxy"])
        if not (host and port and user and pwd):
            continue
        out_lines.append(f"{host}:{port}:{user}:{pwd}")

    with open("proxies_fast.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines).rstrip() + "\n")

    print("")
    print("Per-proxy results (unique host:port best-credential only):")
    for i, row in enumerate(unique_rows, 1):
        print(
            f"{i:2d}. {row['mask']} ok={row['ok_n']}/{rounds} "
            f"429={row['http_429']} exc={row['exc']} "
            f"p90={row['p90_ms']:.1f}ms p99={row['p99_ms']:.1f}ms mean={row['mean_ms']:.1f}ms"
        )

    print("")
    print("Selected proxies written to proxies_fast.txt:")
    for row in selected:
        print(f"- {row['mask']}  (p90={row['p90_ms']:.1f}ms p99={row['p99_ms']:.1f}ms ok_rate={row['ok_rate']*100:.0f}%)")


if __name__ == "__main__":
    main()
