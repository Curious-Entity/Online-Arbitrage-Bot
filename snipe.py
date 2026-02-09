import requests
import time
import sys
import threading
from threading import Lock
import re
import uuid
import random
from urllib.parse import quote
from email.utils import parsedate_to_datetime
from typing import Optional

# CONFIGURATION
# Multiple items to snipe simultaneously
#
# This version uses the marketplace-sales API to fetch reseller listings.
# Provide the classic limited asset id (same id used for Rolimons lookup).
ITEMS_TO_SNIPE = [
    #{
        #'asset_id': 10159600649,
        #'name': '8-Bit Royal Crown',
        #'discount_threshold': 0.5
    #},
    
    {
        'asset_id': 10159600649,  # Used for Rolimons + marketplace-sales lookup
        'name': 'Cheap Limited',
        'discount_threshold': 0.1  # Buy if price <= x% of Rolimons value,
        # Optional per-item overrides:
        # 'roblosecurity': 'your .ROBLOSECURITY cookie for this account',
        # 'proxy': 'http://user:pass@host:port'  # or 'socks5://host:port'
    }
]

# AUTHENTICATION - Copy the .ROBLOSECURITY cookie from your browser's DevTools.
# Other cookies are optional; .ROBLOSECURITY is sufficient for authenticated requests.
COOKIES = {
    '.ROBLOSECURITY': 'CAEaAhADIhsKBGR1aWQSEzEwNjEwOTIxODc3MDU5OTUyNTkoAw.Mh4El1RWg-Gu4v_8cRsKo0Jx_rSSRPf15rURaB-PtBjaiHEXjAj14cXpefLazfZ75Iv3z5NDosv4zWy72u6ZuQaP5LWqgi0YVQqRicGYnEwsiwCjjyOsYZtOvXSJ04ruu5R39Kuzs4MwoJDE5bz-cnmsBWzZV97EQB1AMiaFtyZMB4k_sFbRy2GcGq9ZfEb2G-IlJZeUzFDVoDhdMnAbU-ysRMRh2birGi1f-mAvgy8Au81GXFpy1kR7nUG2A0R19ZJoEhej6D7WkupYt8tl6Xc9UmW0eEZadfv_ta2CLn-Io-8izVET4_9CAUTXwiWnoqe1OhZsPfEXfIfBX2xoFI8SGJRtGoldHFRUKpmUzzZmUuC9nwZl56SZoOI5u3YxkCQehyGhYonvceBsX1bMNiY_j9bzrtZZswHZGO55FCndYkhOCydU7fUTF-meZorBJY0ipESHXQiyKE2ypjR-dBhOJoglSbmE_6D2H5gyFLATg9xuSIszm0Ke2bJgk5a2iUKDqdMigYW6J6h-qfmlXJkOKTUupwsY4ua_dKd--JBKQTtUcCEoT6Hbg7bMl6cy_MrZBUztAA7BIqcuvYkY4VC6p9DbdjcgIJTmmAIWu9zuQ6s_t2oUR9Q5lUCHFO-D09Z3He-pf0BpjTevVUi82p8jjsoegRk9IAZLkuclx3xKe_k9mKzN7uFHc9GoOTz7R4PiGmqsrUfAIFnAS-7GenhV3Voi8BR59RzKXEScM5Z5ROCLJVHgonuu3LC-M2QzRcSAJojnQSz3VOQmF7VENH7AtndxsG1gPUmyRZppTRF1DIgp'
}

USER_AGENT = 'Roblox/WinInet'
POLL_INTERVAL = 0.15  # Check each item every x seconds (increased to reduce rate-limit risk)
HEARTBEAT_EVERY = 10  # Print a heartbeat every N attempts
REQUEST_TIMEOUT = 1  # Seconds per request before timing out
RESELLER_RETRIES = 0  # Number of retries for reseller fetch
RESELLER_BACKOFF_BASE = 0.15  # Base backoff (seconds) between retries
REFRESH_TIME = 86400  # Seconds before refreshing Rolimons value
RESELLER_SEND_COOKIES = False  # Reseller listings are typically public; keep cookies off unless needed.
MIN_SECONDS_BETWEEN_SAME_LISTING_PURCHASE = 1.0  # Avoid hammering the same listing id repeatedly.

# PROXIES
# Create `proxies.txt` (next to this script) with one proxy per line.
# Supported line formats:
# - host:port
# - host:port:username:password
# - user:pass@host:port
# - http://user:pass@host:port (or https:// / socks5:// if you have PySocks installed)
PROXIES_FILE = "proxies_fast.txt"
# - off: do not use proxies unless per-item 'proxy' is set
# - sticky: each thread picks one proxy from the pool and keeps it
# - rotate: each request takes the next proxy from the pool
# - rotate_on_429: stick to a proxy, but swap when a 429 occurs
PROXY_STRATEGY = "rotate_on_429"
PROXY_DEFAULT_SCHEME = "http"
PROXY_429_COOLDOWN_SECONDS = 30
PROXY_ERROR_COOLDOWN_SECONDS = 15
PROXY_MAX_ERROR_STRIKES = 3
PROXY_ERROR_STRIKE_WINDOW_SECONDS = 120
PROXY_SLOW_REQUEST_THRESHOLD_SECONDS = 1
PROXY_SLOW_COOLDOWN_SECONDS = 0.05
PROXY_MIN_ROTATE_INTERVAL_SECONDS = 1.0
# When Roblox returns HTTP 429, respect it by backing off for a bit (helps prevent burning all proxies).
RATE_LIMIT_BACKOFF_BASE_SECONDS = 0.05
RATE_LIMIT_BACKOFF_MAX_SECONDS = 0.10
RATE_LIMIT_BACKOFF_JITTER = 0.01
RESELLER_CONNECT_TIMEOUT = 0.50
RESELLER_READ_TIMEOUT = 0.50
RESELLER_TIMEOUT = (RESELLER_CONNECT_TIMEOUT, RESELLER_READ_TIMEOUT)
# Proxy selection for the high-rate reseller polling path.
# "rotate" spreads load across the pool to reduce 429s at aggressive poll rates.
RESELLER_PROXY_STRATEGY = "rotate_on_429"  # off | sticky | rotate | rotate_on_429
RESELLER_ROTATE_EVERY = 50  # Rotate reseller proxy every N requests when using rotate/rotate_on_429
PURCHASE_USE_PROXY = False  # Purchase is latency-sensitive; direct can be faster than going through a proxy.
PURCHASE_TIMEOUT = 2  # Purchase can be a bit slower than polling; don't cut it off too aggressively.


HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Origin': 'https://www.roblox.com',
    'Referer': 'https://www.roblox.com/'
}

# Thread-safe printing and CSRF management
print_lock = Lock()
catalog_csrf_lock = Lock()
thread_ctx = threading.local()
log_lock = Lock()

def _redact_proxy(p: Optional[str]) -> Optional[str]:
    """Hide proxy credentials in logs (user:pass@...)."""
    if not p:
        return p
    try:
        return re.sub(r"://([^:@/]+):([^@/]+)@", "://***:***@", str(p))
    except Exception:
        return str(p)

# Global CSRF token cache for catalog calls (per account)
catalog_csrf_cache = {}
auth_user_id_cache = {}
auth_user_id_lock = Lock()

# Cache collectibleItemId per asset to reduce catalog calls
collectible_id_cache = {}
collectible_cache_lock = Lock()

# Short-lived balance cache (seconds)
balance_cache = {'value': None, 'last_fetch': 0}
balance_cache_lock = Lock()

# Global flag for graceful shutdown
SHUTDOWN_FLAG = False
shutdown_lock = Lock()

class ProxyPool:
    def __init__(self, proxies):
        self._proxies = list(proxies or [])
        random.shuffle(self._proxies)
        self._idx = 0
        self._lock = Lock()

    def __len__(self):
        return len(self._proxies)

    def next(self):
        with self._lock:
            if not self._proxies:
                return None
            n = len(self._proxies)

            # Prefer proxies that are not dead and not cooling down.
            for _ in range(n):
                p = self._proxies[self._idx % n]
                self._idx += 1
                if (not is_proxy_dead(p)) and (not is_proxy_in_cooldown(p)):
                    return p
            return None


proxy_pool = None
bad_proxies = set()
bad_proxies_lock = Lock()
proxy_rotation_lock = Lock()
rate_limit_stats = {}
rate_limit_lock = Lock()
proxy_cooldowns = {}  # proxy -> unix timestamp when it becomes eligible again
proxy_cooldowns_lock = Lock()
proxy_error_strikes = {}  # proxy -> (strikes, last_ts)
proxy_error_strikes_lock = Lock()

def is_proxy_in_cooldown(proxy):
    if not proxy:
        return False
    with proxy_cooldowns_lock:
        until = proxy_cooldowns.get(proxy)
        if until is None:
            return False
        if time.time() < until:
            return True
        # expired cooldown; clear it
        proxy_cooldowns.pop(proxy, None)
        return False

def is_proxy_bad(proxy):
    if not proxy:
        return False
    with bad_proxies_lock:
        if proxy in bad_proxies:
            return True
    return is_proxy_in_cooldown(proxy)

def is_proxy_dead(proxy):
    if not proxy:
        return False
    with bad_proxies_lock:
        return proxy in bad_proxies

def cooldown_proxy(proxy, seconds, reason=None):
    if not proxy or seconds <= 0:
        return
    until = time.time() + seconds
    with proxy_cooldowns_lock:
        prev = proxy_cooldowns.get(proxy, 0)
        if until <= prev:
            return
        proxy_cooldowns[proxy] = until
    with print_lock:
        msg = f"[PROXY] Cooldown {int(seconds)}s: {_redact_proxy(proxy)}"
        if reason:
            msg += f" ({reason})"
        print(msg)

def mark_proxy_bad(proxy, reason=None):
    if not proxy:
        return
    with bad_proxies_lock:
        if proxy in bad_proxies:
            return
        bad_proxies.add(proxy)
    with print_lock:
        msg = f"[PROXY] Marked bad: {_redact_proxy(proxy)}"
        if reason:
            msg += f" ({reason})"
        print(msg)

def bump_proxy_error_strikes(proxy):
    if not proxy:
        return 0
    now = time.time()
    with proxy_error_strikes_lock:
        strikes, last_ts = proxy_error_strikes.get(proxy, (0, 0.0))
        if now - last_ts > PROXY_ERROR_STRIKE_WINDOW_SECONDS:
            strikes = 0
        strikes += 1
        proxy_error_strikes[proxy] = (strikes, now)
        return strikes

def maybe_mark_proxy_bad(exc):
    # Mark permanently dead only on strong signals; otherwise cooldown with strikes so it can recover.
    proxy = getattr(thread_ctx, "proxy", None)
    if not proxy:
        return
    dead_types = (
        requests.exceptions.ProxyError,
        requests.exceptions.SSLError,
    )
    transient_types = (
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectionError,
    )
    if isinstance(exc, dead_types):
        mark_proxy_bad(proxy, type(exc).__name__)
        # Clear thread proxy so sticky threads can re-pick a working one.
        thread_ctx.proxy = None
        return
    if isinstance(exc, transient_types):
        strikes = bump_proxy_error_strikes(proxy)
        cooldown_proxy(proxy, PROXY_ERROR_COOLDOWN_SECONDS, reason=type(exc).__name__)
        if strikes >= PROXY_MAX_ERROR_STRIKES:
            mark_proxy_bad(proxy, f"{type(exc).__name__} x{strikes}")
        thread_ctx.proxy = None

def rotate_proxy(reason=""):
    """Rotate to a new proxy and return it."""
    global proxy_pool
    if not proxy_pool or len(proxy_pool) == 0:
        return None
    with proxy_rotation_lock:
        old_proxy = getattr(thread_ctx, "proxy", None)
        now = time.time()
        last = getattr(thread_ctx, "last_proxy_rotate_ts", 0.0)
        if now - last < PROXY_MIN_ROTATE_INTERVAL_SECONDS:
            return old_proxy
        new_proxy = proxy_pool.next()
        if new_proxy:
            thread_ctx.proxy = new_proxy
            thread_ctx.last_proxy_rotate_ts = now
        if new_proxy:
            with print_lock:
                msg = f"[PROXY] Rotated proxy to: {_redact_proxy(new_proxy)}"
                if reason:
                    msg += f" ({reason})"
                print(msg)
            return new_proxy or old_proxy

def record_429(context, proxy=None):
    p = proxy if proxy is not None else getattr(thread_ctx, "proxy", None)
    key = p or "none"
    with rate_limit_lock:
        rate_limit_stats[key] = rate_limit_stats.get(key, 0) + 1
        count = rate_limit_stats[key]
    with print_lock:
        print(f"[RATE] 429 {context} proxy={_redact_proxy(p)} count={count}")

def _ratelimit_seconds_from_headers(res):
    """
    Parse common ratelimit headers to a (remaining, reset_seconds) tuple.

    Observed on /resellers:
    - X-RateLimit-Remaining: integer
    - X-RateLimit-Reset: seconds until reset (integer-ish)
    """
    if res is None:
        return (None, None)
    try:
        rem = res.headers.get("X-RateLimit-Remaining") or res.headers.get("X-Ratelimit-Remaining")
        reset = res.headers.get("X-RateLimit-Reset") or res.headers.get("X-Ratelimit-Reset")
    except Exception:
        return (None, None)
    remaining = None
    reset_s = None
    try:
        if rem is not None and str(rem).strip() != "":
            remaining = int(float(str(rem).strip()))
    except Exception:
        remaining = None
    try:
        if reset is not None and str(reset).strip() != "":
            reset_s = max(0.0, float(str(reset).strip()))
    except Exception:
        reset_s = None
    return (remaining, reset_s)

def handle_429(context, proxy_used=None, *, rotate_on_429=True, cooldown_seconds=None):
    p = proxy_used if proxy_used is not None else getattr(thread_ctx, "proxy", None)
    record_429(context, proxy=p)

    # Exponential backoff per thread (resets on success in the call sites).
    n = getattr(thread_ctx, "rate_limit_strikes", 0) + 1
    thread_ctx.rate_limit_strikes = n
    exp = min(6, n)  # cap growth
    backoff = min(RATE_LIMIT_BACKOFF_MAX_SECONDS, RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (exp - 1)))
    backoff *= random.uniform(1.0 - RATE_LIMIT_BACKOFF_JITTER, 1.0 + RATE_LIMIT_BACKOFF_JITTER)
    until = time.time() + backoff
    thread_ctx.rate_limit_until = max(getattr(thread_ctx, "rate_limit_until", 0), until)
    with print_lock:
        print(f"[RATE] Backing off {backoff:.2f}s after 429 ({context})")

    # Prefer server-provided cooldown if available (Retry-After / X-RateLimit-Reset).
    cd = cooldown_seconds if cooldown_seconds is not None else PROXY_429_COOLDOWN_SECONDS
    if cd and cd > 0:
        cooldown_proxy(p, cd, reason="HTTP 429")
    # Some call-sites already rotate every request (e.g. reseller polling). Avoid double-rotating there.
    if rotate_on_429 and PROXY_STRATEGY == "rotate_on_429":
        rotate_proxy("HTTP 429")

def sleep_if_rate_limited():
    until = getattr(thread_ctx, "rate_limit_until", 0)
    now = time.time()
    if until and until > now:
        time.sleep(until - now)

def note_rate_limit_success():
    # On success, reduce strikes so we can ramp back up.
    n = getattr(thread_ctx, "rate_limit_strikes", 0)
    if n > 0:
        thread_ctx.rate_limit_strikes = max(0, n - 1)

def _retry_after_seconds_from_response(res):
    """
    Parse Retry-After header (seconds or HTTP-date) into a non-negative float.
    Returns None if missing/unparseable.
    """
    if res is None:
        return None
    try:
        ra = res.headers.get("Retry-After")
    except Exception:
        return None
    if not ra:
        return None
    ra = str(ra).strip()
    if not ra:
        return None
    # Most commonly it's integer seconds.
    try:
        secs = float(ra)
        return max(0.0, secs)
    except ValueError:
        pass
    # Some servers use an HTTP-date.
    try:
        dt = parsedate_to_datetime(ra)
        if dt is None:
            return None
        # parsedate_to_datetime may return naive datetime; treat as UTC.
        if getattr(dt, "tzinfo", None) is None:
            ts = dt.timestamp()
        else:
            ts = dt.timestamp()
        return max(0.0, ts - time.time())
    except Exception:
        return None

def handle_429_response(context, res, proxy_used=None, *, rotate_on_429=True):
    """
    Convenience wrapper that honors Retry-After (if provided) before delegating
    to the standard 429 handler.
    """
    ra = _retry_after_seconds_from_response(res)
    remaining, reset_s = _ratelimit_seconds_from_headers(res)

    # Prefer explicit Retry-After; fall back to Reset seconds if present.
    server_wait = None
    if ra:
        server_wait = ra
    elif reset_s:
        server_wait = reset_s

    if server_wait:
        until = time.time() + server_wait
        thread_ctx.rate_limit_until = max(getattr(thread_ctx, "rate_limit_until", 0), until)
        with print_lock:
            print(f"[RATE] Retry-After {server_wait:.2f}s ({context})")

    # Cool down the current proxy only as long as needed (helps avoid burning a small pool).
    cooldown_seconds = None
    if server_wait:
        cooldown_seconds = server_wait
    handle_429(
        context,
        proxy_used=proxy_used,
        rotate_on_429=rotate_on_429,
        cooldown_seconds=cooldown_seconds,
    )

def _format_proxy_url(host, port, username=None, password=None, scheme=None):
    scheme = scheme or PROXY_DEFAULT_SCHEME
    if username is None or password is None:
        return f"{scheme}://{host}:{port}"
    user_enc = quote(str(username), safe="")
    pass_enc = quote(str(password), safe="")
    return f"{scheme}://{user_enc}:{pass_enc}@{host}:{port}"

def load_proxies(path):
    """
    Load proxies from a text file, ignoring blank lines and comments.
    Returns a list of proxy URLs like: http://user:pass@host:port
    """
    proxies = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue

                # Full URL already provided.
                if "://" in line:
                    proxies.append(line)
                    continue

                # Common exports:
                # - host:port
                # - host:port:user:pass
                # - user:pass@host:port
                if "@" in line and ":" in line:
                    proxies.append(f"{PROXY_DEFAULT_SCHEME}://{line}")
                    continue

                parts = re.split(r"[\s,:]+", line)
                parts = [p for p in parts if p]
                if len(parts) == 2:
                    host, port = parts
                    proxies.append(_format_proxy_url(host, port))
                elif len(parts) >= 4:
                    host, port, username, password = parts[:4]
                    proxies.append(_format_proxy_url(host, port, username, password))
                else:
                    with print_lock:
                        print(f"[PROXY] Skipping unrecognized proxy line: {line}")
    except FileNotFoundError:
        return []
    except OSError as e:
        with print_lock:
            print(f"[PROXY] Could not read {path}: {e}")
        return []

    # De-duplicate while preserving order.
    seen = set()
    out = []
    for p in proxies:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out

def graceful_shutdown(message=""):
    """Gracefully shut down all snipe threads"""
    global SHUTDOWN_FLAG
    with shutdown_lock:
        SHUTDOWN_FLAG = True
    with print_lock:
        if message:
            print(f"\n{message}")
        print("Stopping sniper gracefully...")
    sys.exit(0)

def input_handler():
    """Listen for user input and gracefully shut down"""
    try:
        while not SHUTDOWN_FLAG:
            user_input = input()
            if user_input.lower() in ['q', 'quit', 'exit', 'stop']:
                graceful_shutdown(f"User typed '{user_input}' - shutting down")
            else:
                with print_lock:
                    print(f"Commands: 'q', 'quit', 'exit', 'stop' to terminate. You typed: {user_input}")
    except EOFError:
        pass  # Ignore EOF errors when input is unavailable
    except KeyboardInterrupt:
        graceful_shutdown("Keyboard interrupt - shutting down")

def get_thread_cookies():
    """Get cookies for the current thread (per-account override if set)."""
    return getattr(thread_ctx, 'cookies', COOKIES)

def get_thread_proxy():
    """Get proxy URL for the current thread (per-item override if set)."""
    proxy_override = getattr(thread_ctx, 'proxy_override', None)
    if proxy_override:
        if not is_proxy_bad(proxy_override):
            thread_ctx.proxy = proxy_override
            return proxy_override
        # If the override is bad, keep returning None rather than falling back to pool silently.
        return None

    proxy = getattr(thread_ctx, 'proxy', None)
    if proxy and not is_proxy_bad(proxy):
        return proxy
    if proxy and is_proxy_bad(proxy):
        thread_ctx.proxy = None

    # Optional global proxy pool.
    global proxy_pool
    if PROXY_STRATEGY == "off" or not proxy_pool or len(proxy_pool) == 0:
        return None

    if PROXY_STRATEGY == "sticky":
        proxy = proxy_pool.next()
        thread_ctx.proxy = proxy
        return proxy

    if PROXY_STRATEGY == "rotate":
        proxy = proxy_pool.next()
        thread_ctx.proxy = proxy
        return proxy
    if PROXY_STRATEGY == "rotate_on_429":
        # Stick to a chosen proxy until a 429 (or error) forces a rotation.
        proxy = getattr(thread_ctx, 'proxy', None)
        if proxy and not is_proxy_bad(proxy):
            return proxy
        proxy = proxy_pool.next()
        thread_ctx.proxy = proxy
        return proxy

    return None

def get_current_proxy():
    """Return the last-selected proxy without mutating selection (useful for logging)."""
    return getattr(thread_ctx, 'proxy', None)

def get_reseller_proxy():
    """Pick a proxy for the high-rate reseller polling path."""
    # Respect explicit per-item proxy override.
    proxy_override = getattr(thread_ctx, 'proxy_override', None)
    if proxy_override:
        if not is_proxy_bad(proxy_override):
            thread_ctx.proxy = proxy_override
            return proxy_override
        return None

    global proxy_pool
    if RESELLER_PROXY_STRATEGY == "off" or not proxy_pool or len(proxy_pool) == 0:
        return None

    if RESELLER_PROXY_STRATEGY == "sticky":
        proxy = getattr(thread_ctx, 'reseller_proxy', None)
        if proxy and not is_proxy_bad(proxy):
            thread_ctx.proxy = proxy
            return proxy
        proxy = proxy_pool.next()
        thread_ctx.reseller_proxy = proxy
        thread_ctx.proxy = proxy
        return proxy

    # rotate / rotate_on_429: stick to a proxy for RESELLER_ROTATE_EVERY requests.
    proxy = getattr(thread_ctx, 'reseller_proxy', None)
    uses = getattr(thread_ctx, 'reseller_proxy_uses', 0)
    rotate_every = max(1, RESELLER_ROTATE_EVERY)
    if proxy and not is_proxy_bad(proxy) and uses < rotate_every:
        thread_ctx.proxy = proxy
        thread_ctx.reseller_proxy_uses = uses + 1
        return proxy

    proxy = proxy_pool.next()
    if not proxy:
        # If everything is cooling down, don't fall back to direct; wait briefly for cooldown to expire.
        with proxy_cooldowns_lock:
            soonest = min(proxy_cooldowns.values(), default=0)
        now = time.time()
        if soonest and soonest > now:
            thread_ctx.rate_limit_until = max(getattr(thread_ctx, "rate_limit_until", 0), soonest)
            sleep_if_rate_limited()
        proxy = proxy_pool.next()
    thread_ctx.reseller_proxy = proxy
    thread_ctx.reseller_proxy_uses = 1 if proxy else 0
    thread_ctx.proxy = proxy
    return proxy

def get_proxy_kwargs():
    """Build proxy kwargs for requests.* calls."""
    proxy = get_thread_proxy()
    if proxy:
        return {'proxies': {'http': proxy, 'https': proxy}}
    return {}

def get_roblox_request_kwargs(include_cookies=True):
    """Build cookies+proxy kwargs for Roblox requests.* calls."""
    kwargs = {}
    if include_cookies:
        kwargs['cookies'] = get_thread_cookies()
    proxy = get_thread_proxy()
    if proxy:
        kwargs['proxies'] = {'http': proxy, 'https': proxy}
    return kwargs

def get_reseller_request_kwargs(include_cookies=False):
    """Build kwargs for the reseller polling call (may use a different proxy strategy)."""
    kwargs = {}
    if include_cookies:
        kwargs['cookies'] = get_thread_cookies()
    proxy = get_reseller_proxy()
    if proxy:
        kwargs['proxies'] = {'http': proxy, 'https': proxy}
    return kwargs

def get_purchase_request_kwargs():
    """Build kwargs for purchase calls. Defaults to direct (no proxy) for lowest latency."""
    kwargs = {'cookies': get_thread_cookies()}
    if PURCHASE_USE_PROXY:
        proxy = get_thread_proxy()
        if proxy:
            kwargs['proxies'] = {'http': proxy, 'https': proxy}
    return kwargs

def get_thread_purchase_session():
    """Get a per-thread session for latency-sensitive purchase calls (typically direct)."""
    if not getattr(thread_ctx, 'purchase_session', None):
        s = requests.Session()
        # Avoid picking up any proxy env vars; we explicitly control proxy usage.
        s.trust_env = False
        thread_ctx.purchase_session = s
    return thread_ctx.purchase_session

def warm_purchase_session():
    """Warm DNS/TCP/TLS for apis.roblox.com on the purchase session."""
    if getattr(thread_ctx, 'purchase_warmed', False):
        return
    try:
        get_thread_purchase_session().get(
            "https://apis.roblox.com/",
            headers={"User-Agent": USER_AGENT},
            timeout=2,
        )
    except requests.exceptions.RequestException:
        pass
    thread_ctx.purchase_warmed = True

def get_cached_catalog_csrf():
    """Return cached CSRF token if present (no network)."""
    roblosecurity = get_thread_cookies().get('.ROBLOSECURITY')
    cache_key = roblosecurity or 'default'
    with catalog_csrf_lock:
        return catalog_csrf_cache.get(cache_key)

def set_thread_context(cookies_override=None, proxy=None):
    """Set per-thread cookies/proxy for this item."""
    if cookies_override:
        thread_ctx.cookies = {'.ROBLOSECURITY': cookies_override}
    else:
        thread_ctx.cookies = COOKIES
    # If an item specifies a proxy, treat it as an override (no pool rotation).
    thread_ctx.proxy_override = proxy
    thread_ctx.proxy = proxy
    if not getattr(thread_ctx, 'session', None):
        s = requests.Session()
        # Avoid picking up proxy env vars; we explicitly control proxy usage.
        s.trust_env = False
        thread_ctx.session = s

def get_thread_session():
    """Get a per-thread session for connection reuse."""
    if not getattr(thread_ctx, 'session', None):
        s = requests.Session()
        # Avoid picking up proxy env vars; we explicitly control proxy usage.
        s.trust_env = False
        thread_ctx.session = s
    return thread_ctx.session

def get_balance():
    """Get current Robux balance"""
    url = "https://economy.roblox.com/v1/user/currency"
    last_err = None
    for _ in range(3):
        try:
            sleep_if_rate_limited()
            response = get_thread_session().get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                **get_roblox_request_kwargs(include_cookies=True),
            )
            if response.status_code == 429:
                handle_429_response("balance", response, proxy_used=getattr(thread_ctx, "proxy", None))
                last_err = f"HTTP 429 - {response.text}"
                continue
            response.raise_for_status()
            note_rate_limit_success()
            return response.json().get('robux', 0)
        except requests.exceptions.RequestException as e:
            maybe_mark_proxy_bad(e)
            last_err = e
    with print_lock:
        print(f"Error fetching balance: {last_err}")
    return 0

def get_balance_cached(cache_seconds=5):
    """Get balance with short caching + jitter to reduce request rate."""
    jitter = random.uniform(-1.0, 1.0)
    ttl = max(1.0, cache_seconds + jitter)
    current_time = time.time()
    with balance_cache_lock:
        if balance_cache['value'] is not None and current_time - balance_cache['last_fetch'] < ttl:
            return balance_cache['value']
    value = get_balance()
    with balance_cache_lock:
        balance_cache['value'] = value
        balance_cache['last_fetch'] = current_time
    return value

def log_high_discount(item_name, product_id, listing_id, price, rolimons_value, discount_pct):
    """Log extreme discounts to a file for later analysis."""
    if discount_pct < 90:
        return
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    line = (
        f"{timestamp} | {item_name} | price={price} | value={rolimons_value} | "
        f"discount={discount_pct:.1f}% | product={product_id} | listing={listing_id}\n"
    )
    with log_lock:
        with open("high_discount_log.txt", "a", encoding="utf-8") as f:
            f.write(line)

def get_authenticated_user_id():
    """Get the current authenticated user id"""
    roblosecurity = get_thread_cookies().get('.ROBLOSECURITY')
    cache_key = roblosecurity or 'default'
    with auth_user_id_lock:
        cached = auth_user_id_cache.get(cache_key)
        if cached:
            return cached
    url = "https://users.roblox.com/v1/users/authenticated"
    last_err = None
    for _ in range(3):
        try:
            sleep_if_rate_limited()
            response = get_thread_session().get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                **get_roblox_request_kwargs(include_cookies=True),
            )
            if response.status_code == 429:
                handle_429_response("auth_user", response, proxy_used=getattr(thread_ctx, "proxy", None))
                last_err = f"HTTP 429 - {response.text}"
                continue
            response.raise_for_status()
            note_rate_limit_success()
            user_id = response.json().get('id')
            if user_id:
                with auth_user_id_lock:
                    auth_user_id_cache[cache_key] = user_id
            return user_id
        except requests.exceptions.RequestException as e:
            maybe_mark_proxy_bad(e)
            last_err = e
    with print_lock:
        print(f"Error fetching authenticated user: {last_err}")
    return None

# STEP 2.5: Get Rolimons Value
def get_rolimons_value(asset_id):
    """Fetch item value from Rolimons. Uses valuation if available, falls back to RAP (Recent Average Price)"""
    # Using the new rolimons.com API endpoint
    url = f"https://api.rolimons.com/items/v1/itemdetails"
    try:
        # Never send Roblox cookies to non-Roblox domains; proxy-only is OK.
        response = get_thread_session().get(url, params={'assetId': asset_id}, timeout=10, **get_proxy_kwargs())
        response.raise_for_status()
        data = response.json()
        
        if not data.get('success'):
            return None
        
        # FIXED: The response structure is different!
        # Items are at TOP LEVEL, not under data['data']['items']
        items = data.get('items', {})
        item_key = str(asset_id)
        
        if item_key not in items:
            return None
        
        # Item data is an array: [name, acronym, rap, value, default_value, demand, trend, projected, hyped, rare]
        item_data = items[item_key]
        
        # Index 3 is "value" (valuation), Index 2 is "rap" (recent average price)
        valuation = item_data[3] if len(item_data) > 3 else None
        rap = item_data[2] if len(item_data) > 2 else None
        
        # Try valuation first, fall back to RAP
        if valuation and valuation > 0:
            return valuation
        elif rap and rap > 0:
            return rap
        
        return None
    except requests.exceptions.RequestException as e:
        maybe_mark_proxy_bad(e)
        with print_lock:
            print(f"Error fetching Rolimons data: {e}")
        return None

def get_catalog_csrf():
    """Retrieve CSRF token for catalog API calls (thread-safe)"""
    roblosecurity = get_thread_cookies().get('.ROBLOSECURITY')
    cache_key = roblosecurity or 'default'
    with catalog_csrf_lock:
        if cache_key in catalog_csrf_cache:
            return catalog_csrf_cache[cache_key]
        last_err = None
        for _ in range(2):
            try:
                sleep_if_rate_limited()
                res = get_thread_session().post(
                    "https://catalog.roblox.com/v1/catalog/items/details",
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                    **get_roblox_request_kwargs(include_cookies=True),
                    json={"items": []},
                )
                if res.status_code == 429:
                    handle_429_response("catalog_csrf", res, proxy_used=getattr(thread_ctx, "proxy", None))
                    last_err = f"HTTP 429 - {res.text}"
                    continue
                token = res.headers.get('x-csrf-token')
                if token:
                    catalog_csrf_cache[cache_key] = token
                note_rate_limit_success()
                return token
            except requests.exceptions.RequestException as e:
                maybe_mark_proxy_bad(e)
                last_err = e
        with print_lock:
            print(f"Error getting catalog CSRF token: {last_err}")
        return None

def get_collectible_item_id(asset_id):
    """Get collectibleItemId for a classic limited asset ID"""
    with collectible_cache_lock:
        cached = collectible_id_cache.get(asset_id)
        if cached:
            return cached
    url = "https://catalog.roblox.com/v1/catalog/items/details"
    headers = dict(HEADERS)
    token = get_catalog_csrf()
    if token:
        headers['x-csrf-token'] = token
    payload = {"items": [{"id": asset_id, "itemType": "Asset"}]}
    last_err = None
    for _ in range(2):
        try:
            sleep_if_rate_limited()
            res = get_thread_session().post(
                url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
                **get_roblox_request_kwargs(include_cookies=True),
            )
            if res.status_code == 429:
                handle_429_response("collectible_id", res, proxy_used=getattr(thread_ctx, "proxy", None))
                last_err = f"HTTP 429 - {res.text}"
                continue
            if res.status_code == 403:
                new_token = res.headers.get('x-csrf-token')
                if new_token:
                    roblosecurity = get_thread_cookies().get('.ROBLOSECURITY')
                    cache_key = roblosecurity or 'default'
                    catalog_csrf_cache[cache_key] = new_token
                    headers['x-csrf-token'] = new_token
                    res = get_thread_session().post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=REQUEST_TIMEOUT,
                        **get_roblox_request_kwargs(include_cookies=True),
                    )
                    if res.status_code == 429:
                        handle_429_response("collectible_id", res, proxy_used=getattr(thread_ctx, "proxy", None))
                        last_err = f"HTTP 429 - {res.text}"
                        continue
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict) and data.get('data'):
                collectible_id = data['data'][0].get('collectibleItemId')
                if collectible_id:
                    with collectible_cache_lock:
                        collectible_id_cache[asset_id] = collectible_id
                note_rate_limit_success()
                return collectible_id
            return None
        except requests.exceptions.RequestException as e:
            maybe_mark_proxy_bad(e)
            last_err = e
    with print_lock:
        print(f"Error fetching collectibleItemId: {last_err}")
    return None

def get_first_reseller(asset_id, limit=1):
    """
    Fetch the first reseller listing from the first page only.
    Assumes the API returns listings sorted by price.
    Returns (collectibleProductId, collectibleItemInstanceId, price, seller_id, seller_type) or None.
    """
    collectible_id = get_collectible_item_id(asset_id)
    if not collectible_id:
        return None
    url = f"https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resellers"
    last_error = None
    for attempt in range(RESELLER_RETRIES + 1):
        sleep_if_rate_limited()
        try:
            req_kwargs = get_reseller_request_kwargs(include_cookies=RESELLER_SEND_COOKIES)
            proxy_used = get_current_proxy()
            t0 = time.perf_counter()
            res = get_thread_session().get(
                url,
                params={"limit": limit},
                timeout=RESELLER_TIMEOUT,
                **req_kwargs,
            )
            dt = time.perf_counter() - t0

            # Proactively respect per-proxy rate limit headers when present to reduce 429s.
            remaining, reset_s = _ratelimit_seconds_from_headers(res)
            if remaining is not None and reset_s and reset_s > 0:
                # When the bucket is nearly empty, cooldown that proxy until reset.
                if proxy_used:
                    if remaining <= 1:
                        cooldown_proxy(proxy_used, reset_s, reason=f"ratelimit remaining={remaining}")
                else:
                    # Direct traffic: throttle this thread until reset when near empty.
                    if remaining <= 1:
                        thread_ctx.rate_limit_until = max(getattr(thread_ctx, "rate_limit_until", 0), time.time() + reset_s)

            if proxy_used and dt > PROXY_SLOW_REQUEST_THRESHOLD_SECONDS:
                # First request on a newly selected proxy is often slower due to connect/TLS warmup.
                # Don't immediately churn to the next proxy on a single slow cold-start.
                uses = getattr(thread_ctx, "reseller_proxy_uses", 0)
                if uses > 1:
                    cooldown_proxy(proxy_used, PROXY_SLOW_COOLDOWN_SECONDS, reason=f"slow {dt:.2f}s")
                    # Force a re-pick next loop; cooldown will keep it out of the pool briefly.
                    thread_ctx.reseller_proxy = None
                    thread_ctx.reseller_proxy_uses = 0
                    thread_ctx.proxy = None
        except requests.exceptions.RequestException as e:
            maybe_mark_proxy_bad(e)
            last_error = str(e)
            if attempt < RESELLER_RETRIES:
                time.sleep(RESELLER_BACKOFF_BASE * (1 + random.random()))
                continue
            break

        if res.status_code == 429:
            # Reseller polling may already rotate proxies per request; don't double-rotate here.
            handle_429_response("resellers", res, proxy_used=proxy_used, rotate_on_429=False)
            last_error = None  # already logged as rate-limit; avoid noisy "Error fetching reseller list"
            if attempt < RESELLER_RETRIES:
                continue
            return None

        try:
            res.raise_for_status()
            data = res.json()
            if not data.get('data'):
                return None
            item = data['data'][0]
            product_id = item.get('collectibleProductId')
            instance_id = item.get('collectibleItemInstanceId')
            price = item.get('price')
            seller = item.get('seller') or {}
            seller_id = seller.get('sellerId')
            seller_type = seller.get('sellerType')
            if not (product_id and instance_id and price is not None):
                return None
            note_rate_limit_success()
            return (product_id, instance_id, price, seller_id, seller_type)
        except ValueError as e:
            last_error = f"Invalid JSON: {e}"
        except requests.exceptions.RequestException as e:
            maybe_mark_proxy_bad(e)
            last_error = str(e)

        if attempt < RESELLER_RETRIES:
            time.sleep(RESELLER_BACKOFF_BASE * (1 + random.random()))

    if last_error:
        with print_lock:
            print(f"Error fetching reseller list: {last_error}")
    return None

def is_uuid(value):
    """Check if a value looks like a UUID"""
    if not isinstance(value, str):
        return False
    return bool(re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', value))

def buy_collectible(asset_id, collectible_product_id, collectible_instance_id, expected_price, expected_seller_id, expected_seller_type):
    """Attempt to purchase a collectible resale listing"""
    collectible_id = get_collectible_item_id(asset_id)
    if not collectible_id:
        with print_lock:
            print("    Purchase failed: missing collectibleItemId")
        return None
    url = f"https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/purchase-resale"
    purchaser_id = get_authenticated_user_id()
    if not purchaser_id:
        with print_lock:
            print("    Purchase failed: could not resolve purchaser id")
        return None
    headers = dict(HEADERS)
    # Try to send a cached CSRF token up-front to avoid a 403 retry.
    roblosecurity = get_thread_cookies().get('.ROBLOSECURITY')
    cache_key = roblosecurity or 'default'
    token = get_cached_catalog_csrf()
    if token:
        headers['x-csrf-token'] = token
    # Some reseller responses omit sellerType; default to User (most resale listings).
    seller_id = str(expected_seller_id) if expected_seller_id is not None else None
    seller_type = expected_seller_type or "User"
    if not isinstance(seller_type, str):
        seller_type = str(seller_type)
    try:
        expected_price = int(expected_price)
    except (TypeError, ValueError):
        pass
    payload = {
        "collectibleItemId": collectible_id,
        "collectibleItemInstanceId": collectible_instance_id,
        "collectibleProductId": collectible_product_id,
        "expectedCurrency": 1,
        "expectedPrice": expected_price,
        "expectedPurchaserId": str(purchaser_id),
        "expectedPurchaserType": "User",
        "expectedSellerId": seller_id,
        "expectedSellerType": seller_type,
        "idempotencyKey": str(uuid.uuid4())
    }
    try:
        # Purchase is latency-sensitive; prefer direct (no proxy) unless PURCHASE_USE_PROXY is enabled.
        res = get_thread_purchase_session().post(url, headers=headers, json=payload, timeout=PURCHASE_TIMEOUT, **get_purchase_request_kwargs())
        if res.status_code == 403:
            new_token = res.headers.get('x-csrf-token')
            if new_token:
                headers['x-csrf-token'] = new_token
                with catalog_csrf_lock:
                    catalog_csrf_cache[cache_key] = new_token
                res = get_thread_purchase_session().post(url, headers=headers, json=payload, timeout=PURCHASE_TIMEOUT, **get_purchase_request_kwargs())
        if res.status_code >= 400:
            with print_lock:
                print(f"    Purchase failed: HTTP {res.status_code} - {res.text}")
            return None
        try:
            return res.json()
        except ValueError:
            return {"raw": res.text}
    except requests.exceptions.RequestException as e:
        # Don't mark the thread proxy bad if we didn't use it for purchase.
        if PURCHASE_USE_PROXY:
            maybe_mark_proxy_bad(e)
        with print_lock:
            print(f"    Error during collectible purchase: {e}")
        return None

# MAIN LOGIC
def snipe_loop(asset_id, item_name, discount_threshold, interval=None, cookies_override=None, proxy=None):
    """Continuously attempt to snipe a single item (runs in dedicated thread)"""
    set_thread_context(cookies_override, proxy)
    if interval is None:
        interval = POLL_INTERVAL
    def sleep_remaining(loop_start):
        # Target a fixed iteration cadence: effective period is max(interval, request time).
        remaining = interval - (time.perf_counter() - loop_start)
        if remaining > 0:
            time.sleep(remaining)

    # Purchase path is latency-sensitive. Warm direct connection and prefetch CSRF/user id for this account.
    warm_purchase_session()
    if not get_cached_catalog_csrf():
        get_catalog_csrf()
    get_authenticated_user_id()
    
    attempt = 0
    last_cheapest = None
    last_insufficient = None
    last_balance_check = {'price': None, 'robux': None}
    rolimons_cache = {'value': None, 'last_fetch': 0}  # Cache Rolimons value
    
    # Fetch initial Rolimons value
    with print_lock:
        print(f"[{item_name}] Fetching Rolimons value...")
    rolimons_value = get_rolimons_value(asset_id)
    if rolimons_value is None:
        with print_lock:
            print(f"[{item_name}] WARNING: Could not fetch Rolimons value initially. Item ID {asset_id} may be invalid.")
    else:
        rolimons_cache['value'] = rolimons_value
        rolimons_cache['last_fetch'] = time.time()
        with print_lock:
            print(f"[{item_name}] Rolimons value: {rolimons_value} Robux")
    
    while True:
        loop_start = time.perf_counter()
        attempt += 1
        
        # Fetch only the first reseller listing (assumes sorted by price)
        cheapest = get_first_reseller(asset_id, limit=1)
        if not cheapest:
            with print_lock:
                print(f"[{item_name}] Attempt {attempt}: Could not fetch prices for resellers")
            sleep_remaining(loop_start)
            continue
        
        cheapest_product_id, cheapest_instance_id, cheapest_price, cheapest_seller_id, cheapest_seller_type = cheapest
        is_new_cheapest = last_cheapest != (cheapest_product_id, cheapest_instance_id, cheapest_price)
        if is_new_cheapest:
            last_cheapest = (cheapest_product_id, cheapest_instance_id, cheapest_price)

        # Check if we should buy (cache Rolimons value to avoid excessive API calls)
        current_time = time.time()
        if current_time - rolimons_cache['last_fetch'] > REFRESH_TIME:  # Refresh every x seconds
            rolimons_value = get_rolimons_value(asset_id)
            if rolimons_value is None:
                with print_lock:
                    print(f"[{item_name}] Could not refresh Rolimons value, using cached value...")
                if rolimons_cache['value'] is None:
                    sleep_remaining(loop_start)
                    continue
                rolimons_value = rolimons_cache['value']
            else:
                with print_lock:
                    print(f"[{item_name}] Refreshed Rolimons value: {rolimons_value} Robux")
                rolimons_cache['value'] = rolimons_value
                rolimons_cache['last_fetch'] = current_time
        else:
            rolimons_value = rolimons_cache['value']
            if rolimons_value is None:
                with print_lock:
                    print(f"[{item_name}] Rolimons value not yet available, skipping...")
                sleep_remaining(loop_start)
                continue
        
        max_buy_price = int(rolimons_value * discount_threshold)
        discount_pct = (1 - cheapest_price / rolimons_value) * 100 if rolimons_value > 0 else 0

        # Logging/printing can add measurable latency during a buy race; defer it until after a purchase attempt.
        if is_new_cheapest and cheapest_price > max_buy_price:
            with print_lock:
                print(f"[{item_name}] Attempt {attempt}: Cheapest Resale Listing {cheapest_instance_id} @ {cheapest_price} Robux")
        
        if cheapest_price <= max_buy_price:
            if is_uuid(cheapest_instance_id):
                if cheapest_seller_id is None:
                    with print_lock:
                        print(f"[{item_name}] Purchase skipped: missing seller id for collectible listing.")
                else:
                    # Avoid repeated purchase attempts for the same listing id in a tight loop.
                    last_id = getattr(thread_ctx, "last_purchase_listing_id", None)
                    last_ts = getattr(thread_ctx, "last_purchase_listing_ts", 0.0)
                    now = time.time()
                    if (last_id == cheapest_instance_id and
                        now - last_ts < MIN_SECONDS_BETWEEN_SAME_LISTING_PURCHASE and
                        attempt % HEARTBEAT_EVERY != 0):
                        sleep_remaining(loop_start)
                        continue
                    thread_ctx.last_purchase_listing_id = cheapest_instance_id
                    thread_ctx.last_purchase_listing_ts = now

                    # Attempt purchase first (skip pre-checking balance for lowest latency).
                    result = buy_collectible(
                        asset_id,
                        cheapest_product_id,
                        cheapest_instance_id,
                        cheapest_price,
                        cheapest_seller_id,
                        cheapest_seller_type,
                    )

                    # Print after the purchase request completes.
                    with print_lock:
                        print(f"\n[SNIPE] Found for {item_name}. Purchase attempted.")
                        print(f"    Item: {item_name}")
                        print(f"    Resale Product ID: {cheapest_product_id}")
                        print(f"    Resale Listing ID: {cheapest_instance_id}")
                        print(f"    Price: {cheapest_price} Robux")
                        print(f"    Discount: {discount_pct:.1f}% off Rolimons")
                        print(f"    Result: {result}")

                    # Do any file logging after the purchase attempt to avoid slowing the request down.
                    log_high_discount(item_name, cheapest_product_id, cheapest_instance_id, cheapest_price, rolimons_value, discount_pct)

                    # If the server indicates insufficient funds, fetch balance for visibility (post-fact).
                    if isinstance(result, dict):
                        err = (result.get("errorMessage") or "").strip().lower()
                        if err in ("insufficientfunds", "notenoughrobux", "insufficientrobux", "notenoughfunds"):
                            robux = get_balance_cached()
                            last_balance_check["price"] = cheapest_price
                            last_balance_check["robux"] = robux
                            insufficient_key = (cheapest_price, robux)
                            if last_insufficient != insufficient_key or attempt % HEARTBEAT_EVERY == 0:
                                with print_lock:
                                    print(f"[{item_name}] Insufficient Robux. Need {cheapest_price}, have {robux}")
                                last_insufficient = insufficient_key
            else:
                with print_lock:
                    print(f"[{item_name}] Purchase skipped: unsupported listing id format {cheapest_instance_id}")
        sleep_remaining(loop_start)

        if attempt % HEARTBEAT_EVERY == 0:
            with print_lock:
                if last_cheapest:
                    print(f"[{item_name}] Heartbeat: attempt {attempt}, last listing {last_cheapest[1]} @ {last_cheapest[2]} Robux (proxy={get_current_proxy()})")
                else:
                    print(f"[{item_name}] Heartbeat: attempt {attempt}, no listings yet (proxy={get_current_proxy()})")

# RUN
if __name__ == "__main__":
    # Initialize optional proxy pool before any network calls.
    if PROXY_STRATEGY != "off":
        loaded = load_proxies(PROXIES_FILE)
        if loaded:
            proxy_pool = ProxyPool(loaded)
            print(f"[STARTUP] Loaded {len(loaded)} proxies from {PROXIES_FILE} (strategy={PROXY_STRATEGY})")
        else:
            print(f"[STARTUP] No proxies loaded from {PROXIES_FILE}; continuing without proxies")

    # Ensure main thread has a session/cookie context for startup checks.
    set_thread_context()

    if not ITEMS_TO_SNIPE:
        print("No items configured to snipe! Add items to ITEMS_TO_SNIPE list.")
        sys.exit(1)
    
    print(f"Starting sniper for {len(ITEMS_TO_SNIPE)} item(s)...")
    print(f"Poll interval: {POLL_INTERVAL}s per item")
    print(f"Total requests/sec: ~{len(ITEMS_TO_SNIPE) / POLL_INTERVAL:.1f} (including other API calls)")
    print("-" * 60)
    
    # Test authentication before starting
    print("\n[STARTUP] Testing authentication...")
    auth_user_id = get_authenticated_user_id()
    if not auth_user_id:
        print("\n❌ FATAL: Authentication failed. Check your cookies!")
        print("   Your .ROBLOSECURITY cookie may be expired or invalid.")
        sys.exit(1)
    else:
        print(f"✓ Authenticated as user id: {auth_user_id}")
    
    # Test Robux balance
    robux = get_balance()
    if robux == 0:
        print(f"⚠️  WARNING: Robux balance is 0. You won't be able to purchase.")
    else:
        print(f"✓ Robux balance: {robux}")
    
    print("-" * 60 + "\n")
    print("💡 Type 'q', 'quit', 'exit', or 'stop' in the terminal to gracefully shut down.")
    print("-" * 60 + "\n")
    
    # Create and start a thread for each item
    snipe_threads = []
    for item in ITEMS_TO_SNIPE:
        thread = threading.Thread(
            target=snipe_loop,
            args=(
                item['asset_id'],
                item['name'],
                item['discount_threshold'],
                None,
                item.get('roblosecurity'),
                item.get('proxy')
            ),
            daemon=True
        )
        thread.start()
        snipe_threads.append(thread)
        time.sleep(0.1 + random.uniform(0.05, 0.25))  # Stagger thread starts with jitter
    
    # Start input handler thread for graceful shutdown
    input_thread = threading.Thread(target=input_handler, daemon=True)
    input_thread.start()
    
    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
            if SHUTDOWN_FLAG:
                break
    except KeyboardInterrupt:
        graceful_shutdown("Keyboard interrupt detected - shutting down")
