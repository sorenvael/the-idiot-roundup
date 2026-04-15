#!/usr/bin/env python3
"""
The Idiot Roundup — Clean rewrite.
Run:  python3 server.py
Open: http://localhost:3000
"""

import json, os, re, sys, time, hashlib
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# ─ CONFIG ─────────────────────────────────────────────────────
PORT = 3000
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_TTL = 86400
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HDRS = {"User-Agent": UA}

# ─ MASTER ACCOUNT LIST (36 accounts, ONE place) ──────────────
OWN_ACCOUNTS = {
    # Zach Bryan
    "americanharddrive", "greatamericanbarscene", "morezachbryan", "oklahomanoutlaw",
    "runnyeggsz", "withheavenontok", "zachbryanarchive", "harleycarmichael",
    # Ella Langley
    "ellalangleyarchive", "ellalangleyextras", "ellalangleylately", "fellas4ella", "langleyloyalists",
    # Ole 60
    "moreole60", "ole60archive", "ole60fans", "ole60vault",
    # Joshua Slone
    "isabelledavis97", "joshuaslonearchive", "joshuaslonenation", "morejoshuaslone",
    # Phil Kane
    "phil.kane.hq", "philkanehq",
    # Gabriella Rose
    "gabriellarosearchive",
    # General
    "barnburners", "colt.johnsontx", "folktunez", "graciekahan", "gunnarhendo",
    "harmonyandtwang", "kaylaalist", "maddiespamzzzz99", "oklahomasmokeshow02",
    "roadshowrecap", "spamrynnnn",
}

# ─ ARTIST → ACCOUNT MAPPING (for dynamic search prompts) ─────
ARTIST_ACCOUNTS = {
    "zach bryan": ["zachbryanarchive", "oklahomanoutlaw", "greatamericanbarscene",
                   "morezachbryan", "americanharddrive", "withheavenontok", "harleycarmichael", "runnyeggsz"],
    "ella langley": ["ellalangleyarchive", "ellalangleyextras", "ellalangleylately", "fellas4ella", "langleyloyalists"],
    "ole 60": ["moreole60", "ole60archive", "ole60fans", "ole60vault"],
    "joshua slone": ["joshuaslonearchive", "joshuaslonenation", "morejoshuaslone", "isabelledavis97"],
    "phil kane": ["phil.kane.hq", "philkanehq"],
    "gabriella rose": ["gabriellarosearchive"],
}
GENERAL_ACCOUNTS = ["barnburners", "colt.johnsontx", "folktunez", "graciekahan", "gunnarhendo",
                    "harmonyandtwang", "kaylaalist", "maddiespamzzzz99", "oklahomasmokeshow02",
                    "roadshowrecap", "spamrynnnn"]

# ─ JUNK DOMAINS (never show these) ───────────────────────────
JUNK_DOMAINS = {"stubhub.com", "seatgeek.com", "ticketmaster.com", "vividseats.com",
                "axs.com", "setlist.fm", "songkick.com", "bandsintown.com", "livenation.com",
                "shazam.com", "spotify.com", "apple.com", "genius.com", "google.com",
                "bing.com", "wikipedia.org", "anthropic.com", "claude.ai"}


def log(msg):
    """Log to stderr."""
    print(msg, file=sys.stderr)


# ─ CACHE ──────────────────────────────────────────────────────

def _cache_path(key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".json")

def cache_get(key):
    """Read cache. Returns data or None."""
    p = _cache_path(key)
    if os.path.exists(p):
        try:
            with open(p) as f:
                d = json.load(f)
            if time.time() - d.get("_ts", 0) < CACHE_TTL:
                log(f"  [cache] HIT")
                return d.get("data")
        except Exception:
            pass
    return None

def cache_set(key, data):
    """Write cache atomically."""
    p = _cache_path(key)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"_ts": time.time(), "data": data}, f)
        os.replace(tmp, p)  # atomic on most systems
    except Exception as e:
        log(f"  [cache] write error: {e}")


# ─ ANTHROPIC WEB SEARCH (with proper tool_use loop) ──────────

def anthropic_web_search(system, prompt, model="claude-sonnet-4-20250514", max_tokens=4096, max_turns=10):
    """
    Call Anthropic API with server-side web_search tool.
    Handles pause_turn (continue), end_turn (done), and 429 (retry once).
    Returns concatenated text from all turns.
    """
    if not API_KEY:
        log("  [api] No API key")
        return ""

    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    messages = [{"role": "user", "content": prompt}]
    all_text = ""

    for turn in range(max_turns):
        log(f"  [api] Turn {turn + 1}...")
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": messages,
        }
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers=headers, json=payload, timeout=120)
            if r.status_code == 429:
                log("  [api] 429 rate limited — waiting 10s")
                time.sleep(10)
                r = requests.post("https://api.anthropic.com/v1/messages",
                                  headers=headers, json=payload, timeout=120)
            r.raise_for_status()
        except Exception as e:
            log(f"  [api] Error: {e}")
            break

        resp = r.json()
        stop = resp.get("stop_reason", "")
        content = resp.get("content", [])
        blocks = [b.get("type") for b in content]
        log(f"  [api] stop={stop} blocks={blocks}")

        # Collect text from this turn
        for b in content:
            if b.get("type") == "text":
                all_text += b["text"]

        if stop == "end_turn":
            break
        elif stop == "pause_turn":
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "Continue listing results."})
        else:
            # Unknown stop reason — try continuing
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "Continue."})

    log(f"  [api] Total text: {len(all_text)} chars")
    return all_text


def parse_json_results(text):
    """Extract JSON array from text. Handles ```json blocks and raw arrays."""
    if not text:
        return []
    cleaned = re.sub(r"```json|```", "", text).strip()
    m = re.search(r"\[[\s\S]*\]", cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return []


# ─ METADATA (oEmbed) ─────────────────────────────────────────

def get_metadata(url):
    """Get title, author, thumbnail, hashtags from oEmbed."""
    meta = {"title": "", "author": "", "thumbnail_url": "", "hashtags": []}
    try:
        if "tiktok.com" in url:
            r = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(url)}", headers=HDRS, timeout=10)
            if r.ok:
                d = r.json()
                meta["title"] = d.get("title", "")
                meta["author"] = d.get("author_name", "")
                meta["thumbnail_url"] = d.get("thumbnail_url", "")
        elif "youtube.com" in url or "youtu.be" in url:
            r = requests.get(f"https://www.youtube.com/oembed?url={quote_plus(url)}&format=json", headers=HDRS, timeout=10)
            if r.ok:
                d = r.json()
                meta["title"] = d.get("title", "")
                meta["author"] = d.get("author_name", "")
                meta["thumbnail_url"] = d.get("thumbnail_url", "")
        meta["hashtags"] = re.findall(r"#(\w+)", meta["title"])
    except Exception as e:
        log(f"  [meta] Error: {e}")
    return meta


# ─ URL VERIFICATION ──────────────────────────────────────────

def verify_url(url):
    """Check if URL is real. HEAD then GET fallback."""
    try:
        r = requests.head(url, headers=HDRS, timeout=8, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        try:
            r = requests.get(url, headers=HDRS, timeout=8, stream=True, allow_redirects=True)
            r.close()
            return r.status_code < 400
        except Exception:
            return False

def verify_urls_parallel(results, max_workers=5):
    """Verify URLs in results list. Returns only results with live URLs."""
    if not results:
        return []
    log(f"  [verify] Checking {len(results)} URLs...")
    verified = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(verify_url, r["url"]): r for r in results}
        for f in as_completed(futures):
            r = futures[f]
            try:
                if f.result():
                    verified.append(r)
                else:
                    log(f"    DEAD: {r['url'][:60]}")
            except Exception:
                pass
    log(f"  [verify] {len(verified)}/{len(results)} live")
    return verified


# ─ ACCOUNT HELPERS ───────────────────────────────────────────

def is_own_account(account_name, url=""):
    """Exact match against OWN_ACCOUNTS. No substring matching."""
    clean = account_name.lstrip("@").lower().strip()
    if clean in OWN_ACCOUNTS:
        return True
    # Also check URL for /@account
    m = re.search(r"/@([^/?]+)", url.lower())
    if m and m.group(1) in OWN_ACCOUNTS:
        return True
    return False

def split_results(results):
    """Split into own[] and others[]."""
    own, others = [], []
    for r in results:
        if is_own_account(r.get("account_name", ""), r.get("url", "")):
            own.append(r)
        else:
            others.append(r)
    return own, others

def tally(results_list):
    """Sum stats across a list of results."""
    t = {"plays": 0, "likes": 0, "comments": 0, "shares": 0, "count": len(results_list)}
    for r in results_list:
        s = r.get("stats", {})
        t["plays"] += (s.get("plays", 0) or 0)
        t["likes"] += (s.get("likes", 0) or 0)
        t["comments"] += (s.get("comments", 0) or 0)
        t["shares"] += (s.get("shares", 0) or 0)
    return t

def get_relevant_accounts(title, hashtags):
    """Pick which of our accounts to search for based on video content."""
    text = (title + " " + " ".join(hashtags)).lower()
    relevant = set()
    for artist, accounts in ARTIST_ACCOUNTS.items():
        if any(kw in text for kw in artist.split()):
            relevant.update(accounts)
    relevant.update(GENERAL_ACCOUNTS)
    if len(relevant) <= len(GENERAL_ACCOUNTS):
        relevant = set(OWN_ACCOUNTS)  # nothing matched → include all
    return sorted(relevant)


# ─ ENRICH RESULT ─────────────────────────────────────────────

def enrich_result(url):
    """Detect platform, extract account from URL, get TikTok oEmbed data."""
    domain = urlparse(url).netloc.lower().replace("www.", "")
    path = urlparse(url).path

    platform = "other"
    account = ""
    description = ""
    thumbnail = ""

    if "tiktok.com" in domain:
        platform = "tiktok"
        m = re.search(r"/@([^/?]+)", path)
        if m:
            account = "@" + m.group(1)
        try:
            r = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(url)}", headers=HDRS, timeout=8)
            if r.ok:
                d = r.json()
                account = "@" + d.get("author_name", account.lstrip("@") or "unknown")
                description = d.get("title", "")
                thumbnail = d.get("thumbnail_url", "")
                log(f"    [enrich] {account}: {description[:50]}")
        except Exception:
            pass
    elif "instagram.com" in domain:
        platform = "instagram"
        parts = path.strip("/").split("/")
        if parts and parts[0] not in ("p", "reel", "reels", "explore", "tv", "stories"):
            account = "@" + parts[0]
    elif "youtube.com" in domain or "youtu.be" in domain:
        platform = "youtube"
    elif "twitter.com" in domain or "x.com" in domain:
        platform = "twitter"
        m = re.search(r"/([^/]+)/status", path)
        if m:
            account = "@" + m.group(1)
    elif "facebook.com" in domain:
        platform = "facebook"
    else:
        account = domain

    if not account:
        account = platform.title() + " Post"

    return {"platform": platform, "account_name": account, "url": url,
            "description": description, "thumbnail": thumbnail, "stats": {}}


# ─ APIFY ─────────────────────────────────────────────────────

def run_apify(actor_id, input_json, label="", poll_interval=3, max_polls=30):
    """Run Apify actor, poll for completion, return items."""
    if not APIFY_TOKEN:
        return []
    try:
        r = requests.post(f"https://api.apify.com/v2/acts/{actor_id}/runs",
                          params={"token": APIFY_TOKEN}, json=input_json, timeout=30)
        r.raise_for_status()
        run_id = r.json().get("data", {}).get("id")
        dataset_id = r.json().get("data", {}).get("defaultDatasetId")
        if not run_id:
            return []
        log(f"  [apify] {label} run={run_id[:8]}...")
        for _ in range(max_polls):
            time.sleep(poll_interval)
            sr = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}",
                              params={"token": APIFY_TOKEN}, timeout=15)
            status = sr.json().get("data", {}).get("status")
            if status == "SUCCEEDED":
                ir = requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                                  params={"token": APIFY_TOKEN, "format": "json"}, timeout=30)
                items = ir.json() if ir.status_code == 200 else []
                log(f"  [apify] {label}: {len(items)} items")
                return items
            elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                log(f"  [apify] {label}: {status}")
                return []
        log(f"  [apify] {label}: timeout")
        return []
    except Exception as e:
        log(f"  [apify] {label} error: {e}")
        return []

def scrape_tiktok_stats(urls):
    """Get TikTok stats via Apify. Returns {url: stats_dict}."""
    if not urls:
        return {}
    items = run_apify("clockworks~tiktok-scraper",
                      {"postURLs": urls, "shouldDownloadCovers": False,
                       "shouldDownloadVideos": False, "shouldDownloadSlideshowImages": False},
                      label="tk-stats")
    stats = {}
    for item in items:
        vu = item.get("webVideoUrl") or item.get("videoUrl") or item.get("url") or ""
        clean_vu = re.sub(r"\?.*$", "", vu)
        so = item.get("stats") or item.get("statsV2") or {}
        def pick(*keys):
            for src in [item, so]:
                for k in keys:
                    v = src.get(k)
                    if v is not None:
                        try:
                            val = int(v)
                            if val > 0:
                                return val
                        except (ValueError, TypeError):
                            pass
            return 0
        stats[clean_vu] = {
            "plays": pick("playCount", "plays", "viewCount", "views"),
            "likes": pick("diggCount", "likes", "likeCount", "heart"),
            "comments": pick("commentCount", "comments"),
            "shares": pick("shareCount", "shares"),
        }
    return stats

def scrape_instagram_stats(urls):
    """Get Instagram stats via Apify."""
    if not urls:
        return {}
    items = run_apify("apify~instagram-scraper",
                      {"directUrls": urls, "resultsType": "posts", "resultsLimit": len(urls)},
                      label="ig-stats")
    stats = {}
    for item in items:
        vu = item.get("url") or item.get("inputUrl") or ""
        for u in urls:
            sc = item.get("shortCode", "")
            if (vu and vu in u) or (sc and sc in u):
                stats[u] = {
                    "plays": item.get("videoPlayCount", 0) or 0,
                    "likes": item.get("likesCount", 0) or 0,
                    "comments": item.get("commentsCount", 0) or 0,
                    "shares": 0,
                }
                owner = item.get("ownerUsername", "")
                if owner:
                    stats[u]["_account"] = "@" + owner
                caption = item.get("caption", "")
                if caption:
                    stats[u]["_description"] = caption[:200]
                break
    return stats

def scrape_twitter_stats(urls):
    """Get Twitter stats via Apify."""
    if not urls:
        return {}
    items = run_apify("datapilot~tweet-twitter-x-scraper",
                      {"urls": urls, "maxItems": len(urls)}, label="tw-stats")
    stats = {}
    for item in items:
        vu = item.get("url") or item.get("tweetUrl") or ""
        if vu:
            stats[vu] = {
                "plays": item.get("viewCount", 0) or 0,
                "likes": item.get("likeCount", 0) or 0,
                "comments": item.get("replyCount", 0) or 0,
                "shares": item.get("retweetCount", 0) or 0,
            }
            author = item.get("author", {})
            if isinstance(author, dict) and author.get("userName"):
                stats[vu]["_account"] = "@" + author["userName"]
            text = item.get("text", "")
            if text:
                stats[vu]["_description"] = text[:200]
    return stats

def scrape_facebook_stats(urls):
    """Get Facebook stats via Apify."""
    if not urls:
        return {}
    items = run_apify("apify~facebook-posts-scraper",
                      {"startUrls": [{"url": u} for u in urls], "resultsLimit": len(urls)},
                      label="fb-stats")
    stats = {}
    for item in items:
        vu = item.get("url") or item.get("postUrl") or ""
        if vu:
            stats[vu] = {
                "plays": item.get("videoViewCount", 0) or 0,
                "likes": item.get("likesCount", 0) or item.get("reactionsCount", 0) or 0,
                "comments": item.get("commentsCount", 0) or 0,
                "shares": item.get("sharesCount", 0) or 0,
            }
            author = item.get("pageName") or item.get("userName") or ""
            if author:
                stats[vu]["_account"] = author
            text = item.get("text") or item.get("message") or ""
            if text:
                stats[vu]["_description"] = text[:200]
    return stats


# ─ FEEDBACK ──────────────────────────────────────────────────

FEEDBACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback.json")

def load_feedback():
    """Load feedback from disk."""
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"good": [], "bad": []}

def save_feedback(fb):
    """Save feedback to disk."""
    tmp = FEEDBACK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(fb, f, indent=2)
    os.replace(tmp, FEEDBACK_FILE)

def record_feedback(url, account, platform, description, is_good):
    """Record feedback entry."""
    fb = load_feedback()
    entry = {"url": url, "account": account, "platform": platform,
             "description": description, "timestamp": time.time()}
    target = fb["good"] if is_good else fb["bad"]
    if not any(e["url"] == url for e in target):
        target.append(entry)
    other_key = "bad" if is_good else "good"
    fb[other_key] = [e for e in fb[other_key] if e["url"] != url]
    save_feedback(fb)
    log(f"  [feedback] {'GOOD' if is_good else 'BAD'}: {account} — {description[:40]}")
    log(f"  [feedback] {len(fb['good'])} good, {len(fb['bad'])} bad")


# ─ HTTP SERVER ───────────────────────────────────────────────

class Handler(SimpleHTTPRequestHandler):
    """Serves static files from public/ and handles API endpoints."""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory="public", **kw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if self.path == "/api/search":
            self._handle_search(body)
        elif self.path == "/api/event-search":
            self._handle_event_search(body)
        elif self.path == "/api/feedback":
            self._handle_feedback(body)
        elif self.path == "/api/load-ref":
            self._handle_load_ref(body)
        else:
            self.send_error(404)

    def _handle_search(self, body):
        """Repost Finder: paste URL, find reposts."""
        url = body.get("url", "").strip()
        platform = body.get("platform", "")
        if not url:
            return self._json(400, {"error": "url required"})
        cached = cache_get("search_" + url)
        if cached:
            return self._json(200, cached)
        meta = get_metadata(url)
        system = ("You find reposts of viral videos and news articles about them. "
                  "Return ONLY a JSON array. Each object: platform, account_name, url, description, confidence, date_found. "
                  "CRITICAL: TikTok video IDs are 19 digits. NEVER truncate URLs.")
        prompt = (f"Find all reposts and news coverage of this video:\n"
                  f"URL: {url}\nPlatform: {platform}\nTitle: {meta['title']}\nAuthor: @{meta['author']}\n"
                  f"Search TikTok, Instagram, YouTube, Facebook, Twitter/X, and news sites. Be thorough.")
        text = anthropic_web_search(system, prompt)
        results = parse_json_results(text)
        response = {"metadata": meta, "results": results}
        cache_set("search_" + url, response)
        self._json(200, response)

    def _handle_event_search(self, body):
        """Event Finder: paste URL, find reposts across all platforms, split own/others, scrape stats."""
        url = body.get("url", "").strip()
        if not url:
            return self._json(400, {"error": "url required"})

        log(f"\n{'='*60}")
        log(f"[event-search] {url[:60]}")

        cached = cache_get("event_" + url)
        if cached:
            return self._json(200, cached)

        # Step 1: Get metadata
        meta = get_metadata(url)
        log(f"  [meta] Title: {meta['title'][:60]}, Author: {meta['author']}")

        # Step 2: Build dynamic search prompt with relevant accounts
        relevant = get_relevant_accounts(meta["title"], meta["hashtags"])
        accounts_block = "\n".join(f"  - @{a}" for a in relevant[:10])

        system = ("You find reposts of viral videos across all social media platforms and news sites. "
                  "Return ONLY a JSON array. Each object must have: platform, account_name, url, description. "
                  "Include TikTok, Instagram, YouTube, Facebook, Twitter/X, and news articles. "
                  "DO NOT include ticketing sites, Spotify, setlist sites, Wikipedia. "
                  "CRITICAL: TikTok video IDs are 19 digits long. NEVER truncate URLs. "
                  "Search thoroughly — find as many results as possible.")

        prompt = (f"Find all reposts and coverage of this video:\n\n"
                  f"URL: {url}\n"
                  f"Title: {meta['title']}\n"
                  f"Author: @{meta['author']}\n\n"
                  f"ALSO specifically search for posts about this from these affiliated accounts:\n"
                  f"{accounts_block}\n\n"
                  f"Search each of those accounts on TikTok for posts about this same topic. "
                  f"Return as many results as you can find.")

        log(f"  [step1] Web search ({len(relevant)} accounts in prompt)...")
        text = anthropic_web_search(system, prompt)
        raw_results = parse_json_results(text)
        log(f"  [step1] Parsed {len(raw_results)} results")

        # Step 2b: Scrape our accounts' profiles for matching posts
        if APIFY_TOKEN:
            title_words = re.sub(r"#\w+", "", meta["title"]).lower().split()
            match_terms = {w for w in title_words if len(w) > 3}
            match_terms.update(h.lower() for h in meta["hashtags"])
            log(f"  [step2b] Scraping {len(relevant)} account profiles (match: {list(match_terms)[:5]})...")

            # Batch profiles — scrape 10 at a time
            for i in range(0, len(relevant), 10):
                batch = relevant[i:i+10]
                try:
                    items = run_apify("clockworks~tiktok-profile-scraper",
                                     {"profiles": batch, "resultsPerPage": 10},
                                     label=f"profiles batch {i//10+1}", poll_interval=3, max_polls=25)
                    found = 0
                    for item in items:
                        author = (item.get("authorMeta", {}).get("name", "") or
                                  item.get("author", {}).get("uniqueId", "") or
                                  item.get("authorName", "") or item.get("uniqueId", "") or "").lower()
                        desc = (item.get("text") or item.get("desc") or "").lower()

                        # Check if this post matches the reference video topic
                        if not any(t in desc for t in match_terms):
                            continue

                        # Build best URL we can — link to profile if video URL is truncated
                        vid_url = item.get("webVideoUrl") or item.get("videoUrl") or item.get("url") or ""
                        vid_id = str(item.get("id") or item.get("videoId") or "")
                        if vid_id and len(vid_id) >= 18 and author:
                            vid_url = f"https://www.tiktok.com/@{author}/video/{vid_id}"

                        if not vid_url and author:
                            vid_url = f"https://www.tiktok.com/@{author}"

                        # Extract stats directly from Apify data (no separate scrape needed)
                        so = item.get("stats") or item.get("statsV2") or {}
                        def _pick(*keys):
                            for src in [item, so]:
                                for k in keys:
                                    v = src.get(k)
                                    if v is not None:
                                        try:
                                            val = int(v)
                                            if val > 0: return val
                                        except (ValueError, TypeError): pass
                            return 0

                        stats = {
                            "plays": _pick("playCount", "plays", "viewCount"),
                            "likes": _pick("diggCount", "likes", "likeCount"),
                            "comments": _pick("commentCount", "comments"),
                            "shares": _pick("shareCount", "shares"),
                        }

                        thumbnail = (item.get("cover") or item.get("originCover") or
                                     item.get("dynamicCover") or item.get("videoMeta", {}).get("coverUrl", "") or "")

                        raw_results.append({
                            "url": vid_url,
                            "platform": "tiktok",
                            "account_name": "@" + author if author else "@unknown",
                            "description": desc[:200],
                            "thumbnail": thumbnail,
                            "stats": stats,
                            "_from_profile": True,  # flag: skip URL verification, stats already scraped
                        })
                        found += 1
                    if found:
                        log(f"    [profiles] Batch {i//10+1}: {found} matching posts")
                except Exception as e:
                    log(f"    [profiles] Batch {i//10+1} error: {e}")

            log(f"  [step2b] Total results after profile scrape: {len(raw_results)}")

        # Step 3: Enrich each result
        # Profile-scraped results already have all data — skip enrichment for them
        seen = {url}  # skip the reference video itself
        enriched = []
        for r in raw_results:
            rurl = r.get("url", "")
            if not rurl or rurl in seen:
                continue
            # Skip junk domains
            domain = urlparse(rurl).netloc.lower().replace("www.", "")
            if domain in JUNK_DOMAINS:
                continue
            if "/discover/" in rurl or "/search/" in rurl or "/explore/" in rurl:
                continue
            seen.add(rurl)

            if r.get("_from_profile"):
                # Already enriched by Apify — keep as-is
                enriched.append(r)
            else:
                # Web search result — enrich with oEmbed
                enriched.append(enrich_result(rurl))

        log(f"  [step2] Enriched {len(enriched)} results")

        # Step 4: Verify URLs are real (skip profile-scraped results — their data is already verified by Apify)
        from_profiles = [r for r in enriched if r.get("_from_profile")]
        from_web = [r for r in enriched if not r.get("_from_profile")]
        if from_web:
            from_web = verify_urls_parallel(from_web)
        enriched = from_profiles + from_web
        log(f"  [step4] {len(from_profiles)} profile results (trusted) + {len(from_web)} web results (verified)")

        # Step 5: Scrape stats by platform
        by_plat = {}
        for r in enriched:
            by_plat.setdefault(r["platform"], []).append(r["url"])

        # Only scrape stats for TikTok URLs from web search (profile results already have stats)
        tiktok_need_stats = [r["url"] for r in enriched
                             if r["platform"] == "tiktok" and not r.get("_from_profile")
                             and not r.get("stats", {}).get("plays") and "/video/" in r["url"]]
        if tiktok_need_stats:
            log(f"  [step5] Scraping stats for {len(tiktok_need_stats)} TikTok videos (web search only)...")
            tk_stats = scrape_tiktok_stats(tiktok_need_stats)
            for r in enriched:
                if r["platform"] == "tiktok" and not r.get("_from_profile"):
                    clean = re.sub(r"\?.*$", "", r["url"])
                    if clean in tk_stats:
                        r["stats"] = tk_stats[clean]

        if by_plat.get("instagram"):
            ig_stats = scrape_instagram_stats(by_plat["instagram"])
            for r in enriched:
                if r["platform"] == "instagram" and r["url"] in ig_stats:
                    s = ig_stats[r["url"]]
                    r["stats"] = {k: v for k, v in s.items() if not k.startswith("_")}
                    if s.get("_account"):
                        r["account_name"] = s["_account"]
                    if s.get("_description"):
                        r["description"] = s["_description"]

        if by_plat.get("twitter"):
            tw_stats = scrape_twitter_stats(by_plat["twitter"])
            for r in enriched:
                if r["platform"] == "twitter" and r["url"] in tw_stats:
                    s = tw_stats[r["url"]]
                    r["stats"] = {k: v for k, v in s.items() if not k.startswith("_")}
                    if s.get("_account"):
                        r["account_name"] = s["_account"]
                    if s.get("_description"):
                        r["description"] = s["_description"]

        if by_plat.get("facebook"):
            fb_stats = scrape_facebook_stats(by_plat["facebook"])
            for r in enriched:
                if r["platform"] == "facebook" and r["url"] in fb_stats:
                    s = fb_stats[r["url"]]
                    r["stats"] = {k: v for k, v in s.items() if not k.startswith("_")}
                    if s.get("_account"):
                        r["account_name"] = s["_account"]
                    if s.get("_description"):
                        r["description"] = s["_description"]

        # News articles: use domain as description
        for r in enriched:
            if r["platform"] == "other" and not r["description"]:
                r["description"] = urlparse(r["url"]).netloc.replace("www.", "")

        # Step 6: Split and tally
        own, others = split_results(enriched)
        own_totals = tally(own)
        other_totals = tally(others)
        totals = {k: own_totals[k] + other_totals[k] for k in ["plays", "likes", "comments", "shares"]}

        log(f"  [done] {len(enriched)} results ({len(own)} own, {len(others)} others)")
        log(f"  [stats] OUR: {own_totals}")
        log(f"  [stats] TOTAL: {totals}")
        log(f"{'='*60}\n")

        response = {
            "metadata": meta,
            "results": others,
            "own_posts": own,
            "total": len(enriched),
            "totals": totals,
            "own_totals": own_totals,
            "other_totals": other_totals,
        }
        cache_set("event_" + url, response)
        self._json(200, response)

    def _handle_feedback(self, body):
        """Record feedback on a result."""
        record_feedback(
            body.get("url", ""), body.get("account", ""),
            body.get("platform", ""), body.get("description", ""),
            body.get("good", False),
        )
        fb = load_feedback()
        self._json(200, {"ok": True, "total_good": len(fb["good"]), "total_bad": len(fb["bad"])})

    def _handle_load_ref(self, body):
        """Load reference video metadata."""
        url = body.get("url", "").strip()
        if not url:
            return self._json(400, {"error": "url required"})
        self._json(200, get_metadata(url))

    def _json(self, status, data):
        """Send JSON response."""
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
            log(f"[http] {msg}")


# ─ STARTUP ───────────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if not API_KEY:
        print("\n⚠️  Set ANTHROPIC_API_KEY environment variable\n")
    if not APIFY_TOKEN:
        print("⚠️  Set APIFY_TOKEN environment variable")
    print(f"\n🤠 The Idiot Roundup running at http://localhost:{PORT}")
    print(f"   {len(OWN_ACCOUNTS)} accounts tracked across {len(ARTIST_ACCOUNTS)} artists")
    print(f"   API key: {'...'+API_KEY[-8:] if API_KEY else 'NOT SET'}")
    print(f"   Apify: {'...'+APIFY_TOKEN[-8:] if APIFY_TOKEN else 'NOT SET'}\n")
    import socketserver
    socketserver.TCPServer.allow_reuse_address = True
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
