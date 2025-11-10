import os, time, json, requests
from statistics import median
from urllib.parse import quote_plus
from dotenv import load_dotenv

# =============== Config ===============
INTERVAL_S        = 60        # used only when running locally (loop mode)
PAGE_LIMIT        = 50        # per-page auction fetch; will auto-downgrade to 25/10
PAGES             = 3         # how many auction pages to scan (with cursor)
MAX_REF_ITEMS     = 30        # cap unique items we compute buy-now refs for per run
MIN_SAMPLES       = 3         # min buy-now listings to form a reference price
PROFIT_UNDER      = 0.12      # bid must be >=12% under reference (i.e., <= 88% of ref)
MAX_BID_USD       = 1500.0    # ignore silly expensive auctions

# anti-spam: notify once at each stage per listing id
STAGE_10M         = "t10"
STAGE_5M          = "t5"

# Discord batching / formatting
TOP_N_GLOBAL      = 10        # keep only the top N deals this run
FIELDS_PER_EMBED  = 10
EMBED_COLOR       = 0x2F855A

TYPE_BUY_NOW      = "buy_now"
TYPE_AUCTION      = "auction"
STATE_FILE        = "auction_state.json"  # listing_id -> "t10"/"t5"
# ======================================

load_dotenv()
API_KEY = os.getenv("CSFLOAT_API_KEY", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip() or None
SINGLE_PASS = os.getenv("SINGLE_PASS", "0") == "1"
if not API_KEY:
    raise SystemExit("Missing CSFLOAT_API_KEY")

S = requests.Session()
S.headers.update({
    "Authorization": API_KEY,  # raw key style (works for you)
    "Accept": "application/json",
    "User-Agent": "csfloat-watcher/auction-sniper-global-1.0"
})

# ---------- persistence ----------
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_state(d):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass

# ---------- utils ----------
def _parse_list(data):
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            return data["data"]
        for k in ("listings", "results", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []

def _get(url, params, timeout=15):
    r = S.get(url, params=params, timeout=timeout)
    if r.status_code == 200:
        return r.json() if r.content else {}
    # short debug
    body = r.text[:160].replace("\n", " ").strip() if r.text else ""
    print(f"[DEBUG] HTTP {r.status_code} for {params} :: {body}")
    return {}

def seconds_left(L):
    now = time.time()
    v = L.get("time_left")
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    for key in ("expires_at", "ends_at", "end_time"):
        t = L.get(key)
        if isinstance(t, (int, float)) and t > 0:
            if t > 10**12: t /= 1000.0  # ms -> s
            return max(0, int(t - now))
    if isinstance(L.get("auction"), dict):
        t = L["auction"].get("ends_at")
        if isinstance(t, (int, float)) and t > 0:
            if t > 10**12: t /= 1000.0
            return max(0, int(t - now))
    return None

def current_bid_usd(L):
    for key in ("current_bid", "current_price", "price"):
        v = L.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return v/100.0 if v > 50 else float(v)
    if isinstance(L.get("auction"), dict):
        v = L["auction"].get("current_bid")
        if isinstance(v, (int, float)) and v > 0:
            return v/100.0 if v > 50 else float(v)
    return None

def trimmed_median(prices):
    if len(prices) < MIN_SAMPLES: return None
    prices = sorted(prices)
    if len(prices) >= 6:
        k = max(1, int(len(prices)*0.10))
        prices = prices[k:-k]
    return float(median(prices)) if prices else None

def ref_price_for_item(market_name):
    # fetch buy-now prices & compute trimmed median
    for limit in (50, 25, 10):
        data = _get("https://csfloat.com/api/v1/listings", {
            "market_hash_name": market_name,
            "type": TYPE_BUY_NOW,
            "limit": limit,
            "sort_by": "lowest_price"
        })
        lst = _parse_list(data)
        prices = [(L.get("price", 0) or 0)/100.0 for L in lst if isinstance(L, dict) and (L.get("price", 0) or 0) > 0]
        if prices:
            rp = trimmed_median(prices)
            if rp: return rp
    return None

def fetch_all_auctions(limit=PAGE_LIMIT, pages=PAGES):
    """Global auction scan with cursor; ending soon first; robust to param quirks."""
    auctions = []
    cursor = None
    for _ in range(pages):
        for limit_try in (limit, 25, 10):
            params = {"type": TYPE_AUCTION, "limit": limit_try, "sort_by": "ending_soon"}
            if cursor: params["cursor"] = cursor
            data = _get("https://csfloat.com/api/v1/listings", params)
            batch = _parse_list(data)
            if batch:
                auctions.extend(batch)
                cursor = data.get("cursor")
                break
        else:
            break  # all limit tries failed
        if not cursor:
            break
    return auctions

def links_for_listing(item_name, L):
    q = quote_plus(item_name)
    search_url = f"https://csfloat.com/search?market_hash_name={q}&type={TYPE_AUCTION}&sort_by=ending_soon"
    seller = (L.get("seller") or {}).get("steam_id") or ""
    stall_url = f"https://csfloat.com/stall/{seller}" if seller else ""
    inspect = (L.get("item") or {}).get("inspect_link") or ""
    return search_url, stall_url, inspect

# ---------- discord ----------
def build_field(d):
    title = f"{d['item']} [{d['wear']}] • ⏳ {d['mins_left']}m"
    lines = [
        f"**Bid ${d['bid']:.2f}** • ↓{d['drop_pct']:.1f}% vs ref ${d['ref']:.2f}",
        f"`float {d['float']}`" if d['float'] != "?" else "`float ?`",
        f"[Search]({d['search']})" + (f" • [Seller]({d['stall']})" if d['stall'] else "")
    ]
    return {"name": title[:256], "value": "\n".join(lines)[:1024], "inline": False}

def send_embeds(deals):
    if not DISCORD_WEBHOOK:
        for d in deals:
            print(f"🔔 {d['item']} [{d['wear']}] ${d['bid']:.2f} "
                  f"(↓{d['drop_pct']:.1f}% vs ${d['ref']:.2f}) • {d['mins_left']}m left")
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for i in range(0, len(deals), FIELDS_PER_EMBED):
        chunk = deals[i:i+FIELDS_PER_EMBED]
        payload = {
            "embeds": [{
                "title": f"Auction alerts • {now}",
                "description": f"{len(chunk)} match(es) (≤10m/≤5m & profitable)",
                "color": EMBED_COLOR,
                "fields": [build_field(d) for d in chunk]
            }]
        }
        try:
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
            print("[discord]", r.status_code)
        except Exception as e:
            print("[discord error]", e)

# ---------- core ----------
def run_once():
    state = load_state()
    auctions = fetch_all_auctions()
    if not auctions:
        print("[info] no auctions fetched")
        return

    # Pre-filter by time and sanity price, and collect item names for refs
    candidates = []
    item_names = []
    for L in auctions:
        if not isinstance(L, dict): continue
        lid = str(L.get("id") or "")
        if not lid: continue
        secs = seconds_left(L)
        if secs is None: continue

        # stage windows
        if 0 <= secs <= 5*60:
            stage_needed = STAGE_5M
        elif 5*60 < secs <= 10*60:
            stage_needed = STAGE_10M
        else:
            continue

        # if this stage was already sent, skip
        if state.get(lid) == stage_needed:
            continue

        bid = current_bid_usd(L)
        if bid is None or bid <= 0 or bid > MAX_BID_USD:
            continue

        item = L.get("item") or {}
        name = item.get("market_hash_name") or ""
        if not name:
            continue

        candidates.append((L, lid, name, secs, stage_needed, bid))
        item_names.append(name)

    if not candidates:
        print("[info] no candidates in 10m/5m window")
        return

    # Compute reference prices per item (buy-now trimmed median), with a cap
    refs = {}
    unique_names = []
    for name in item_names:
        if name not in refs:
            unique_names.append(name)
    for name in unique_names[:MAX_REF_ITEMS]:
        rp = ref_price_for_item(name)
        if rp: refs[name] = rp

    deals = []
    for L, lid, name, secs, stage_needed, bid in candidates:
        ref = refs.get(name)
        if not ref:  # no reference, skip
            continue
        target = ref * (1.0 - PROFIT_UNDER)
        if bid > target:
            continue

        wear = (L.get("item") or {}).get("wear_name", "?")
        fval = (L.get("item") or {}).get("float_value")
        ftxt = f"{fval:.5f}" if isinstance(fval, (int, float)) else "?"
        search_url, stall_url, inspect = links_for_listing(name, L)

        deals.append({
            "id": lid,
            "item": name,
            "wear": wear,
            "bid": bid,
            "ref": ref,
            "drop_pct": (ref - bid)/ref*100.0,
            "mins_left": int(secs//60),
            "float": ftxt,
            "search": search_url,
            "stall": stall_url,
            "inspect": inspect,
            "stage": stage_needed
        })

    if not deals:
        print("[info] no profitable auctions in window")
        return

    # Prefer soonest, then biggest discount; keep top N globally
    deals.sort(key=lambda d: (d["mins_left"], -d["drop_pct"]))
    if TOP_N_GLOBAL and len(deals) > TOP_N_GLOBAL:
        deals = deals[:TOP_N_GLOBAL]

    # mark stages as notified
    for d in deals:
        state[d["id"]] = d["stage"]
    save_state(state)

    send_embeds(deals)

def main():
    print("CSFloat Auction Sniper (global) started.")
    if SINGLE_PASS:
        run_once(); return
    while True:
        run_once()
        time.sleep(INTERVAL_S)

if __name__ == "__main__":
    main()
