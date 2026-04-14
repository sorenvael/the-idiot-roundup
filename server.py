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

EXCLUDE_KEYWORDS = {"tampa", "florida", "raymond james", "harley davidson",
                     "harley bike", "motorcycle", "hockey", "nhl", "nfl", "nba",
                     "wedding", "shane gillis"}
MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days for presentation

def filter_results(results):
    now = time.time()
    out = []
    for r in results:
        desc = (r.get("description") or "").lower()
        if any(excl in desc for excl in EXCLUDE_KEYWORDS):
            continue
        ct = r.get("createTime")
        if ct:
            try:
                if now - float(ct) > MAX_AGE_SECONDS: continue
            except (ValueError, TypeError): pass
        out.append(r)
    return out

def build_search_queries(event_info):
    """Build search queries dynamically from ALL user-provided event info."""
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

    # Event name is the core query
    if event:
        add(event)

    # Event + venue
    if event and venue:
        venue_short = venue.split(',')[0].strip()  # e.g. "Alamodome" from "Alamodome, San Antonio TX"
        add(f"{event} {venue_short}")

    # Each keyword alone and combined with event
    for kw in keywords:
        add(kw)
        if event:
            add(f"{event} {kw}")

    # Extract key phrases from the description (first 3 meaningful phrases)
    if description:
        # Simple extraction: split on commas/periods, take short phrases
        phrases = [p.strip() for p in re.split(r'[,.]', description) if len(p.strip()) > 3]
        for phrase in phrases[:3]:
            # Use first few words of each phrase
            words = phrase.split()[:4]
            add(' '.join(words))

    # If we still don't have enough queries, add broader combinations
    if venue and keywords:
        for kw in keywords[:3]:
            add(f"{venue.split(',')[0].strip()} {kw}")

    print(f"  [queries] Built {len(queries)} dynamic queries: {queries[:5]}...", file=sys.stderr)
    return queries

def build_hashtags(event_info):
    """Build hashtag list dynamically from event info."""
    event = event_info.get('event', '').strip()
    keywords_str = event_info.get('keywords', '')
    keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]

    hashtags = []
    # Generate hashtags from event name (remove spaces)
    if event:
        hashtags.append(re.sub(r'[^a-zA-Z0-9]', '', event).lower())
    # Generate hashtags from keywords
    for kw in keywords:
        tag = re.sub(r'[^a-zA-Z0-9]', '', kw).lower()
        if tag and tag not in hashtags:
            hashtags.append(tag)

    print(f"  [hashtags] Built {len(hashtags)} dynamic hashtags: {hashtags}", file=sys.stderr)
    return hashtags


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

            # ── Cache key includes ALL fields so different prompts = different results ──
            cache_id = hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest()
            cached = cache_get(cache_id)
            if cached: return self.reply(cached)
            if not APIFY_TOKEN: return self.reply({"error": "Apify token not set"}, 500)

            # ── Parse own accounts from request (or use defaults) ──
            own_accounts_str = body.get('own_accounts', '')
            if own_accounts_str.strip():
                own_accounts = {a.strip().lstrip('@').lower() for a in own_accounts_str.split(',') if a.strip()}
            else:
                own_accounts = DEFAULT_OWN_ACCOUNTS

            # ── Build dynamic vision prompt from description ──
            description = body.get('description', '')
            vision_prompt = build_vision_prompt(description)
            print(f"  [vision] Using dynamic prompt based on: {description[:60]}...", file=sys.stderr)

            seen = set()
            candidates = []

            # ── Step 1: Keyword search (dynamic queries) ──
            queries = build_search_queries(body)
            print(f"  [step1] {len(queries)} keyword queries...", file=sys.stderr)
            for q in queries:
                try:
                    items = run_apify_actor("clockworks~tiktok-scraper",
                        {"searchQueries": [q], "resultsPerPage": 20,
                         "shouldDownloadCovers": False, "shouldDownloadVideos": False,
                         "shouldDownloadSlideshowImages": False},
                        label=f"kw '{q}'", max_polls=15)
                    for item in items:
                        p = parse_apify_item(item, seen)
                        if p: candidates.append(p)
                except Exception as e:
                    print(f"  [step1] Error on '{q}': {e}", file=sys.stderr)
                if len(candidates) >= 80:
                    print(f"  [step1] Hit 80 candidates, moving on", file=sys.stderr)
                    break
            print(f"  [step1] Got {len(candidates)} from keywords", file=sys.stderr)

            # ── Step 2: Hashtag search (dynamic hashtags) ──
            hashtags = build_hashtags(body)
            print(f"  [step2] {len(hashtags)} hashtag searches...", file=sys.stderr)
            ht_count = 0
            for tag in hashtags:
                try:
                    items = run_apify_actor("clockworks~tiktok-hashtag-scraper",
                        {"hashtags": [tag], "resultsPerPage": 20,
                         "shouldDownloadCovers": False, "shouldDownloadVideos": False,
                         "shouldDownloadSlideshowImages": False},
                        label=f"#{tag}", max_polls=15)
                    for item in items:
                        p = parse_apify_item(item, seen)
                        if p:
                            candidates.append(p)
                            ht_count += 1
                except Exception as e:
                    print(f"  [step2] #{tag} failed: {e}, skipping", file=sys.stderr)
            print(f"  [step2] Got {ht_count} new from hashtags (total: {len(candidates)})", file=sys.stderr)

            # ── Step 3: Filter out junk ──
            filtered = filter_results(candidates)
            print(f"  [step3] {len(candidates)} -> {len(filtered)} after text filter", file=sys.stderr)

            # ── Step 4: Vision scan (dynamic prompt) ──
            print(f"  [step4] Vision scanning {len(filtered)} thumbnails...", file=sys.stderr)
            final = vision_filter_results(filtered, vision_prompt=vision_prompt)
            print(f"  [step4] {len(final)} passed vision check", file=sys.stderr)

            # ── No more hardcoded seeds — results are fully dynamic ──

            own, others = split_results(final, own_accounts)

            totals = {"plays": 0, "likes": 0, "comments": 0, "shares": 0}
            for r in final:
                s = r.get("stats", {})
                totals["plays"] += s.get("plays", 0) or 0
                totals["likes"] += s.get("likes", 0) or 0
                totals["comments"] += s.get("comments", 0) or 0
                totals["shares"] += s.get("shares", 0) or 0

            print(f"  [done] {len(final)} videos ({len(own)} own, {len(others)} others)", file=sys.stderr)
            print(f"  [stats] {totals}", file=sys.stderr)
            print(f"{'='*60}\n", file=sys.stderr)

            response = {"results": others, "own_posts": own, "total": len(final), "totals": totals}
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
