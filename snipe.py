import requests
import time
import sys
import threading
from threading import Lock
import signal
import re
import json
import uuid

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
         'asset_id': 24826737,  # Used for Rolimons + marketplace-sales lookup
         'name': 'Moon Egg',
         'discount_threshold': 1.5
     }
]

# AUTHENTICATION - Copy all cookies from your browser's DevTools
COOKIES = {
    '.ROBLOSECURITY': 'CAEaAhADIhsKBGR1aWQSEzEwNjEwOTIxODc3MDU5OTUyNTkoAw.1ixOLthU4c-yZSMEXt5VvxAPWfz695LC-WU22n9CHqEVXGEWIbTLJHK8BxO-cr_yy31O5GyekiQFm5qXa47PAV5kW40M64c3jZL2VFIOrf4SWoZ51NUq_tikRzK3wZQjq0nEe0it6Ei8KHf898iepNbG57oAti72As6K7q0v7kzXzGOL4AGFbDAY6K9pHBO_e9Bvvp7Id_xh_Fst1doN57SEhGTb18wDk3b_PXiRftPRVgiEBc-pBF5l30sKSyVZQJPiBZ10H8EjQl_pJjddOqBXwzPVcSG-P9X6Y1RjZOF_-_2_xQzjN1G_6rqWOHyXz5bdQWLK4eB_Z8yWqA5wLhkG_8bLgqO3yemFNmdsVr4RNg_v_2osRGm4u0blmP3NSuzo5WGEnmZFu2kXEoYw3A8eZcUkOCGDdHHjFDAZGLmxyTsv-iUSgIj38QrQjCLQtU3KKgDC2jD3NYLXdl7tt3mm5aZjENn9OSYpaSmljVbfZ_VdbGfU-y0Rar0lSHHvJDrwR4ix8GU6K0xLWiinLOhjBg8t0AxE9xzeM88iBpKuJwYcgY4VBGLCK9cuJgEOFigumh2eck_cpuVEuMMM6pJACteG0ILr0cjQ7CzAwrUsKagHafFOBW7jNK_8tUGPkP--R4s1pOGHiWro8Saqjjz9yjkMRHgPjDvBkkC6x2dy-uhrizURSGkZeSJmgVJ_KwEDP9i29adBO3tN1OG82uiP5yLUqKglYsBweH70U6NlQlqKeZSG8-JVO-hMhPZSxa3LOHfhq1miu5SOTBcXUshgmSg',
    '.RBXEventTrackerV2': 'CreateDate=01/27/2026 21:20:13&rbxid=1368859808&browserid=1757041865873001',
    'RBXSessionTracker': 'sessionid=c7ce1ab5-4734-4ed8-9ea6-34b596e896bd',
    'rbx-ip2': 'rbx-ip2',  # Note: your cookies have rbx-ip2, not rbx-ip
    'RBXIDCHECK': 'b4867215-71b0-4098-90f9-fde0eabb97b4'
    # rblx-save-state is not available in your current cookies
}

USER_AGENT = 'Roblox/WinInet'
POLL_INTERVAL = 1  # Check each item every x seconds (increased to reduce rate-limit risk)

HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Origin': 'https://www.roblox.com',
    'Referer': 'https://www.roblox.com/'
}

# Thread-safe printing and CSRF management
print_lock = Lock()
csrf_lock = Lock()
catalog_csrf_lock = Lock()

# Global CSRF token (refreshed as needed)
CSRF_TOKEN = None
CATALOG_CSRF_TOKEN = None

# Cache collectibleItemId per asset to reduce catalog calls
collectible_id_cache = {}
collectible_cache_lock = Lock()

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

def get_balance():
    """Get current Robux balance"""
    url = "https://economy.roblox.com/v1/user/currency"
    try:
        response = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=10)
        response.raise_for_status()
        return response.json().get('robux', 0)
    except requests.exceptions.RequestException as e:
        with print_lock:
            print(f"Error fetching balance: {e}")
        return 0

def get_authenticated_user_id():
    """Get the current authenticated user id"""
    url = "https://users.roblox.com/v1/users/authenticated"
    try:
        response = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=10)
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
    global CATALOG_CSRF_TOKEN
    with catalog_csrf_lock:
        if CATALOG_CSRF_TOKEN:
            return CATALOG_CSRF_TOKEN
        try:
            res = requests.post(
                "https://catalog.roblox.com/v1/catalog/items/details",
                headers=HEADERS,
                cookies=COOKIES,
                json={"items": []},
                timeout=10
            )
            token = res.headers.get('x-csrf-token')
            if token:
                CATALOG_CSRF_TOKEN = token
            return token
        except requests.exceptions.RequestException as e:
            with print_lock:
                print(f"Error getting catalog CSRF token: {e}")
            return None

def get_collectible_item_id(asset_id):
    """Get collectibleItemId for a classic limited asset ID"""
    global CATALOG_CSRF_TOKEN
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
        res = requests.post(url, headers=headers, cookies=COOKIES, json=payload, timeout=10)
        if res.status_code == 403:
            new_token = res.headers.get('x-csrf-token')
            if new_token:
                CATALOG_CSRF_TOKEN = new_token
                headers['x-csrf-token'] = new_token
                res = requests.post(url, headers=headers, cookies=COOKIES, json=payload, timeout=10)
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

def get_resellers(asset_id, limit=100, max_pages=1):
    """
    Fetch reseller listings via the marketplace-sales API.
    Returns list of (collectibleProductId, collectibleItemInstanceId, price, seller_id, seller_type) tuples.
    """
    collectible_id = get_collectible_item_id(asset_id)
    if not collectible_id:
        return []
    url = f"https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resellers"
    uaids = []
    cursor = None
    pages = 0
    try:
        while pages < max_pages:
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            res = requests.get(url, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()
            for item in data.get('data', []):
                product_id = item.get('collectibleProductId')
                instance_id = item.get('collectibleItemInstanceId')
                price = item.get('price')
                seller = item.get('seller') or {}
                seller_id = seller.get('sellerId')
                seller_type = seller.get('sellerType')
                if product_id and instance_id and price is not None:
                    uaids.append((product_id, instance_id, price, seller_id, seller_type))
            cursor = data.get('nextPageCursor')
            pages += 1
            if not cursor:
                break
        return uaids
    except requests.exceptions.RequestException as e:
        with print_lock:
            print(f"Error fetching reseller list: {e}")
        return []

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
        res = requests.post(url, headers=HEADERS, cookies=COOKIES, json=payload, timeout=10)
        if res.status_code == 403:
            new_token = res.headers.get('x-csrf-token')
            if new_token:
                headers = dict(HEADERS)
                headers['x-csrf-token'] = new_token
                res = requests.post(url, headers=headers, cookies=COOKIES, json=payload, timeout=10)
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

# STEP 3: Check Balance
def get_xcsrf():
    """Retrieve CSRF token required for purchases (thread-safe)"""
    global CSRF_TOKEN
    with csrf_lock:
        try:
            res = requests.post('https://auth.roblox.com/v2/logout', headers=HEADERS, cookies=COOKIES, timeout=10)
            token = res.headers.get('x-csrf-token')
            if not token:
                with print_lock:
                    print("Failed to retrieve CSRF token. Authentication may have failed.")
                return False
            CSRF_TOKEN = token
            HEADERS['x-csrf-token'] = token
            with print_lock:
                print(f"[AUTH] CSRF token refreshed: {token[:20]}...")
            return True
        except requests.exceptions.RequestException as e:
            with print_lock:
                print(f"Error getting CSRF token: {e}")
            return False

# STEP 4: Attempt Purchase
def buy_item(user_asset_id, expected_price):
    """Attempt to purchase an item"""
    global CSRF_TOKEN
    url = f"https://economy.roblox.com/v1/purchases/products/{user_asset_id}"
    payload = {
        "expectedCurrency": 1,
        "expectedPrice": expected_price,
        "expectedSellerId": None
    }
    try:
        res = requests.post(url, headers=HEADERS, cookies=COOKIES, json=payload, timeout=10)
        
        # Handle 403 Forbidden - CSRF token may have expired
        if res.status_code == 403:
            with print_lock:
                print("    [AUTH] Received 403 Forbidden - CSRF token expired, refreshing...")
            new_token = res.headers.get('x-csrf-token')
            if new_token:
                CSRF_TOKEN = new_token
                HEADERS['x-csrf-token'] = new_token
                # Retry purchase with new token
                res = requests.post(url, headers=HEADERS, cookies=COOKIES, json=payload, timeout=10)
            else:
                with print_lock:
                    print("    Failed to refresh CSRF token")
                return None
        
        res.raise_for_status()
        result = res.json()
        
        # Check if purchase was successful
        if result.get('purchased'):
            return result
        else:
            with print_lock:
                print(f"    Purchase rejected: {result.get('errorMsg', 'Unknown error')}")
            return None
            
    except requests.exceptions.RequestException as e:
        with print_lock:
            print(f"    Error during purchase: {e}")
        return None

# MAIN LOGIC
def snipe_once(asset_id, item_name, discount_threshold):
    """Single snipe attempt (legacy function - use snipe_loop for continuous sniping)"""
    with print_lock:
        print(f"\nStarting snipe for {item_name}...")
    
    if not get_xcsrf():
        with print_lock:
            print("Failed to authenticate. Check your cookie.")
        return False
    
    robux = get_balance()
    with print_lock:
        print(f"Current balance: {robux} Robux")
    
    # Fetch Rolimons value
    rolimons_value = get_rolimons_value(asset_id)
    if rolimons_value is None:
        with print_lock:
            print("Could not fetch Rolimons value. Aborting for safety.")
        return False
    
    max_buy_price = int(rolimons_value * discount_threshold)
    with print_lock:
        print(f"Rolimons value: {rolimons_value} Robux")
        print(f"Max buy price ({int(discount_threshold*100)}% discount): {max_buy_price} Robux")
    
    uaids = get_resellers(asset_id)
    if not uaids:
        with print_lock:
            print("No resellers found or failed to fetch data.")
        return False
    
    with print_lock:
        print(f"Found {len(uaids)} resellers. Checking prices...")
    
    for product_id, instance_id, price, seller_id, seller_type in uaids:
        if price > max_buy_price:
            with print_lock:
                print(f"Price {price} exceeds threshold ({max_buy_price}). Skipping.")
            continue
        
        if robux >= price:
            with print_lock:
                print(f"✓ Attempting to buy Listing ID {instance_id} at {price} Robux (Value: {rolimons_value}, Discount: {(1 - price/rolimons_value)*100:.1f}%)")
            if is_uuid(instance_id):
                if seller_id is None:
                    with print_lock:
                        print("    Purchase skipped: missing seller id for collectible listing.")
                else:
                    result = buy_collectible(asset_id, product_id, instance_id, price, seller_id, seller_type)
                    if result:
                        with print_lock:
                            print(f"✓ Purchase response: {result}")
                        return True
                    else:
                        with print_lock:
                            print(f"✗ Purchase failed for Listing ID {instance_id}")
            else:
                result = buy_item(instance_id, price)
                if result:
                    with print_lock:
                        print(f"✓ Purchase successful! Result: {result}")
                    return True
                else:
                    with print_lock:
                        print(f"✗ Purchase failed for Listing ID {instance_id}")
    
    with print_lock:
        print("No affordable copies found within discount threshold.")
    return False

def snipe_loop(asset_id, item_name, discount_threshold, interval=None):
    """Continuously attempt to snipe a single item (runs in dedicated thread)"""
    if interval is None:
        interval = POLL_INTERVAL
    
    attempt = 0
    last_cheapest = None
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
        
        # Fetch resellers via marketplace-sales API
        prices = get_resellers(asset_id, limit=100, max_pages=1)
        
        if not prices:
            with print_lock:
                print(f"[{item_name}] Attempt {attempt}: Could not fetch prices for resellers")
            time.sleep(interval)
            continue
        
        prices.sort(key=lambda x: x[2])
        cheapest_product_id, cheapest_instance_id, cheapest_price, cheapest_seller_id, cheapest_seller_type = prices[0]
        
        # Only log if price changed (reduce spam)
        if last_cheapest != (cheapest_product_id, cheapest_instance_id, cheapest_price):
            with print_lock:
                print(f"[{item_name}] Attempt {attempt}: Cheapest Resale Listing {cheapest_instance_id} @ {cheapest_price} Robux")
            last_cheapest = (cheapest_product_id, cheapest_instance_id, cheapest_price)
        
        # Check if we should buy (cache Rolimons value to avoid excessive API calls)
        current_time = time.time()
        if current_time - rolimons_cache['last_fetch'] > 30:  # Refresh every 30 seconds
            rolimons_value = get_rolimons_value(asset_id)
            if rolimons_value is None:
                with print_lock:
                    print(f"[{item_name}] Could not refresh Rolimons value, using cached value...")
                if rolimons_cache['value'] is None:
                    time.sleep(interval)
                    continue
                rolimons_value = rolimons_cache['value']
            else:
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
        
        if cheapest_price <= max_buy_price:
            robux = get_balance()
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
                                discount_pct = (1 - cheapest_price/rolimons_value)*100 if rolimons_value > 0 else 0
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
                    result = buy_item(cheapest_instance_id, cheapest_price)
                    if result and result.get('purchased'):
                        with print_lock:
                            discount_pct = (1 - cheapest_price/rolimons_value)*100 if rolimons_value > 0 else 0
                            print(f"✓✓✓ PURCHASE SUCCESSFUL FOR {item_name}!")
                            print(f"    Item: {item_name}")
                            print(f"    Listing ID: {cheapest_instance_id}")
                            print(f"    Price: {cheapest_price} Robux")
                            print(f"    Discount: {discount_pct:.1f}% off Rolimons")
                        break
                    else:
                        with print_lock:
                            print(f"[{item_name}] Purchase failed, continuing snipe...")
            else:
                with print_lock:
                    print(f"[{item_name}] Insufficient Robux. Need {cheapest_price}, have {robux}")
        
        time.sleep(interval)

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
    if not get_xcsrf():
        print("\n❌ FATAL: Authentication failed. Check your cookies!")
        print("   Your .ROBLOSECURITY cookie may be expired or invalid.")
        sys.exit(1)
    
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
            args=(item['asset_id'], item['name'], item['discount_threshold']),
            daemon=True
        )
        thread.start()
        snipe_threads.append(thread)
        time.sleep(0.1)  # Stagger thread starts
    
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
