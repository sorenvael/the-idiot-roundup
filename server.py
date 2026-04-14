#!/usr/bin/env python3
"""
The Idiot Roundup — Server with Anthropic web search + Vision AI
Run:  python3 server.py
Open: http://localhost:3000
"""
 
import json, os, re, sys, time, hashlib, base64
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

PORT = 3000
TIMEOUT = 10
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# ── Your API keys (set via environment variables) ──
# For local dev, create a .env file or export them in your shell:
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   export APIFY_TOKEN="apify_api_..."
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")

# ── Master account list (all exist on both TikTok and Instagram) ─────
DEFAULT_OWN_ACCOUNTS = {
    # Zach Bryan pages
    "americanharddrive", "greatamericanbarscene", "morezachbryan", "oklahomanoutlaw",
    "runnyeggsz", "withheavenontok", "zachbryanarchive", "harleycarmichael",
    # Ella Langley pages
    "ellalangleyarchive", "ellalangleyextras", "ellalangleylately", "fellas4ella", "langleyloyalists",
    # Ole 60 pages
    "moreole60", "ole60archive", "ole60fans", "ole60vault",
    # Joshua Slone pages
    "isabelledavis97", "joshuaslonearchive", "joshuaslonenation", "morejoshuaslone",
    # Phil Kane pages
    "phil.kane.hq", "philkanehq",
    # Gabriella Rose pages
    "gabriellarosearchive",
    # Other affiliated accounts
    "barnburners", "colt.johnsontx", "folktunez", "graciekahan", "gunnarhendo",
    "harmonyandtwang", "kaylaalist", "maddiespamzzzz99", "oklahomasmokeshow02",
    "roadshowrecap", "spamrynnnn",
}

# ── Cache ────────────────────────────────────────────────────
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_TTL = 24 * 60 * 60

def cache_key(url):
    normalized = re.sub(r'\?.*$', '', url).rstrip('/').lower()
    return hashlib.md5(normalized.encode()).hexdigest()

def cache_get(url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_key(url) + ".json")
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < CACHE_TTL:
            with open(path) as f:
                data = json.load(f)
            print(f"  [cache] HIT (age: {int(age/60)}m)", file=sys.stderr)
            return data
    return None

def cache_set(url, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_key(url) + ".json")
    with open(path, "w") as f:
        json.dump(data, f)


# ── Metadata via oEmbed ──────────────────────────────────────

def get_metadata(url, platform):
    try:
        if platform == "tiktok":
            r = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(url)}", headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status(); d = r.json()
            return {"title": d.get("title", ""), "author": d.get("author_name", ""), "thumbnail_url": d.get("thumbnail_url", "")}
        if platform == "youtube":
            r = requests.get(f"https://www.youtube.com/oembed?url={quote_plus(url)}&format=json", headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status(); d = r.json()
            vid = None
            for pat in [r"shorts/([a-zA-Z0-9_-]{11})", r"v=([a-zA-Z0-9_-]{11})", r"youtu\.be/([a-zA-Z0-9_-]{11})"]:
                m = re.search(pat, url)
                if m: vid = m.group(1); break
            thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else d.get("thumbnail_url", "")
            return {"title": d.get("title", ""), "author": d.get("author_name", ""), "thumbnail_url": thumb}
        if platform == "instagram":
            r = requests.get(f"https://api.instagram.com/oembed/?url={quote_plus(url)}", headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status(); d = r.json()
            return {"title": d.get("title", "Instagram Reel"), "author": d.get("author_name", ""), "thumbnail_url": d.get("thumbnail_url", "")}
    except Exception as e:
        print(f"  [meta] oEmbed failed: {e}", file=sys.stderr)
    parts = urlparse(url).path.strip("/").split("/")
    author = parts[0].lstrip("@") if parts else "unknown"
    return {"title": f"{platform.title()} video", "author": author, "thumbnail_url": ""}


# ── Anthropic API Search (Repost Finder) ─────────────────────

SYSTEM_PROMPT = """You find reposts of viral videos and news articles about them. Return ONLY a JSON array.
Each object must have: platform (tiktok/instagram/youtube/facebook/twitter/other), account_name (@user or publication name), url (direct link), confidence (high/medium), date_found (YYYY-MM-DD).
Search thoroughly. Return ONLY the JSON array, nothing else."""

def search_with_api(url, platform, metadata):
    title = metadata.get("title", "")
    author = metadata.get("author", "")
    prompt = f"Find all reposts and news coverage of this video:\nURL: {url}\nPlatform: {platform}\nTitle: {title}\nAuthor: @{author}\nSearch for: reposts on other platforms, news articles, reaction videos. Be thorough."
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2048, "system": SYSTEM_PROMPT,
              "tools": [{"type": "web_search_20250305", "name": "web_search"}],
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    text = "\n".join(b["text"] for b in r.json().get("content", []) if b.get("type") == "text")
    results = []
    cleaned = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        try: results = json.loads(match.group(0))
        except json.JSONDecodeError: pass
    return results


# ── Helpers ──────────────────────────────────────────────────

def is_own_account(result, own_accounts=None):
    if own_accounts is None:
        own_accounts = DEFAULT_OWN_ACCOUNTS
    name = result.get("account_name", "").lstrip("@").lower()
    url = result.get("url", "").lower()
    for acct in own_accounts:
        acct = acct.lstrip("@").lower().strip()
        if not acct:
            continue
        if acct in name or f"/@{acct}" in url or f"/{acct}" in url:
            return True
    return False

def split_results(results, own_accounts=None):
    own, reposts = [], []
    for r in results:
        (own if is_own_account(r, own_accounts) else reposts).append(r)
    return own, reposts


# ── Apify helpers ────────────────────────────────────────────

def extract_stats(item):
    stats_obj = item.get("stats") or item.get("statsV2") or {}
    def pick(item, so, *keys):
        for src in [item, so]:
            for k in keys:
                v = src.get(k)
                if v is not None:
                    try:
                        val = int(v)
                        if val > 0: return val
                    except (ValueError, TypeError): pass
        return 0
    return {
        "plays": pick(item, stats_obj, "playCount", "plays", "viewCount", "views"),
        "likes": pick(item, stats_obj, "diggCount", "likes", "likeCount", "heart", "heartCount"),
        "comments": pick(item, stats_obj, "commentCount", "comments"),
        "shares": pick(item, stats_obj, "shareCount", "shares"),
    }

def parse_apify_item(item, seen_urls):
    video_url = item.get("webVideoUrl") or item.get("videoUrl") or item.get("url") or ""
    if not video_url or video_url in seen_urls:
        return None
    seen_urls.add(video_url)
    author = (item.get("authorMeta", {}).get("name", "") or item.get("author", {}).get("uniqueId", "")
              or item.get("authorName", "") or item.get("uniqueId", "") or "")
    create_time = item.get("createTime") or item.get("createTimeISO") or ""
    thumbnail = (item.get("videoMeta", {}).get("coverUrl", "") or item.get("covers", {}).get("default", "")
                 or item.get("cover", "") or item.get("originCover", "") or item.get("dynamicCover", "") or "")
    return {
        "platform": "tiktok",
        "account_name": f"@{author}" if author else "@unknown",
        "url": video_url,
        "description": (item.get("text") or item.get("desc") or "")[:200],
        "date_found": "",
        "createTime": create_time,
        "stats": extract_stats(item),
        "thumbnail": thumbnail,
    }


def run_apify_actor(actor_id, input_json, label="", poll_interval=3, max_polls=30):
    """Run ANY Apify actor, wait for completion, return items. Graceful on failure."""
    try:
        r = requests.post(
            f"https://api.apify.com/v2/acts/{actor_id}/runs",
            params={"token": APIFY_TOKEN}, json=input_json, timeout=30,
        )
        r.raise_for_status()
        run_data = r.json().get("data", {})
        run_id = run_data.get("id")
        if not run_id:
            print(f"  [apify] {label} — No run ID", file=sys.stderr)
            return []
        print(f"  [apify] {label} — Run {run_id}, waiting...", file=sys.stderr)
        status = "RUNNING"
        for _ in range(max_polls):
            time.sleep(poll_interval)
            try:
                sr = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}",
                                  params={"token": APIFY_TOKEN}, timeout=15)
                status = sr.json().get("data", {}).get("status")
            except Exception:
                continue
            if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                break
        if status != "SUCCEEDED":
            print(f"  [apify] {label} — ended: {status}", file=sys.stderr)
            return []
        dataset_id = run_data.get("defaultDatasetId")
        if not dataset_id: return []
        items_r = requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                               params={"token": APIFY_TOKEN, "format": "json"}, timeout=30)
        items = items_r.json() if items_r.status_code == 200 else []
        print(f"  [apify] {label} — got {len(items)} items", file=sys.stderr)
        if items:
            s = items[0]
            print(f"  [apify]   sample: playCount={s.get('playCount')}, diggCount={s.get('diggCount')}, cover={bool(s.get('cover') or s.get('originCover'))}", file=sys.stderr)
        return items
    except Exception as e:
        print(f"  [apify] {label} — Error: {e}", file=sys.stderr)
        return []


# ── Vision-based matching ────────────────────────────────────

DEFAULT_VISION_PROMPT = """Look at this video thumbnail. Does it show a man wearing a distinctive FLAME/FIRE PATTERN SHIRT and/or WHITE SUNGLASSES on a concert stage? He may be doing gloving (LED light show with hands).

Respond with exactly one word: YES, MAYBE, or NO."""

def build_vision_prompt(description=""):
    """Build a vision prompt dynamically from the user's moment description."""
    if not description or not description.strip():
        return DEFAULT_VISION_PROMPT
    return f"""Look at this video thumbnail. Does it appear to show or relate to the following moment?

"{description}"

Consider visual cues like setting, people, actions, clothing, and context. Respond with exactly one word: YES, MAYBE, or NO."""

def download_thumbnail(url, timeout=8):
    try:
        r = requests.get(url, timeout=timeout, headers=HEADERS)
        if r.status_code == 200 and len(r.content) > 500:
            return base64.standard_b64encode(r.content).decode("utf-8")
    except Exception:
        pass
    return None

def vision_check_thumbnail(b64, media_type="image/jpeg", vision_prompt=None):
    if vision_prompt is None:
        vision_prompt = DEFAULT_VISION_PROMPT
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 10,
                  "messages": [{"role": "user", "content": [
                      {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                      {"type": "text", "text": vision_prompt}
                  ]}]},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get("content", [{}])[0].get("text", "").strip().upper()
        else:
            print(f"  [vision] API {resp.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  [vision] Error: {e}", file=sys.stderr)
    return "SKIP"

def vision_filter_results(results, max_concurrent=5, vision_prompt=None):
    if not results: return []
    if vision_prompt is None:
        vision_prompt = DEFAULT_VISION_PROMPT
    print(f"  [vision] Scanning {len(results)} thumbnails...", file=sys.stderr)
    matched = []

    def check_one(r):
        thumb = r.get("thumbnail", "")
        if not thumb: return r, "NO_THUMB"
        b64 = download_thumbnail(thumb)
        if not b64: return r, "DL_FAIL"
        mt = "image/webp" if ".webp" in thumb.lower() else "image/png" if ".png" in thumb.lower() else "image/jpeg"
        return r, vision_check_thumbnail(b64, mt, vision_prompt)

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        futures = {pool.submit(check_one, r): r for r in results}
        for future in as_completed(futures):
            try:
                r, verdict = future.result()
            except Exception:
                continue
            acct = r.get("account_name", "?")
            if verdict in ("YES", "MAYBE"):
                print(f"  [vision] ✓ {verdict}: {acct}", file=sys.stderr)
                r["vision_match"] = verdict
                matched.append(r)
            elif verdict in ("NO_THUMB", "DL_FAIL", "SKIP"):
                r["vision_match"] = "UNKNOWN"
                matched.append(r)  # keep — can't verify so give benefit of doubt
            else:
                print(f"  [vision] ✗ NO: {acct}", file=sys.stderr)

    print(f"  [vision] Result: {len(matched)} kept out of {len(results)}", file=sys.stderr)
    return matched


# ── Search queries & filter ──────────────────────────────────

# ── No more hardcoded exclude keywords — filtering is fully dynamic now ──
MAX_AGE_SECONDS = 60 * 24 * 60 * 60  # 60 days


# ── Scrape our own accounts for matching posts ─────────────
def scrape_own_accounts(event_info, max_concurrent=5):
    """Scrape all 36 accounts on TikTok via Apify, find posts matching the event."""
    description = event_info.get('description', '').lower()
    event = event_info.get('event', '').lower()
    keywords_str = event_info.get('keywords', '')
    keywords = [k.strip().lower() for k in keywords_str.split(',') if k.strip()]

    # Build match terms from all user input
    match_terms = set()
    for kw in keywords:
        match_terms.add(kw)
    # Add key words from event name
    for w in event.split():
        if len(w) > 3:
            match_terms.add(w)
    # Add key words from description
    for w in description.split():
        if len(w) > 4:
            match_terms.add(w)

    print(f"  [own-scrape] Scraping {len(DEFAULT_OWN_ACCOUNTS)} accounts...", file=sys.stderr)
    print(f"  [own-scrape] Match terms: {list(match_terms)[:10]}...", file=sys.stderr)

    all_own_posts = []
    seen_urls = set()

    # Batch accounts into groups to avoid too many Apify runs
    account_list = sorted(DEFAULT_OWN_ACCOUNTS)
    batches = []
    batch_size = 5
    for i in range(0, len(account_list), batch_size):
        batches.append(account_list[i:i+batch_size])

    for batch_idx, batch in enumerate(batches):
        print(f"  [own-scrape] Batch {batch_idx+1}/{len(batches)}: {batch}", file=sys.stderr)

        # Scrape each account's recent posts using Apify TikTok profile scraper
        def scrape_account(acct):
            try:
                # Use the TikTok scraper to get recent posts from a profile
                profile_urls = [f"https://www.tiktok.com/@{acct}"]
                items = run_apify_actor("clockworks~tiktok-scraper",
                    {"profileUrls": profile_urls, "resultsPerPage": 30,
                     "shouldDownloadCovers": False, "shouldDownloadVideos": False,
                     "shouldDownloadSlideshowImages": False},
                    label=f"profile @{acct}", poll_interval=3, max_polls=20)
                return acct, items
            except Exception as e:
                print(f"  [own-scrape] @{acct} failed: {e}", file=sys.stderr)
                return acct, []

        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futures = {pool.submit(scrape_account, acct): acct for acct in batch}
            for future in as_completed(futures):
                try:
                    acct, items = future.result()
                except Exception:
                    continue

                matched = 0
                for item in items:
                    parsed = parse_apify_item(item, seen_urls)
                    if not parsed:
                        continue

                    # Check if post matches the event/moment
                    post_desc = (parsed.get("description") or "").lower()
                    post_url = parsed.get("url", "")

                    # Match if any keyword appears in the post description
                    is_match = False
                    if match_terms:
                        for term in match_terms:
                            if term in post_desc:
                                is_match = True
                                break
                    else:
                        # No match terms = include all recent posts
                        is_match = True

                    if is_match:
                        parsed["vision_match"] = "YES"  # our own posts are always valid
                        all_own_posts.append(parsed)
                        matched += 1

                print(f"  [own-scrape] @{acct}: {len(items)} posts scraped, {matched} matched", file=sys.stderr)

    print(f"  [own-scrape] Total: {len(all_own_posts)} matching posts from our accounts", file=sys.stderr)
    return all_own_posts

def filter_results(results):
    """Filter only by age — no more hardcoded keyword exclusions."""
    now = time.time()
    out = []
    for r in results:
        ct = r.get("createTime")
        if ct:
            try:
                if now - float(ct) > MAX_AGE_SECONDS: continue
            except (ValueError, TypeError): pass
        out.append(r)
    return out

def build_search_queries(event_info):
    """Build search queries dynamically — venue/location-specific queries come FIRST."""
    event = event_info.get('event', '').strip()
    venue = event_info.get('venue', '').strip()
    description = event_info.get('description', '').strip()
    keywords_str = event_info.get('keywords', '')
    keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]

    queries = []
    seen = set()

    def add(q):
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)

    # ── PRIORITY 1: Venue/location-specific queries (most specific first) ──
    # Parse venue into parts (e.g. "raymond james stadium" -> ["raymond james stadium", "raymond james"])
    venue_parts = []
    if venue:
        venue_full = venue.split(',')[0].strip()
        venue_parts.append(venue_full)
        # Also try shorter version (first two words)
        vwords = venue_full.split()
        if len(vwords) > 2:
            venue_parts.append(' '.join(vwords[:2]))
        # Extract city from venue (after comma)
        if ',' in venue:
            city = venue.split(',')[1].strip().split()[0]  # first word after comma
            if city and len(city) > 2:
                venue_parts.append(city)

    # Venue + each keyword (most targeted queries)
    for vp in venue_parts:
        for kw in keywords:
            add(f"{kw} {vp}")
        if event:
            add(f"{event} {vp}")

    # ── PRIORITY 2: Keyword combos (specific to the moment) ──
    # Multi-keyword combos (e.g. "baker mayfield zach bryan tampa")
    if len(keywords) >= 2:
        add(' '.join(keywords[:3]))
    for kw in keywords:
        add(kw)
        if event:
            add(f"{event} {kw}")

    # ── PRIORITY 3: Description phrases ──
    if description:
        phrases = [p.strip() for p in re.split(r'[,.]', description) if len(p.strip()) > 3]
        for phrase in phrases[:3]:
            words = phrase.split()[:5]
            add(' '.join(words))

    # ── PRIORITY 4: Broader event queries (lowest priority) ──
    if event:
        add(event)

    print(f"  [queries] Built {len(queries)} dynamic queries:", file=sys.stderr)
    for i, q in enumerate(queries):
        print(f"    {i+1}. {q}", file=sys.stderr)
    return queries

def build_hashtags(event_info):
    """Build hashtag list dynamically — includes venue/location hashtags."""
    event = event_info.get('event', '').strip()
    venue = event_info.get('venue', '').strip()
    keywords_str = event_info.get('keywords', '')
    keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]

    hashtags = []
    seen = set()

    def add_tag(text):
        tag = re.sub(r'[^a-zA-Z0-9]', '', text).lower()
        if tag and tag not in seen:
            seen.add(tag)
            hashtags.append(tag)

    # Venue-based hashtags (highest priority)
    if venue:
        # Full venue as hashtag
        add_tag(venue.split(',')[0].strip())
        # Individual venue words > 3 chars
        for w in venue.replace(',', ' ').split():
            if len(w) > 3:
                add_tag(w)
        # Venue + event combo
        if event:
            first_venue_word = venue.split(',')[0].strip().split()[0]
            add_tag(f"{event} {first_venue_word}")

    # Keyword hashtags
    for kw in keywords:
        add_tag(kw)

    # Event hashtag
    if event:
        add_tag(event)

    print(f"  [hashtags] Built {len(hashtags)} dynamic hashtags: {hashtags}", file=sys.stderr)
    return hashtags


# ── Relevance filter: reject results from the WRONG show ──
def relevance_filter(results, event_info):
    """Smart filter: if a venue is specified, results MUST match a location term.
    Generic terms like 'revival' or 'zach bryan' alone are NOT enough — those match every show."""
    venue = event_info.get('venue', '').lower().strip()
    keywords_str = event_info.get('keywords', '')
    description = event_info.get('description', '').lower().strip()
    keywords = [k.strip().lower() for k in keywords_str.split(',') if k.strip()]

    # ── Separate location terms from generic content terms ──
    location_terms = set()
    if venue:
        for w in venue.replace(',', ' ').split():
            w = w.lower().strip()
            if len(w) > 2:
                location_terms.add(w)

    # Also pull location-like keywords (city names, venue names)
    # Anything that's NOT a common music/artist term is treated as location-specific
    generic_terms = {"zach", "bryan", "concert", "show", "tour", "live", "revival",
                     "stage", "crowd", "fan", "fans", "sing", "singing", "song",
                     "music", "country", "performance", "video", "tiktok", "viral"}

    # Unique content terms = keywords that are specific to THIS moment (not generic)
    moment_terms = set()
    for kw in keywords:
        for w in kw.split():
            w = w.lower().strip()
            if len(w) > 2 and w not in generic_terms:
                # Check if it looks like a location term
                if w in location_terms:
                    continue  # already in location
                moment_terms.add(w)

    # Also get moment-specific terms from description
    for w in description.split():
        w = w.lower().strip()
        if len(w) > 3 and w not in generic_terms:
            moment_terms.add(w)

    has_venue = bool(location_terms)

    print(f"  [relevance] Location terms (REQUIRED if venue set): {location_terms}", file=sys.stderr)
    print(f"  [relevance] Moment terms (bonus): {moment_terms}", file=sys.stderr)
    print(f"  [relevance] Venue specified: {has_venue}", file=sys.stderr)

    if not location_terms and not moment_terms:
        return results

    kept = []
    dropped = 0
    for r in results:
        desc = (r.get("description") or "").lower()
        url = (r.get("url") or "").lower()
        acct = (r.get("account_name") or "").lower()
        combined = f"{desc} {url} {acct}"

        has_location_match = any(t in combined for t in location_terms)
        moment_matches = sum(1 for t in moment_terms if t in combined)

        if has_venue:
            # STRICT: must have a location match, OR 2+ moment term matches
            if has_location_match or moment_matches >= 2:
                kept.append(r)
            else:
                dropped += 1
        else:
            # No venue specified — more lenient, any moment term works
            if moment_matches >= 1:
                kept.append(r)
            else:
                dropped += 1

    print(f"  [relevance] Kept {len(kept)}, dropped {dropped} wrong-show results", file=sys.stderr)
    return kept


# ── HTTP Server ──────────────────────────────────────────────

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="public", **kwargs)

    def do_POST(self):
        if self.path == "/api/search": self.handle_search()
        elif self.path == "/api/event-search": self.handle_event_search()
        else: self.send_error(404)

    def handle_search(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            url = body.get("url", "").strip()
            platform = body.get("platform", "")
            if not url or not platform: return self.reply({"error": "Missing url or platform"}, 400)
            if not API_KEY: return self.reply({"error": "Set your API key in server.py"}, 500)
            cached = cache_get(url)
            if cached: return self.reply(cached)
            meta = get_metadata(url, platform)
            results = search_with_api(url, platform, meta)
            response = {"metadata": meta, "results": results}
            cache_set(url, response)
            self.reply(response)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.reply({"error": str(e)}, 500)

    def handle_event_search(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            print(f"\n{'='*60}", file=sys.stderr)
            print(f"[event-search] {body.get('event', '?')}", file=sys.stderr)

            cache_id = hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest()
            cached = cache_get(cache_id)
            if cached: return self.reply(cached)

            event = body.get('event', '').strip()
            venue = body.get('venue', '').strip()
            date = body.get('date', '').strip()
            description = body.get('description', '').strip()
            keywords_str = body.get('keywords', '').strip()
            ref_video = body.get('ref_video', '').strip()

            seen_urls = set()
            all_results = []

            # ══════════════════════════════════════════════════════
            # STEP 1: Use Anthropic web search to find ACTUAL URLs
            # This is the primary discovery method now — no more
            # relying on Apify keyword search which returns random trending stuff
            # ══════════════════════════════════════════════════════
            print(f"  [step1] Anthropic web search for specific URLs...", file=sys.stderr)
            if API_KEY:
                search_prompt = f"""Find TikTok and Instagram videos from this SPECIFIC event:

EVENT: {event}
VENUE: {venue}
DATE: {date}
WHAT HAPPENED: {description}
KEYWORDS: {keywords_str}
REFERENCE VIDEO: {ref_video}

CRITICAL: ONLY return results from {venue} on {date}. Do NOT include videos from other shows, cities, or dates.

Search TikTok, Instagram, YouTube, Twitter/X, and news sites for:
- "baker mayfield zach bryan tampa"
- "zach bryan raymond james stadium"
- "zach bryan revival tampa"
- site:tiktok.com {keywords_str} {venue.split(',')[0] if venue else ''}

Find at least 20-30 specific video URLs. Include the EXACT TikTok/Instagram URLs.
Return ONLY a JSON array. Each object: platform, account_name, url, description, confidence (high/medium), date_found."""

                try:
                    r = requests.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                        json={
                            "model": "claude-sonnet-4-20250514",
                            "max_tokens": 8192,
                            "system": f"You find videos of ONE SPECIFIC event at ONE SPECIFIC venue. ONLY return results from {venue} on {date}. REJECT results from other cities/dates. Return ONLY a JSON array.",
                            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                            "messages": [{"role": "user", "content": search_prompt}]
                        },
                        timeout=120,
                    )
                    r.raise_for_status()
                    text = "\n".join(b["text"] for b in r.json().get("content", []) if b.get("type") == "text")
                    cleaned = re.sub(r"```json|```", "", text).strip()
                    match = re.search(r"\[[\s\S]*\]", cleaned)
                    if match:
                        try:
                            web_results = json.loads(match.group(0))
                            for wr in web_results:
                                url = wr.get("url", "")
                                if url and url not in seen_urls:
                                    seen_urls.add(url)
                                    # Try to get real stats via oEmbed
                                    platform = wr.get("platform", "other")
                                    stats = {}
                                    thumbnail = ""
                                    if "tiktok.com" in url:
                                        platform = "tiktok"
                                        try:
                                            oembed = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(url)}", headers=HEADERS, timeout=8)
                                            if oembed.ok:
                                                od = oembed.json()
                                                thumbnail = od.get("thumbnail_url", "")
                                                if not wr.get("account_name"):
                                                    wr["account_name"] = "@" + od.get("author_name", "unknown")
                                                if not wr.get("description"):
                                                    wr["description"] = od.get("title", "")
                                        except Exception:
                                            pass
                                    elif "instagram.com" in url:
                                        platform = "instagram"
                                    elif "youtube.com" in url or "youtu.be" in url:
                                        platform = "youtube"

                                    all_results.append({
                                        "platform": platform,
                                        "account_name": wr.get("account_name", "@unknown"),
                                        "url": url,
                                        "description": wr.get("description", ""),
                                        "confidence": wr.get("confidence", "medium"),
                                        "thumbnail": thumbnail,
                                        "stats": stats,
                                        "vision_match": "YES",  # Claude already verified relevance
                                    })
                            print(f"  [step1] Found {len(all_results)} URLs via web search", file=sys.stderr)
                        except json.JSONDecodeError as e:
                            print(f"  [step1] JSON parse error: {e}", file=sys.stderr)
                except Exception as e:
                    print(f"  [step1] Web search error: {e}", file=sys.stderr)

            # ══════════════════════════════════════════════════════
            # STEP 2: Search the EXACT hashtags the user provided
            # These are the real hashtags people used, not guesses
            # ══════════════════════════════════════════════════════
            hashtags_from_keywords = []
            for kw in keywords_str.split(','):
                kw = kw.strip().lstrip('#').strip()
                if kw:
                    tag = re.sub(r'[^a-zA-Z0-9]', '', kw).lower()
                    if tag and tag not in hashtags_from_keywords:
                        hashtags_from_keywords.append(tag)

            if APIFY_TOKEN and hashtags_from_keywords:
                print(f"  [step2] Searching {len(hashtags_from_keywords)} exact hashtags: {hashtags_from_keywords}", file=sys.stderr)
                for tag in hashtags_from_keywords:
                    try:
                        items = run_apify_actor("clockworks~tiktok-hashtag-scraper",
                            {"hashtags": [tag], "resultsPerPage": 30,
                             "shouldDownloadCovers": False, "shouldDownloadVideos": False,
                             "shouldDownloadSlideshowImages": False},
                            label=f"#{tag}", max_polls=20)
                        added = 0
                        for item in items:
                            p = parse_apify_item(item, seen_urls)
                            if p:
                                all_results.append(p)
                                added += 1
                        print(f"  [step2] #{tag}: {added} new results", file=sys.stderr)
                    except Exception as e:
                        print(f"  [step2] #{tag} failed: {e}", file=sys.stderr)
                print(f"  [step2] Total after hashtags: {len(all_results)}", file=sys.stderr)

            # ══════════════════════════════════════════════════════
            # STEP 3: Scrape our own accounts for matching posts
            # ══════════════════════════════════════════════════════
            if APIFY_TOKEN:
                print(f"  [step3] Scraping {len(DEFAULT_OWN_ACCOUNTS)} own accounts...", file=sys.stderr)
                own_scraped = scrape_own_accounts(body)
                # Add to results, deduplicating
                for r in own_scraped:
                    if r["url"] not in seen_urls:
                        seen_urls.add(r["url"])
                        all_results.append(r)
                print(f"  [step3] {len(own_scraped)} from own accounts, total: {len(all_results)}", file=sys.stderr)

            # ══════════════════════════════════════════════════════
            # STEP 4: Split own vs others, tally stats
            # ══════════════════════════════════════════════════════
            own, others = split_results(all_results, DEFAULT_OWN_ACCOUNTS)

            own_totals = {"plays": 0, "likes": 0, "comments": 0, "shares": 0, "count": len(own)}
            for r in own:
                s = r.get("stats", {})
                own_totals["plays"] += s.get("plays", 0) or 0
                own_totals["likes"] += s.get("likes", 0) or 0
                own_totals["comments"] += s.get("comments", 0) or 0
                own_totals["shares"] += s.get("shares", 0) or 0

            other_totals = {"plays": 0, "likes": 0, "comments": 0, "shares": 0, "count": len(others)}
            for r in others:
                s = r.get("stats", {})
                other_totals["plays"] += s.get("plays", 0) or 0
                other_totals["likes"] += s.get("likes", 0) or 0
                other_totals["comments"] += s.get("comments", 0) or 0
                other_totals["shares"] += s.get("shares", 0) or 0

            totals = {
                "plays": own_totals["plays"] + other_totals["plays"],
                "likes": own_totals["likes"] + other_totals["likes"],
                "comments": own_totals["comments"] + other_totals["comments"],
                "shares": own_totals["shares"] + other_totals["shares"],
            }

            total = len(own) + len(others)
            print(f"  [done] {total} videos ({len(own)} own, {len(others)} others)", file=sys.stderr)
            print(f"  [stats] OUR NETWORK: {own_totals}", file=sys.stderr)
            print(f"  [stats] OTHERS: {other_totals}", file=sys.stderr)
            print(f"  [stats] COMBINED: {totals}", file=sys.stderr)
            print(f"{'='*60}\n", file=sys.stderr)

            response = {
                "results": others,
                "own_posts": own,
                "total": total,
                "totals": totals,
                "own_totals": own_totals,
                "other_totals": other_totals,
            }
            cache_set(cache_id, response)
            self.reply(response)

        except Exception as e:
            import traceback; traceback.print_exc()
            self.reply({"error": str(e)}, 500)

    def reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/" in msg:
            print(f"[http] {msg}", file=sys.stderr)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if not API_KEY:
        print("\n⚠️  Set your API key in server.py line 20\n")
    else:
        print(f"\n🤠 The Idiot Roundup running at http://localhost:{PORT}")
        print(f"   API key: ...{API_KEY[-8:]}")
        print(f"   Apify token: ...{APIFY_TOKEN[-8:]}")
        print(f"   Dynamic mode: queries, vision, and hashtags adapt to user input")
        print(f"   Server starting.\n")
    import socketserver
    socketserver.TCPServer.allow_reuse_address = True
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
