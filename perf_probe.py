import time
import uuid
import statistics


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


class Stats:
    def __init__(self):
        self._d = {}  # name -> [seconds]
        self._counts = {}  # name -> int

    def add(self, name, seconds):
        self._d.setdefault(name, []).append(float(seconds))
        self._counts[name] = self._counts.get(name, 0) + 1

    def summary(self, name):
        vals = self._d.get(name, [])
        if not vals:
            return None
        return {
            "count": len(vals),
            "mean_ms": statistics.fmean(vals) * 1000.0,
            "p50_ms": _pct(vals, 50) * 1000.0,
            "p90_ms": _pct(vals, 90) * 1000.0,
            "p99_ms": _pct(vals, 99) * 1000.0,
            "min_ms": min(vals) * 1000.0,
            "max_ms": max(vals) * 1000.0,
        }

    def dump(self, names):
        for name in names:
            s = self.summary(name)
            if not s:
                print(f"{name}: no samples")
                continue
            print(
                f"{name}: n={s['count']} mean={s['mean_ms']:.1f}ms "
                f"p50={s['p50_ms']:.1f}ms p90={s['p90_ms']:.1f}ms p99={s['p99_ms']:.1f}ms "
                f"min={s['min_ms']:.1f}ms max={s['max_ms']:.1f}ms"
            )


def main():
    import snipe

    asset_id = snipe.ITEMS_TO_SNIPE[0]["asset_id"]

    # Initialize proxy pool like main, but keep output minimal.
    if snipe.PROXY_STRATEGY != "off":
        loaded = snipe.load_proxies(snipe.PROXIES_FILE)
        if loaded:
            snipe.proxy_pool = snipe.ProxyPool(loaded)

    # Set up per-thread context + prewarm caches.
    snipe.set_thread_context()
    snipe.warm_purchase_session()
    if not snipe.get_cached_catalog_csrf():
        snipe.get_catalog_csrf()
    snipe.get_authenticated_user_id()
    snipe.get_collectible_item_id(asset_id)

    # Wrap HTTP calls to capture timing for the specific endpoints we care about.
    stats = Stats()

    sess = snipe.get_thread_session()
    orig_request = sess.request

    def request_wrapped(method, url, **kwargs):
        t0 = time.perf_counter()
        resp = orig_request(method, url, **kwargs)
        dt = time.perf_counter() - t0
        if "/marketplace-sales/v1/item/" in url and url.endswith("/resellers"):
            stats.add("resellers.http", dt)
        elif "/marketplace-sales/v1/item/" in url and url.endswith("/purchase-resale"):
            stats.add("purchase.http.via_thread_session", dt)
        return resp

    sess.request = request_wrapped

    psess = snipe.get_thread_purchase_session()
    orig_prequest = psess.request

    def prequest_wrapped(method, url, **kwargs):
        t0 = time.perf_counter()
        resp = orig_prequest(method, url, **kwargs)
        dt = time.perf_counter() - t0
        if "/marketplace-sales/v1/item/" in url and url.endswith("/purchase-resale"):
            stats.add("purchase.http", dt)
        return resp

    psess.request = prequest_wrapped

    # Sampling plan:
    # - Reseller polling steady-state (includes any internal backoff/sleeps the function does)
    # - End-to-end "detect -> purchase request" but with a safe payload that cannot purchase
    reseller_samples = 250
    e2e_samples = 40
    interval = snipe.POLL_INTERVAL

    print(f"Asset: {asset_id}")
    print(f"Proxy strategy: {snipe.PROXY_STRATEGY} (pool={len(snipe.proxy_pool) if snipe.proxy_pool else 0})")
    print(f"Request timeout: {snipe.REQUEST_TIMEOUT}s, poll interval: {interval}s")
    print("Running measurements...")

    ok = 0
    for i in range(reseller_samples):
        # Predicted rate-limit sleep before the call (sleep_if_rate_limited uses this).
        now = time.time()
        until = getattr(snipe.thread_ctx, "rate_limit_until", 0.0)
        if until and until > now:
            stats.add("resellers.rate_limit_sleep_pred", until - now)

        t0 = time.perf_counter()
        r = snipe.get_first_reseller(asset_id, limit=1)
        t1 = time.perf_counter()
        stats.add("resellers.total", t1 - t0)
        if r:
            ok += 1

        # Keep it similar to your real loop cadence.
        time.sleep(interval)

    print(f"Reseller fetch success rate: {ok}/{reseller_samples} ({(ok/reseller_samples)*100:.1f}%)")

    # End-to-end measurement: reseller fetch + immediate purchase request (dry-run)
    # We use a random instance/product id so the purchase can never succeed.
    ok2 = 0
    for i in range(e2e_samples):
        t0 = time.perf_counter()
        r = snipe.get_first_reseller(asset_id, limit=1)
        t1 = time.perf_counter()
        stats.add("e2e.resellers.total", t1 - t0)
        if not r:
            time.sleep(interval)
            continue
        ok2 += 1

        # Use real collectible id (cached) and authenticated context, but invalid listing ids.
        product_id, instance_id, price, seller_id, seller_type = r
        fake_instance = str(uuid.uuid4())
        fake_product = str(uuid.uuid4())

        t2 = time.perf_counter()
        _ = snipe.buy_collectible(
            asset_id,
            fake_product,
            fake_instance,
            1,  # any positive int; should fail due to invalid listing ids
            "0",
            "User",
        )
        t3 = time.perf_counter()
        stats.add("e2e.purchase.total", t3 - t2)
        stats.add("e2e.total", t3 - t0)

        time.sleep(interval)

    print(f"E2E cycles with reseller data: {ok2}/{e2e_samples} ({(ok2/e2e_samples)*100:.1f}%)")
    print("")

    stats.dump(
        [
            "resellers.rate_limit_sleep_pred",
            "resellers.http",
            "resellers.total",
            "purchase.http",
            "e2e.resellers.total",
            "e2e.purchase.total",
            "e2e.total",
        ]
    )


if __name__ == "__main__":
    main()

