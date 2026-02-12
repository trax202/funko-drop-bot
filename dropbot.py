import os, json, re, hashlib, time
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright
import urllib.request

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

# Pages we’ll watch (tune these anytime)
TARGETS = [
    # Funko EU (UK) – new releases
    {"name": "Funko UK – New Releases", "url": "https://funko.com/gb/new-featured/new-releases/", "base": "https://funko.com"},
    # Forbidden Planet (main site promo page)
    {"name": "Forbidden Planet – Funko Latest", "url": "https://forbiddenplanet.com/promotion/funko-see--latest/", "base": "https://forbiddenplanet.com"},
    # Forbidden Planet International shop (often easier to parse)
    {"name": "Forbidden Planet Int’l – Funko Exclusives", "url": "https://shop.forbiddenplanet.co.uk/collections/funko-exclusives", "base": "https://shop.forbiddenplanet.co.uk"},
    # GAME animation category
    {"name": "GAME – Pop Animation", "url": "https://www.game.co.uk/funko/pop-animation", "base": "https://www.game.co.uk"},
    # Smyths and HMV are trickier; you can add specific Smyths search pages + HMV preorder pages you care about.
]

# “Hard / limited” signals (keep these!)
HARD_KEYWORDS = [
    "exclusive", "limited", "chase", "convention", "sdcc", "nycc", "glow", "gitd",
    "flocked", "metallic", "diamond", "special edition", "funko shop", "web exclusive"
]

# “Anime-ish” signals. This is intentionally broad.
ANIME_KEYWORDS = [
    "anime", "manga",
    "one piece", "naruto", "boruto", "bleach", "dragon ball", "dbz", "jujutsu", "jjk",
    "demon slayer", "kimetsu", "chainsaw", "spy x family", "my hero", "mha", "attack on titan",
    "aot", "hunter x hunter", "hxh", "black clover", "haikyuu", "jojo", "tokyo ghoul",
    "sailor moon", "yu-gi-oh", "inuyasha", "fullmetal", "fma", "evangelion", "gundam",
    "studio ghibli", "ghibli", "pokemon"  # optional: remove if you don’t want it
]

STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def matches_filters(title: str) -> bool:
    t = norm(title)
    has_anime = any(k in t for k in ANIME_KEYWORDS)
    has_hard = any(k in t for k in HARD_KEYWORDS)
    # If you truly want "all anime", keep has_anime.
    # If you want *only* limited/hard anime, require both:
    return has_anime and has_hard

def send_discord(message: str):
    if not DISCORD_WEBHOOK:
        print("No DISCORD_WEBHOOK set; printing instead:\n", message)
        return
    data = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "funko-drop-bot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()

def extract_links(page, base_url: str):
    # Collect product-ish links + their visible text
    anchors = page.locator("a").all()
    items = []
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
            text = a.inner_text() or ""
        except Exception:
            continue
        if not href:
            continue

        abs_url = href
        if href.startswith("/"):
            abs_url = urljoin(base_url, href)

        # Light filtering: only keep links that look like product pages or product cards
        if any(x in abs_url for x in ["/products/", "/product/", "/store/", "/p/"]) or "funko" in abs_url.lower():
            title = text.strip()
            if title and len(title) >= 3:
                items.append({"title": title, "url": abs_url.split("?")[0]})
    # Deduplicate by url
    dedup = {}
    for it in items:
        dedup[it["url"]] = it
    return list(dedup.values())

def main():
    state = load_state()

    alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
        )

        for target in TARGETS:
            name, url, base = target["name"], target["url"], target["base"]
            print(f"Checking: {name} -> {url}")

            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)  # small settle
            except Exception as e:
                print(f"Failed to load {url}: {e}")
                page.close()
                continue

            found = extract_links(page, base)
            page.close()

            # Hash the set to detect big changes (backup mechanism)
            combined = "\n".join(sorted([f"{i['title']}|{i['url']}" for i in found]))
            digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()

            prev_digest = state.get(name, {}).get("digest")
            prev_seen = set(state.get(name, {}).get("seen_urls", []))

            # Find new URLs
            new_items = [i for i in found if i["url"] not in prev_seen]

            # Filter to “limited/hard anime”
            filtered = [i for i in new_items if matches_filters(i["title"])]

            if filtered:
                for i in filtered[:15]:
                    alerts.append(f"🆕 **{name}**\n**{i['title']}**\n{i['url']}")

            # Update state
            state[name] = {
                "digest": digest,
                "seen_urls": list(set(prev_seen).union({i["url"] for i in found}))[:5000],
                "last_checked": int(time.time()),
            }

        browser.close()

    save_state(state)

    if alerts:
        msg = "\n\n".join(alerts)
        send_discord(msg)
        print("Sent alerts:", len(alerts))
    else:
        print("No matching drops.")

if __name__ == "__main__":
    main()
