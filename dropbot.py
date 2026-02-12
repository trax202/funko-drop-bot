"""
FUNKO UK DROP BOT (Advanced)
Monitors: Funko Europe, Smyths, Forbidden Planet, GAME, HMV
Alerts for: "anime + hard/limited", in-stock flips, price changes, ultra-rare signals, Funko web exclusives.

How it works:
- Grabs listing pages (new/exclusives/animation/search pages)
- Extracts product cards (title + url + optional price)
- Visits product pages for candidates to confirm:
  - stock status (in stock / preorder vs oos)
  - price (for price-change alerts)
  - Funko exclusive/limited signals (for Funko, often not in title)
  - ultra-rare signals (LE counts like 3000, 5000, 1000, etc)

Run options:
- Locally: `python dropbot.py`
- GitHub Actions schedule: recommended every 2–3 minutes

ENV:
- DISCORD_WEBHOOK (required for Discord alerts)

NOTE:
- Keep your check frequency reasonable (2–5 mins) so stores don’t block you.
"""

import os
import re
import json
import time
import hashlib
from urllib.parse import urljoin

import urllib.request
from playwright.sync_api import sync_playwright

# -------------------------
# CONFIG
# -------------------------

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

STATE_FILE = "state.json"

# Retailers you asked for (plus pages that tend to update with drops).
# You can add/remove URLs without changing the rest of the script.
TARGETS = [
    # Funko Europe (UK)
    {
        "name": "Funko UK – New Releases",
        "url": "https://funko.com/gb/new-featured/new-releases/",
        "base": "https://funko.com",
        "type": "listing",
    },

    # Forbidden Planet (main site promo "latest")
    {
        "name": "Forbidden Planet – Funko Latest",
        "url": "https://forbiddenplanet.com/promotion/funko-see--latest/",
        "base": "https://forbiddenplanet.com",
        "type": "listing",
    },
    # Forbidden Planet International shop (often cleaner structure)
    {
        "name": "Forbidden Planet Int'l – Funko Exclusives",
        "url": "https://shop.forbiddenplanet.co.uk/collections/funko-exclusives",
        "base": "https://shop.forbiddenplanet.co.uk",
        "type": "listing",
    },

    # GAME
    {
        "name": "GAME – Pop Animation",
        "url": "https://www.game.co.uk/funko/pop-animation",
        "base": "https://www.game.co.uk",
        "type": "listing",
    },

    # HMV
    {
        "name": "HMV – Funko Pre-orders",
        "url": "https://hmv.com/store/pop-culture/funko-pre-orders",
        "base": "https://hmv.com",
        "type": "listing",
    },
    {
        "name": "HMV – Pop Vinyl Animation",
        "url": "https://hmv.com/store/pop-culture/funko/pop-vinyl/animation",
        "base": "https://hmv.com",
        "type": "listing",
    },

    # Smyths (search is the most stable “feed” Smyths gives)
    {
        "name": "Smyths – Funko Search",
        "url": "https://www.smythstoys.com/uk/en-gb/search?q=funko+pop",
        "base": "https://www.smythstoys.com",
        "type": "listing",
    },
]

# Anime signals (broad). Add/remove series as you like.
ANIME_KEYWORDS = [
    "anime", "manga",
    "one piece", "naruto", "boruto", "bleach",
    "dragon ball", "dbz", "dragonball",
    "jujutsu", "jjk", "jujutsu kaisen",
    "demon slayer", "kimetsu",
    "chainsaw", "chainsaw man",
    "spy x family", "spyxfamily",
    "my hero", "mha", "my hero academia",
    "attack on titan", "aot",
    "hunter x hunter", "hxh",
    "black clover",
    "haikyuu",
    "jojo", "jojo's",
    "tokyo ghoul",
    "sailor moon",
    "yu-gi-oh", "yugioh",
    "inuyasha",
    "fullmetal", "fma", "fullmetal alchemist",
    "evangelion",
    "gundam",
    "ghibli", "studio ghibli",
    "pokemon",  # keep/remove depending on your definition of "anime"
]

# “Hard/limited” signals (title-based)
HARD_KEYWORDS = [
    "exclusive",
    "limited",
    "chase",
    "convention",
    "sdcc",
    "nycc",
    "funko shop",
    "web exclusive",
    "special edition",
    "glow", "gitd", "glow-in-the-dark",
    "flocked",
    "metallic",
    "diamond",
    "signed",
]

# Ultra-rare thresholds (piece counts)
# You asked “ultra rare alert” — this will ping separately if it finds a LE count <= ULTRA_RARE_MAX.
ULTRA_RARE_MAX = 2500

# If a page doesn’t show prices, we attempt on product page.
CURRENCY_RE = re.compile(r"(£\s?\d+(?:\.\d{2})?)|(\d+(?:\.\d{2})?\s?£)")

# -------------------------
# HELPERS
# -------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"items": {}, "targets": {}}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def send_discord(messages: list[str]) -> None:
    if not messages:
        return
    if not DISCORD_WEBHOOK:
        print("\n".join(messages))
        return

    # Discord has message limits; chunk if needed
    chunk = ""
    chunks = []
    for m in messages:
        if len(chunk) + len(m) + 2 > 1800:
            chunks.append(chunk)
            chunk = ""
        chunk += (m + "\n\n")
    if chunk.strip():
        chunks.append(chunk)

    for c in chunks:
        data = json.dumps({"content": c.strip()}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "funko-drop-bot/2.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp.read()

def looks_like_product_url(u: str) -> bool:
    u = u.lower()
    return any(x in u for x in ["/products/", "/product/", "/p/", "/store/"]) and not any(x in u for x in ["/search", "/promotion", "/collections", "/category"])

def extract_price_text(text: str) -> str | None:
    if not text:
        return None
    m = CURRENCY_RE.search(text.replace("\n", " "))
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip()

def price_to_float(price_text: str | None) -> float | None:
    if not price_text:
        return None
    p = price_text.replace("£", "").strip()
    try:
        return float(p)
    except Exception:
        return None

def matches_anime_and_hard(title: str) -> bool:
    return True

# def matches_anime_and_hard(title: str) -> bool:
  #  t = norm(title)
   # has_anime = any(k in t for k in ANIME_KEYWORDS)
    #has_hard = any(k in t for k in HARD_KEYWORDS)
    #return has_anime and has_hard

def stock_status_from_page_html(html_lower: str) -> str:
    """
    Returns: "in_stock", "oos", "unknown"
    """
    in_stock_phrases = [
        "add to basket",
        "add to cart",
        "add to bag",
        "add to trolley",
        "buy now",
        "pre-order",
        "preorder",
        "available for delivery",
        "available to collect",
        "in stock",
    ]

    oos_phrases = [
        "out of stock",
        "sold out",
        "currently unavailable",
        "not available",
        "temporarily unavailable",
        "no longer available",
    ]

    if any(p in html_lower for p in oos_phrases):
        return "oos"
    if any(p in html_lower for p in in_stock_phrases):
        return "in_stock"
    return "unknown"

def funko_is_exclusive_or_limited(html_lower: str) -> bool:
    """
    Funko pages often include “Web Exclusive” or “Limited Edition” in page content.
    """
    signals = [
        "web exclusive",
        "funko exclusive",
        "limited edition",
        "special edition",
        "exclusive",
        "limited",
    ]
    return any(s in html_lower for s in signals)

def extract_le_piece_count(text: str) -> int | None:
    """
    Tries to find limited edition piece counts like:
      - "LE 3000"
      - "Limited Edition 3,000"
      - "5000 pcs"
      - "5000 pieces"
    Returns int if found, else None.
    """
    if not text:
        return None
    t = text.lower().replace(",", "")
    patterns = [
        r"\ble\s*(\d{3,6})\b",
        r"\blimited edition\s*(\d{3,6})\b",
        r"\b(\d{3,6})\s*(?:pcs|pieces)\b",
        r"\bmax\.*\s*(\d{2,6})\b",  # sometimes "Max 99"
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None

def stable_item_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

def extract_listing_items(page, base_url: str) -> list[dict]:
    """
    Best-effort extraction:
    - Collect anchors with href
    - Prefer product-looking URLs
    - Title: anchor text
    - Price: tries nearby text via parent node (best effort)
    """
    items = []
    anchors = page.locator("a[href]").all()

    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
            text = (a.inner_text() or "").strip()
        except Exception:
            continue

        if not href:
            continue

        abs_url = href
        if href.startswith("/"):
            abs_url = urljoin(base_url, href)
        abs_url = abs_url.split("?")[0]

        if not looks_like_product_url(abs_url):
            continue

        # Skip super-short non-titles
        if not text or len(text) < 3:
            continue

        # Try to get a price from nearby DOM (parent text)
        price_text = None
        try:
            parent_text = a.locator("xpath=..").inner_text(timeout=250)
            price_text = extract_price_text(parent_text)
        except Exception:
            pass

        items.append(
            {
                "title": text,
                "url": abs_url,
                "listing_price_text": price_text,
            }
        )

    # Deduplicate by URL
    dedup = {}
    for it in items:
        dedup[it["url"]] = it
    return list(dedup.values())

# -------------------------
# MAIN
# -------------------------

def main():
    state = load_state()
    state.setdefault("items", {})
    state.setdefault("targets", {})

    alerts_new = []
    alerts_stock = []
    alerts_price = []
    alerts_ultra = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
        )

        for target in TARGETS:
            name = target["name"]
            url = target["url"]
            base = target["base"]

            print(f"Checking: {name} -> {url}")
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1500)
            except Exception as e:
                print(f"Failed load: {name}: {e}")
                page.close()
                continue

            items = extract_listing_items(page, base)
            page.close()

            # Track target-level digest (useful for debugging + change tracking)
            combined = "\n".join(sorted([f"{i['title']}|{i['url']}|{i.get('listing_price_text') or ''}" for i in items]))
            digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()
            prev_digest = state["targets"].get(name, {}).get("digest")

            state["targets"][name] = {
                "digest": digest,
                "last_checked": int(time.time()),
                "url": url,
                "changed": bool(prev_digest and prev_digest != digest),
            }

            # For each listing item:
            for it in items:
                title = it["title"]
                product_url = it["url"]
                item_id = stable_item_id(product_url)

                # Basic anime signal from title (broad)
                tnorm = norm(title)
                is_anime = any(k in tnorm for k in ANIME_KEYWORDS)

                # We only deep-check if it is anime-ish OR contains hard keywords
                # (keeps runtime lower and avoids too many product page loads)
                is_hard_title = any(k in tnorm for k in HARD_KEYWORDS)
                should_open = is_anime and (is_hard_title or "funko.com" in product_url)

                # Initialise state record
                prev = state["items"].get(item_id, {})
                first_seen = prev.get("first_seen", int(time.time()))
                prev_seen_urls = prev.get("url") == product_url

                # Save minimal immediately (so we keep memory of this item)
                state["items"][item_id] = {
                    "id": item_id,
                    "title": title,
                    "url": product_url,
                    "source": name,
                    "first_seen": first_seen,
                    "last_seen": int(time.time()),
                    "listing_price_text": it.get("listing_price_text"),
                    # product-level fields will be updated below if we open product page
                    "last_price": prev.get("last_price"),
                    "last_stock": prev.get("last_stock"),
                    "last_le_count": prev.get("last_le_count"),
                    "last_funko_exclusive": prev.get("last_funko_exclusive"),
                }

                # NEW LISTING ALERT (only if it never existed before)
                if not prev:
                    # We only alert new listing immediately if it already passes anime+hard title check.
                    # Funko is handled on product page because titles can be vague.
                    if matches_anime_and_hard(title):
                        alerts_new.append(f"🆕 **NEW LISTING** ({name})\n**{title}**\n{product_url}")

                if not should_open:
                    continue

                # Open product page to confirm stock/price/exclusive/le-count
                product_page = context.new_page()
                try:
                    product_page.goto(product_url, wait_until="domcontentloaded", timeout=60000)
                    product_page.wait_for_timeout(1200)
                    html_lower = product_page.content().lower()

                    # STOCK
                    stock = stock_status_from_page_html(html_lower)

                    # PRICE: best-effort from product page text
                    page_text = ""
                    try:
                        page_text = product_page.inner_text("body", timeout=1500)
                    except Exception:
                        page_text = ""

                    page_price_text = extract_price_text(page_text) or extract_price_text(it.get("listing_price_text") or "")
                    page_price_val = price_to_float(page_price_text)

                    # FUNKO exclusive/limited signals (product page)
                    is_funko = "funko.com" in product_url
                    funko_excl = funko_is_exclusive_or_limited(html_lower) if is_funko else None

                    # LE piece count (ultra rare)
                    le_count = extract_le_piece_count(page_text) or extract_le_piece_count(title)

                    # Save product-level fields
                    rec = state["items"][item_id]
                    rec["last_stock"] = stock
                    rec["last_price"] = page_price_val
                    rec["last_price_text"] = page_price_text
                    rec["last_funko_exclusive"] = funko_excl
                    rec["last_le_count"] = le_count

                    prev_stock = prev.get("last_stock")
                    prev_price = prev.get("last_price")
                    prev_le = prev.get("last_le_count")
                    prev_funko_excl = prev.get("last_funko_exclusive")

                    # Decide whether this item qualifies as "anime + hard"
                    # For Funko: allow anime + exclusive detected on page (even if title lacks "exclusive/limited")
                    qualifies = False
                    if is_funko:
                        qualifies = is_anime and bool(funko_excl)
                    else:
                        qualifies = matches_anime_and_hard(title)

                    # 1) IN-STOCK FLIP ALERT
                    if qualifies:
                        if prev_stock in (None, "oos", "unknown") and stock == "in_stock":
                            alerts_stock.append(
                                f"✅ **IN STOCK** ({name})\n**{title}**\n{product_url}\n"
                                f"{('Price: £' + str(page_price_val)) if page_price_val is not None else ''}"
                            )

                    # 2) PRICE CHANGE ALERT
                    # Trigger when we have a previous price and it changes by at least 1p (basic)
                    if qualifies and page_price_val is not None and prev_price is not None and page_price_val != prev_price:
                        direction = "⬆️" if page_price_val > prev_price else "⬇️"
                        alerts_price.append(
                            f"{direction} **PRICE CHANGE** ({name})\n**{title}**\n{product_url}\n"
                            f"Was: £{prev_price:.2f}  Now: £{page_price_val:.2f}"
                        )

                    # 3) FUNKO “exclusive detected” NEW ALERT
                    # If previously not exclusive, now detected (helps catch Funko web exclusives fast)
                    if is_funko and is_anime and funko_excl and not prev_funko_excl:
                        alerts_new.append(
                            f"🎯 **FUNKO EXCLUSIVE/LIMITED DETECTED**\n**{title}**\n{product_url}"
                        )

                    # 4) ULTRA RARE ALERT
                    # Ping if LE count <= ULTRA_RARE_MAX and this is a qualifying item
                    if qualifies and le_count is not None and le_count <= ULTRA_RARE_MAX:
                        # Only alert if we haven't alerted for this LE count before (prevents repeats)
                        if prev_le != le_count:
                            alerts_ultra.append(
                                f"🚨 **ULTRA RARE SIGNAL (LE {le_count})** ({name})\n**{title}**\n{product_url}\n"
                                f"{'In Stock ✅' if stock == 'in_stock' else 'Not In Stock yet'}"
                            )

                except Exception as e:
                    print(f"Product page error: {product_url}: {e}")
                finally:
                    product_page.close()

        browser.close()

    # Cleanup: keep state from growing forever (optional)
    # Keep only last 8000 items
    if len(state["items"]) > 8000:
        # sort by last_seen and keep newest
        items_sorted = sorted(state["items"].items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)
        state["items"] = dict(items_sorted[:8000])

    save_state(state)

    # Send alerts (order matters: ultra rare + in stock first)
    outgoing = []
    outgoing.extend(alerts_ultra)
    outgoing.extend(alerts_stock)
    outgoing.extend(alerts_price)
    outgoing.extend(alerts_new)

    if outgoing:
        send_discord(outgoing)
        print(f"Sent alerts: {len(outgoing)}")
    else:
        print("No alerts this run.")

if __name__ == "__main__":
    main()
