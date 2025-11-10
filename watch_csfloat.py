import os, time, json, requests
from statistics import mean
from urllib.parse import quote_plus
from dotenv import load_dotenv

# ------------- Config -------------
INTERVAL_S   = 60       # pause between full passes
PAGE_LIMIT   = 50       # API caps; we’ll auto-try 25, then 10 if needed
DISCOUNT     = 0     # 10% under batch average
MIN_SAMPLES  = 2        # minimum listings to form an average
TYPE_FILTER  = "buy_now"
WATCHLIST_FN = "watchlist.txt"
SEEN_FILE    = "seen_ids.json"
# ----------------------------------

load_dotenv()
API_KEY = os.getenv("CSFLOAT_API_KEY", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip() or None
if not API_KEY:
    raise SystemExit("Missing CSFLOAT_API_KEY in .env")

S = requests.Session()
S.headers.update({
    "Authorization": API_KEY,              # raw key (works for you)
    "Accept": "application/json",
    "User-Agent": "csfloat-watcher/per-item-average-links-1.0"
})

def notify(msg: str):
    print(msg, flush=True)
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
        except Exception as e:
            print(f"[webhook error] {e}")

def load_watchlist(path=WATCHLIST_FN):
    items = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    items.append(line)
    return items

def load_seen(path=SEEN_FILE):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def save_seen(seen, path=SEEN_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(seen), f)
    except Exception:
        pass

def _parse_list(data):
    """Handle various response shapes; prefer 'data' key."""
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            return data["data"]
        for k in ("listings", "results", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []

def fetch_item_listings(name: str, start_limit: int = PAGE_LIMIT):
    """
    Fetch listings for a single market_hash_name with robust fallbacks and limit downgrades.
    Tries param combos across limits [start, 25, 10] until success.
    """
    limits = [start_limit, 25, 10] if start_limit not in (25, 10) else [start_limit, 10]
    param_variants = [
        {"market_hash_name": name, "type": TYPE_FILTER, "sort_by": "lowest_price"},
        {"market_hash_name": name, "type": TYPE_FILTER},
        {"market_hash_name": name},
    ]
    last_exc = None
    for lim in limits:
        for pv in param_variants:
            params = dict(pv)
            params["limit"] = lim
            r = S.get("https://csfloat.com/api/v1/listings", params=params, timeout=15)
            if r.status_code == 200:
                return _parse_list(r.json() if r.content else {})
            # Show short debug and keep trying
            body = r.text[:200].replace("\n", " ").strip() if r.text else ""
            print(f"[DEBUG {name}] HTTP {r.status_code} for {params} :: {body}")
            last_exc = requests.HTTPError(f"{r.status_code} for params {params}", response=r)
            time.sleep(0.25)
    # All attempts failed
    raise last_exc or requests.HTTPError("Failed to fetch listings")

def process_item(name: str, seen_ids: set) -> int:
    """Compute batch average for this item; alert if any listing <= 90% of avg. Returns alert count."""
    try:
        listings = fetch_item_listings(name)
    except requests.HTTPError as e:
        code = e.response.status_code if getattr(e, "response", None) is not None else "?"
        print(f"[{name}] HTTP {code} – skipping this cycle.")
        return 0
    except Exception as e:
        print(f"[{name}] error: {e}")
        return 0

    prices = []
    normalized = []  # (lid, price_usd, wear, float_txt, search_url, stall_url, inspect)
    qname = quote_plus(name)
    search_url = f"https://csfloat.com/search?market_hash_name={qname}&type={TYPE_FILTER}&sort_by=lowest_price"

    for L in listings:
        if not isinstance(L, dict):
            continue
        lid = L.get("id")
        price_usd = (L.get("price", 0) or 0) / 100.0
        if price_usd <= 0:
            continue
        item = L.get("item", {}) or {}
        wear = item.get("wear_name", "?")
        fval = item.get("float_value")
        ftxt = f"{fval:.5f}" if isinstance(fval, (int, float)) else "?"
        inspect = item.get("inspect_link") or ""
        seller_obj = L.get("seller") or {}
        seller_id = seller_obj.get("steam_id") or ""
        stall_url = f"https://csfloat.com/stall/{seller_id}" if seller_id else ""
        normalized.append((lid, price_usd, wear, ftxt, search_url, stall_url, inspect))
        prices.append(price_usd)

    if len(prices) < MIN_SAMPLES:
        print(f"[{name}] only {len(prices)} listing(s); need {MIN_SAMPLES} for avg.")
        return 0

    avg_price = mean(prices)
    threshold = avg_price * (1.0 - DISCOUNT)

    alerts = 0
    for lid, price_usd, wear, ftxt, s_url, stall_url, inspect in normalized:
        if not lid or lid in seen_ids:
            continue
        if price_usd <= threshold:
            drop = (avg_price - price_usd) / avg_price * 100.0
            msg_lines = [
                f"🔔 Deal: {name} [{wear}] ${price_usd:.2f} (↓{drop:.1f}% vs batch avg ${avg_price:.2f})",
                f"float {ftxt}",
                f"Search: {s_url}",
            ]
            if stall_url:
                msg_lines.append(f"Seller: {stall_url}")
            if inspect:
                msg_lines.append(f"Inspect: {inspect}")
            notify("\n".join(msg_lines))
            alerts += 1
        seen_ids.add(lid)

    return alerts

def main():
    seen_ids = load_seen()
    notify("CSFloat watcher (per-item batch average, 10% under) started.")
    if not os.path.exists(WATCHLIST_FN):
        notify(f"Create {WATCHLIST_FN} (one item per line, exact market_hash_name incl. wear).")

    while True:
        items = load_watchlist()
        if not items:
            time.sleep(INTERVAL_S)
            continue

        total_alerts = 0
        for name in items:
            total_alerts += process_item(name, seen_ids)

        if total_alerts:
            save_seen(seen_ids)

        time.sleep(INTERVAL_S)

if __name__ == "__main__":
    main()
