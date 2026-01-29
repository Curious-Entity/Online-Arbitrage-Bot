import requests
import time
import sys
import threading
from threading import Lock
import re
import uuid
import random

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
    '.ROBLOSECURITY': 'CAEaAhADIhsKBGR1aWQSEzEwNjEwOTIxODc3MDU5OTUyNTkoAw.1ixOLthU4c-yZSMEXt5VvxAPWfz695LC-WU22n9CHqEVXGEWIbTLJHK8BxO-cr_yy31O5GyekiQFm5qXa47PAV5kW40M64c3jZL2VFIOrf4SWoZ51NUq_tikRzK3wZQjq0nEe0it6Ei8KHf898iepNbG57oAti72As6K7q0v7kzXzGOL4AGFbDAY6K9pHBO_e9Bvvp7Id_xh_Fst1doN57SEhGTb18wDk3b_PXiRftPRVgiEBc-pBF5l30sKSyVZQJPiBZ10H8EjQl_pJjddOqBXwzPVcSG-P9X6Y1RjZOF_-_2_xQzjN1G_6rqWOHyXz5bdQWLK4eB_Z8yWqA5wLhkG_8bLgqO3yemFNmdsVr4RNg_v_2osRGm4u0blmP3NSuzo5WGEnmZFu2kXEoYw3A8eZcUkOCGDdHHjFDAZGLmxyTsv-iUSgIj38QrQjCLQtU3KKgDC2jD3NYLXdl7tt3mm5aZjENn9OSYpaSmljVbfZ_VdbGfU-y0Rar0lSHHvJDrwR4ix8GU6K0xLWiinLOhjBg8t0AxE9xzeM88iBpKuJwYcgY4VBGLCK9cuJgEOFigumh2eck_cpuVEuMMM6pJACteG0ILr0cjQ7CzAwrUsKagHafFOBW7jNK_8tUGPkP--R4s1pOGHiWro8Saqjjz9yjkMRHgPjDvBkkC6x2dy-uhrizURSGkZeSJmgVJ_KwEDP9i29adBO3tN1OG82uiP5yLUqKglYsBweH70U6NlQlqKeZSG8-JVO-hMhPZSxa3LOHfhq1miu5SOTBcXUshgmSg'
}

USER_AGENT = 'Roblox/WinInet'
POLL_INTERVAL = 1  # Check each item every x seconds (increased to reduce rate-limit risk)
HEARTBEAT_EVERY = 20  # Print a heartbeat every N attempts
REQUEST_TIMEOUT = 6  # Seconds per request before timing out
RESELLER_RETRIES = 0  # Number of retries for reseller fetch
RESELLER_BACKOFF_BASE = 0.15  # Base backoff (seconds) between retries
REFRESH_TIME = 300  # Seconds before refreshing Rolimons and Robux value


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

# Global CSRF token cache for catalog calls (per account)
catalog_csrf_cache = {}

# Cache collectibleItemId per asset to reduce catalog calls
collectible_id_cache = {}
collectible_cache_lock = Lock()

# Short-lived balance cache (seconds)
balance_cache = {'value': None, 'last_fetch': 0}
balance_cache_lock = Lock()

# Global flag for graceful shutdown
SHUTDOWN_FLAG = False
shutdown_lock = Lock()

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
    return getattr(thread_ctx, 'proxy', None)

def get_request_kwargs():
    """Build cookies/proxy kwargs for requests.* calls."""
    kwargs = {'cookies': get_thread_cookies()}
    proxy = get_thread_proxy()
    if proxy:
        kwargs['proxies'] = {'http': proxy, 'https': proxy}
    return kwargs

def set_thread_context(cookies_override=None, proxy=None):
    """Set per-thread cookies/proxy for this item."""
    if cookies_override:
        thread_ctx.cookies = {'.ROBLOSECURITY': cookies_override}
    else:
        thread_ctx.cookies = COOKIES
    thread_ctx.proxy = proxy
    if not getattr(thread_ctx, 'session', None):
        thread_ctx.session = requests.Session()

def get_thread_session():
    """Get a per-thread session for connection reuse."""
    if not getattr(thread_ctx, 'session', None):
        thread_ctx.session = requests.Session()
    return thread_ctx.session

def get_balance():
    """Get current Robux balance"""
    url = "https://economy.roblox.com/v1/user/currency"
    try:
        response = get_thread_session().get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **get_request_kwargs())
        response.raise_for_status()
        return response.json().get('robux', 0)
    except requests.exceptions.RequestException as e:
        with print_lock:
            print(f"Error fetching balance: {e}")
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
    url = "https://users.roblox.com/v1/users/authenticated"
    try:
        response = get_thread_session().get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **get_request_kwargs())
        response.raise_for_status()
        return response.json().get('id')
    except requests.exceptions.RequestException as e:
        with print_lock:
            print(f"Error fetching authenticated user: {e}")
        return None

# STEP 2.5: Get Rolimons Value
def get_rolimons_value(asset_id):
    """Fetch item value from Rolimons. Uses valuation if available, falls back to RAP (Recent Average Price)"""
    # Using the new rolimons.com API endpoint
    url = f"https://api.rolimons.com/items/v1/itemdetails"
    try:
        response = requests.get(url, params={'assetId': asset_id}, timeout=10)
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
        try:
            res = requests.post(
                "https://catalog.roblox.com/v1/catalog/items/details",
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                **get_request_kwargs(),
                json={"items": []},
            )
            token = res.headers.get('x-csrf-token')
            if token:
                catalog_csrf_cache[cache_key] = token
            return token
        except requests.exceptions.RequestException as e:
            with print_lock:
                print(f"Error getting catalog CSRF token: {e}")
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
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT, **get_request_kwargs())
        if res.status_code == 403:
            new_token = res.headers.get('x-csrf-token')
            if new_token:
                roblosecurity = get_thread_cookies().get('.ROBLOSECURITY')
                cache_key = roblosecurity or 'default'
                catalog_csrf_cache[cache_key] = new_token
                headers['x-csrf-token'] = new_token
                res = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT, **get_request_kwargs())
        res.raise_for_status()
        data = res.json()
        if isinstance(data, dict) and data.get('data'):
            collectible_id = data['data'][0].get('collectibleItemId')
            if collectible_id:
                with collectible_cache_lock:
                    collectible_id_cache[asset_id] = collectible_id
            return collectible_id
        return None
    except requests.exceptions.RequestException as e:
        with print_lock:
            print(f"Error fetching collectibleItemId: {e}")
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
    try:
        last_error = None
        for attempt in range(RESELLER_RETRIES + 1):
            res = get_thread_session().get(
                url,
                params={"limit": limit},
                timeout=REQUEST_TIMEOUT,
                **get_request_kwargs()
            )
            if res.status_code == 429:
                last_error = f"HTTP 429 - {res.text}"
            else:
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
                    return (product_id, instance_id, price, seller_id, seller_type)
                except requests.exceptions.RequestException as e:
                    last_error = str(e)
            # jittered backoff before retry
            time.sleep(RESELLER_BACKOFF_BASE * (1 + random.random()))
        if last_error:
            with print_lock:
                print(f"Error fetching reseller list: {last_error}")
        return None
    except requests.exceptions.RequestException as e:
        with print_lock:
            print(f"Error fetching reseller list: {e}")
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
    payload = {
        "collectibleItemId": collectible_id,
        "collectibleItemInstanceId": collectible_instance_id,
        "collectibleProductId": collectible_product_id,
        "expectedCurrency": 1,
        "expectedPrice": expected_price,
        "expectedPurchaserId": str(purchaser_id),
        "expectedPurchaserType": "User",
        "expectedSellerId": 1,
        "expectedSellerType": None,
        "idempotencyKey": str(uuid.uuid4())
    }
    try:
        res = get_thread_session().post(url, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT, **get_request_kwargs())
        if res.status_code == 403:
            new_token = res.headers.get('x-csrf-token')
            if new_token:
                headers = dict(HEADERS)
                headers['x-csrf-token'] = new_token
                res = get_thread_session().post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT, **get_request_kwargs())
        if res.status_code >= 400:
            with print_lock:
                print(f"    Purchase failed: HTTP {res.status_code} - {res.text}")
            return None
        try:
            return res.json()
        except ValueError:
            return {"raw": res.text}
    except requests.exceptions.RequestException as e:
        with print_lock:
            print(f"    Error during collectible purchase: {e}")
        return None

# MAIN LOGIC
def snipe_loop(asset_id, item_name, discount_threshold, interval=None, cookies_override=None, proxy=None):
    """Continuously attempt to snipe a single item (runs in dedicated thread)"""
    set_thread_context(cookies_override, proxy)
    if interval is None:
        interval = POLL_INTERVAL
    
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
        attempt += 1
        
        # Fetch only the first reseller listing (assumes sorted by price)
        cheapest = get_first_reseller(asset_id, limit=1)
        if not cheapest:
            with print_lock:
                print(f"[{item_name}] Attempt {attempt}: Could not fetch prices for resellers")
            time.sleep(interval)
            continue
        
        cheapest_product_id, cheapest_instance_id, cheapest_price, cheapest_seller_id, cheapest_seller_type = cheapest
        
        # Only log if price changed (reduce spam)
        if last_cheapest != (cheapest_product_id, cheapest_instance_id, cheapest_price):
            with print_lock:
                print(f"[{item_name}] Attempt {attempt}: Cheapest Resale Listing {cheapest_instance_id} @ {cheapest_price} Robux")
            last_cheapest = (cheapest_product_id, cheapest_instance_id, cheapest_price)
        
        # Check if we should buy (cache Rolimons value to avoid excessive API calls)
        current_time = time.time()
        if current_time - rolimons_cache['last_fetch'] > REFRESH_TIME:  # Refresh every x seconds
            rolimons_value = get_rolimons_value(asset_id)
            if rolimons_value is None:
                with print_lock:
                    print(f"[{item_name}] Could not refresh Rolimons value, using cached value...")
                if rolimons_cache['value'] is None:
                    time.sleep(interval)
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
                time.sleep(interval)
                continue
        
        max_buy_price = int(rolimons_value * discount_threshold)
        discount_pct = (1 - cheapest_price / rolimons_value) * 100 if rolimons_value > 0 else 0
        log_high_discount(item_name, cheapest_product_id, cheapest_instance_id, cheapest_price, rolimons_value, discount_pct)
        
        if cheapest_price <= max_buy_price:
            # Skip balance checks if price is unchanged and last known balance was insufficient
            if (last_balance_check['price'] == cheapest_price and
                last_balance_check['robux'] is not None and
                last_balance_check['robux'] < cheapest_price and
                attempt % HEARTBEAT_EVERY != 0):
                time.sleep(interval)
                continue

            robux = get_balance_cached()
            last_balance_check['price'] = cheapest_price
            last_balance_check['robux'] = robux
            if robux >= cheapest_price:
                with print_lock:
                    print(f"\n✓✓✓ SNIPE FOUND FOR {item_name}! Attempting purchase...")
                if is_uuid(cheapest_instance_id):
                    if cheapest_seller_id is None:
                        with print_lock:
                            print(f"[{item_name}] Purchase skipped: missing seller id for collectible listing.")
                    else:
                        result = buy_collectible(asset_id, cheapest_product_id, cheapest_instance_id, cheapest_price, cheapest_seller_id, cheapest_seller_type)
                        if result:
                            with print_lock:
                                print(f"✓✓✓ PURCHASE ATTEMPTED FOR {item_name} (collectible UUID)")
                                print(f"    Item: {item_name}")
                                print(f"    Resale Product ID: {cheapest_product_id}")
                                print(f"    Resale Listing ID: {cheapest_instance_id}")
                                print(f"    Price: {cheapest_price} Robux")
                                print(f"    Discount: {discount_pct:.1f}% off Rolimons")
                                print(f"    Result: {result}")
                        else:
                            with print_lock:
                                print(f"[{item_name}] Purchase failed for collectible listing, continuing snipe...")
                else:
                    with print_lock:
                        print(f"[{item_name}] Purchase skipped: unsupported listing id format {cheapest_instance_id}")
            else:
                # Only log if price/balance changed or on heartbeat interval
                insufficient_key = (cheapest_price, robux)
                if last_insufficient != insufficient_key or attempt % HEARTBEAT_EVERY == 0:
                    with print_lock:
                        print(f"[{item_name}] Insufficient Robux. Need {cheapest_price}, have {robux}")
                    last_insufficient = insufficient_key
        
        time.sleep(interval)

        if attempt % HEARTBEAT_EVERY == 0:
            with print_lock:
                if last_cheapest:
                    print(f"[{item_name}] Heartbeat: attempt {attempt}, last listing {last_cheapest[1]} @ {last_cheapest[2]} Robux")
                else:
                    print(f"[{item_name}] Heartbeat: attempt {attempt}, no listings yet")

# RUN
if __name__ == "__main__":
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
