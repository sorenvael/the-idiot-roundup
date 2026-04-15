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

# ── Feedback store (persists to disk, survives restarts) ─────
FEEDBACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback.json")

def load_feedback():
    """Load feedback from disk. Stores good/bad examples with descriptions for pattern learning."""
    default = {"good": [], "bad": []}
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE) as f:
                data = json.load(f)
            for k in default:
                if k not in data:
                    data[k] = default[k]
            return data
        except Exception:
            pass
    return default

def save_feedback(fb):
    with open(FEEDBACK_FILE, "w") as f:
        json.dump(fb, f, indent=2)

def record_feedback(url, account, domain, platform, description, is_good):
    """Record feedback — stores the full context so the system can learn patterns."""
    fb = load_feedback()
    entry = {"url": url, "account": account, "domain": domain, "platform": platform,
             "description": description, "timestamp": time.time()}

    target = fb["good"] if is_good else fb["bad"]
    # Don't duplicate
    if not any(e["url"] == url for e in target):
        target.append(entry)
    # If it was in the other list, remove it
    other = fb["bad"] if is_good else fb["good"]
    fb["bad" if is_good else "good"] = [e for e in other if e["url"] != url]

    save_feedback(fb)
    print(f"  [feedback] {'GOOD' if is_good else 'BAD'}: {account} — {description[:60]}", file=sys.stderr)
    print(f"  [feedback] Totals: {len(fb['good'])} good examples, {len(fb['bad'])} bad examples", file=sys.stderr)

    # Extract what makes bad results bad (learn patterns)
    if not is_good and fb["bad"]:
        bad_descriptions = [e["description"] for e in fb["bad"] if e.get("description")]
        if bad_descriptions:
            # Extract common words from bad descriptions (excluding generic terms)
            from collections import Counter
            generic = {"the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "is", "was",
                        "this", "that", "with", "from", "his", "her", "he", "she", "it", "my",
                        "just", "been", "have", "had", "has", "not", "but", "out", "all",
                        "zach", "bryan", "zachbryan", "concert", "fyp", "foryou", "viral"}
            words = []
            for d in bad_descriptions:
                for w in re.findall(r'[a-zA-Z]{3,}', d.lower()):
                    if w not in generic:
                        words.append(w)
            common_bad = Counter(words).most_common(10)
            print(f"  [feedback] Learned bad patterns: {common_bad}", file=sys.stderr)

def apply_feedback(results):
    """Score results using learned feedback. Bad-pattern matches get ranked lower, not removed."""
    fb = load_feedback()
    if not fb["bad"] and not fb["good"]:
        return results

    # Build pattern words from bad examples
    from collections import Counter
    generic = {"the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "is", "was",
                "this", "that", "with", "from", "his", "her", "he", "she", "it", "my",
                "just", "been", "have", "had", "has", "not", "but", "out", "all",
                "zach", "bryan", "zachbryan", "concert", "fyp", "foryou", "viral"}

    # Never treat account names as signals — accounts post about multiple events
    own_account_words = {a.lower() for a in DEFAULT_OWN_ACCOUNTS}
    # Also never treat these common words as signals
    never_signal = {"stadium", "arena", "fan", "fans", "night", "show", "live",
                    "performed", "playing", "crowd", "audience", "stage",
                    "buccaneers", "bucs", "tampa", "raymond", "james",
                    "com", "www", "https", "tiktok", "instagram", "youtube",
                    "two", "one", "first", "last", "new", "old", "just",
                    "best", "even", "back", "know", "say", "find", "you",
                    "got", "get", "like", "love", "good", "great"}

    bad_words = Counter()
    for e in fb["bad"]:
        for w in re.findall(r'[a-zA-Z]{3,}', (e.get("description") or "").lower()):
            if w not in generic and w not in own_account_words and w not in never_signal:
                bad_words[w] += 1

    good_words = Counter()
    for e in fb["good"]:
        for w in re.findall(r'[a-zA-Z]{3,}', (e.get("description") or "").lower()):
            if w not in generic and w not in own_account_words and w not in never_signal:
                good_words[w] += 1

    # Words that appear 2+ times in bad but NOT in good are negative signals
    bad_signals = {w for w, c in bad_words.items() if c >= 2 and w not in good_words}
    good_signals = {w for w, c in good_words.items() if c >= 2 and w not in bad_words}

    if bad_signals:
        print(f"  [feedback] Bad signals: {bad_signals}", file=sys.stderr)
    if good_signals:
        print(f"  [feedback] Good signals: {good_signals}", file=sys.stderr)

    # Exact bad URLs always go to the bottom
    bad_urls = {e["url"] for e in fb["bad"]}

    # Score and sort — bad-pattern matches go to the bottom, good matches to the top
    def score(r):
        url = r.get("url", "")
        desc = (r.get("description") or "").lower()
        words = set(re.findall(r'[a-zA-Z]{3,}', desc))

        if url in bad_urls:
            return -100  # exact bad URL — bottom

        s = 0
        s += len(words & good_signals) * 10   # good signal words boost
        s -= len(words & bad_signals) * 10     # bad signal words penalize
        return s

    results.sort(key=score, reverse=True)

    # Log what happened
    if bad_signals or good_signals:
        top3 = [(r.get("account_name","?"), score(r)) for r in results[:3]]
        bot3 = [(r.get("account_name","?"), score(r)) for r in results[-3:]]
        print(f"  [feedback] Top 3: {top3}, Bottom 3: {bot3}", file=sys.stderr)

    return results

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


# ── Anthropic API with proper tool_use loop ─────────────────
# The web_search tool requires an agentic loop: the API returns tool_use blocks,
# we must send tool_result blocks back until the model produces a final text response.

def anthropic_web_search(system_prompt, user_prompt, model="claude-sonnet-4-20250514", max_tokens=8192, max_turns=20):
    """Call Anthropic API with web_search tool, handling the full agentic tool_use loop.
    Returns the final text response after all web searches are complete."""
    if not API_KEY:
        return ""

    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    messages = [{"role": "user", "content": user_prompt}]

    for turn in range(max_turns):
        print(f"    [api] Turn {turn+1}...", file=sys.stderr)
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    "messages": messages,
                },
                timeout=120,
            )
            r.raise_for_status()
            resp = r.json()
        except Exception as e:
            print(f"    [api] Error: {e}", file=sys.stderr)
            break

        stop_reason = resp.get("stop_reason", "")
        content = resp.get("content", [])

        # Log what we got back
        block_types = [b.get("type") for b in content]
        print(f"    [api] stop_reason={stop_reason}, blocks={block_types}", file=sys.stderr)

        # Collect any text from this response
        turn_text = "\n".join(b["text"] for b in content if b.get("type") == "text")

        # If stop_reason is "end_turn", we're done
        if stop_reason == "end_turn":
            # Collect text from ALL turns
            all_text = "\n".join(b["text"] for b in content if b.get("type") == "text")
            print(f"    [api] Final response: {len(all_text)} chars", file=sys.stderr)
            return all_text

        # If stop_reason is "pause_turn", Claude got cut off mid-response
        # Continue the conversation so it can finish
        if stop_reason == "pause_turn":
            print(f"    [api] Paused (got {len(turn_text)} chars so far), continuing...", file=sys.stderr)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "Continue. List the remaining URLs."})
            continue

        # If stop_reason is "tool_use", handle tool results
        messages.append({"role": "assistant", "content": content})
        tool_results = []
        for block in content:
            if block.get("type") == "tool_use":
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": "Search completed.",
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unknown stop reason — return whatever text we have
            print(f"    [api] Unknown stop_reason '{stop_reason}', returning {len(turn_text)} chars", file=sys.stderr)
            return turn_text

    print(f"    [api] Hit max turns ({max_turns})", file=sys.stderr)
    return ""


def parse_json_results(text):
    """Extract a JSON array from text that may contain markdown formatting."""
    cleaned = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            print(f"    [json] Parse error: {e}", file=sys.stderr)
    return []


# ── Repost Finder search ─────────────────────────────────────

REPOST_SYSTEM = """You find reposts of viral videos and news articles about them. Return ONLY a JSON array.
Each object must have: platform (tiktok/instagram/youtube/facebook/twitter/other), account_name (@user or publication name), url (direct link), confidence (high/medium), date_found (YYYY-MM-DD).
Search thoroughly. Return ONLY the JSON array, nothing else."""

def search_with_api(url, platform, metadata):
    title = metadata.get("title", "")
    author = metadata.get("author", "")
    prompt = f"Find all reposts and news coverage of this video:\nURL: {url}\nPlatform: {platform}\nTitle: {title}\nAuthor: @{author}\nSearch for: reposts on other platforms, news articles, reaction videos. Be thorough."
    text = anthropic_web_search(REPOST_SYSTEM, prompt, model="claude-sonnet-4-20250514", max_tokens=4096)
    return parse_json_results(text)


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
        elif self.path == "/api/load-ref": self.handle_load_ref()
        elif self.path == "/api/event-search": self.handle_event_search()
        elif self.path == "/api/feedback": self.handle_feedback()
        else: self.send_error(404)

    def handle_feedback(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            url = body.get("url", "")
            account = body.get("account", "")
            domain = body.get("domain", "")
            platform = body.get("platform", "")
            description = body.get("description", "")
            is_good = body.get("good", False)

            if not url:
                return self.reply({"error": "No URL"}, 400)

            record_feedback(url, account, domain, platform, description, is_good)
            fb = load_feedback()
            self.reply({
                "ok": True,
                "total_good": len(fb["good"]),
                "total_bad": len(fb["bad"]),
            })
        except Exception as e:
            self.reply({"error": str(e)}, 500)

    def handle_load_ref(self):
        """Load reference video metadata: thumbnail, title, author, hashtags."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            url = body.get("url", "").strip()
            if not url:
                return self.reply({"error": "No URL provided"}, 400)

            print(f"[load-ref] Loading: {url}", file=sys.stderr)
            result = {"url": url, "title": "", "author": "", "thumbnail_url": "", "hashtags": []}

            # Try oEmbed for TikTok
            if "tiktok.com" in url:
                try:
                    r = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(url)}", headers=HEADERS, timeout=10)
                    if r.ok:
                        d = r.json()
                        result["title"] = d.get("title", "")
                        result["author"] = d.get("author_name", "")
                        result["thumbnail_url"] = d.get("thumbnail_url", "")
                        # Extract hashtags from title
                        tags = re.findall(r'#(\w+)', result["title"])
                        result["hashtags"] = tags
                        print(f"  [load-ref] Title: {result['title'][:60]}", file=sys.stderr)
                        print(f"  [load-ref] Author: {result['author']}", file=sys.stderr)
                        print(f"  [load-ref] Hashtags: {tags}", file=sys.stderr)
                        print(f"  [load-ref] Thumbnail: {bool(result['thumbnail_url'])}", file=sys.stderr)
                except Exception as e:
                    print(f"  [load-ref] oEmbed failed: {e}", file=sys.stderr)

            elif "instagram.com" in url:
                try:
                    r = requests.get(f"https://api.instagram.com/oembed/?url={quote_plus(url)}", headers=HEADERS, timeout=10)
                    if r.ok:
                        d = r.json()
                        result["title"] = d.get("title", "")
                        result["author"] = d.get("author_name", "")
                        result["thumbnail_url"] = d.get("thumbnail_url", "")
                        tags = re.findall(r'#(\w+)', result["title"])
                        result["hashtags"] = tags
                except Exception as e:
                    print(f"  [load-ref] Instagram oEmbed failed: {e}", file=sys.stderr)

            elif "youtube.com" in url or "youtu.be" in url:
                try:
                    r = requests.get(f"https://www.youtube.com/oembed?url={quote_plus(url)}&format=json", headers=HEADERS, timeout=10)
                    if r.ok:
                        d = r.json()
                        result["title"] = d.get("title", "")
                        result["author"] = d.get("author_name", "")
                        vid = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
                        if vid:
                            result["thumbnail_url"] = f"https://img.youtube.com/vi/{vid.group(1)}/hqdefault.jpg"
                except Exception as e:
                    print(f"  [load-ref] YouTube oEmbed failed: {e}", file=sys.stderr)

            # ── Vision scan: actually look at the thumbnail ──
            if result["thumbnail_url"] and API_KEY:
                print(f"  [load-ref] Scanning thumbnail with vision AI...", file=sys.stderr)
                b64 = download_thumbnail(result["thumbnail_url"])
                if b64:
                    try:
                        mt = "image/webp" if ".webp" in result["thumbnail_url"].lower() else "image/jpeg"
                        vr = requests.post(
                            "https://api.anthropic.com/v1/messages",
                            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                                  "messages": [{"role": "user", "content": [
                                      {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}},
                                      {"type": "text", "text": "Describe exactly what you see in this image. Who are the people? What are they doing? What's the setting? Be specific about identifying features, clothing, and the scene. Keep it to 2-3 sentences."}
                                  ]}]},
                            timeout=30,
                        )
                        if vr.status_code == 200:
                            vision_desc = vr.json().get("content", [{}])[0].get("text", "").strip()
                            result["vision_description"] = vision_desc
                            print(f"  [load-ref] Vision: {vision_desc}", file=sys.stderr)
                        else:
                            print(f"  [load-ref] Vision API {vr.status_code}", file=sys.stderr)
                    except Exception as e:
                        print(f"  [load-ref] Vision error: {e}", file=sys.stderr)

            self.reply(result)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.reply({"error": str(e)}, 500)

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

    def handle_event_search_OLD(self):
        pass  # old version removed

    def handle_event_search(self):
        """Same as handle_search (Repost Finder) but with own/other split and stats."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            url = body.get("url", "").strip()
            platform = body.get("platform", "")
            print(f"\n{'='*60}", file=sys.stderr)
            print(f"[event-search] URL: {url[:60]}", file=sys.stderr)

            if not url:
                return self.reply({"error": "No URL"}, 400)
            if not API_KEY:
                return self.reply({"error": "API key not set"}, 500)

            cache_id = hashlib.md5(url.encode()).hexdigest() + "_event"
            cached = cache_get(cache_id)
            if cached: return self.reply(cached)

            # ── Step 1: Get metadata (same as repost finder) ──
            meta = get_metadata(url, platform or "tiktok")
            print(f"  [meta] Title: {meta.get('title','')[:60]}, Author: {meta.get('author','')}", file=sys.stderr)

            # ── Step 2: Search for reposts (same function as repost finder) ──
            print(f"  [step1] Searching all platforms for reposts...", file=sys.stderr)
            results = search_with_api(url, platform or "tiktok", meta)
            print(f"  [step1] Found {len(results)} results", file=sys.stderr)

            # ── Step 3: Enrich with oEmbed ──
            all_results = []
            seen = {url}  # exclude the reference video itself
            for r in results:
                rurl = r.get("url", "")
                if not rurl or rurl in seen:
                    continue
                seen.add(rurl)

                rplat = r.get("platform", "other")
                thumbnail = ""
                account = r.get("account_name", "@unknown")
                description = r.get("description", "")

                if "tiktok.com" in rurl:
                    rplat = "tiktok"
                    m = re.search(r'/@([^/]+)', rurl)
                    if m and (not account or account == "@unknown"):
                        account = "@" + m.group(1)
                    try:
                        oembed = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(rurl)}", headers=HEADERS, timeout=8)
                        if oembed.ok:
                            od = oembed.json()
                            thumbnail = od.get("thumbnail_url", "")
                            account = "@" + od.get("author_name", account.lstrip("@"))
                            description = od.get("title", "") or description
                            print(f"    [enrich] {account}: {description[:50]}", file=sys.stderr)
                    except Exception:
                        pass
                elif "instagram.com" in rurl: rplat = "instagram"
                elif "youtube.com" in rurl or "youtu.be" in rurl: rplat = "youtube"
                elif "facebook.com" in rurl: rplat = "facebook"
                elif "twitter.com" in rurl or "x.com" in rurl: rplat = "twitter"
                else:
                    domain = urlparse(rurl).netloc.replace('www.', '')
                    if not account or account == "@unknown": account = domain

                all_results.append({"platform": rplat, "account_name": account, "url": rurl,
                    "description": description, "thumbnail": thumbnail, "stats": {}, "vision_match": "YES"})

            # ── Step 4: Scrape TikTok stats ──
            tiktok_urls = [r["url"] for r in all_results if r["platform"] == "tiktok" and "/video/" in r["url"]]
            if APIFY_TOKEN and tiktok_urls:
                print(f"  [step2] Scraping stats for {len(tiktok_urls)} TikTok videos...", file=sys.stderr)
                try:
                    items = run_apify_actor("clockworks~tiktok-scraper",
                        {"postURLs": tiktok_urls, "shouldDownloadCovers": False,
                         "shouldDownloadVideos": False, "shouldDownloadSlideshowImages": False},
                        label="stats", poll_interval=3, max_polls=30)
                    stats_map = {}
                    for item in items:
                        vu = item.get("webVideoUrl") or item.get("videoUrl") or item.get("url") or ""
                        if vu: stats_map[re.sub(r'\?.*$', '', vu)] = extract_stats(item)
                    for r in all_results:
                        clean = re.sub(r'\?.*$', '', r["url"])
                        if clean in stats_map: r["stats"] = stats_map[clean]
                    print(f"  [step2] Got stats for {len(stats_map)} videos", file=sys.stderr)
                except Exception as e:
                    print(f"  [step2] Stats error: {e}", file=sys.stderr)

            # ── Step 5: Split own vs others, tally ──
            own, others = split_results(all_results, DEFAULT_OWN_ACCOUNTS)
            def tally(lst):
                t = {"plays": 0, "likes": 0, "comments": 0, "shares": 0, "count": len(lst)}
                for r in lst:
                    s = r.get("stats", {})
                    for k in ["plays","likes","comments","shares"]: t[k] += s.get(k, 0) or 0
                return t
            own_totals = tally(own)
            other_totals = tally(others)
            totals = {k: own_totals[k] + other_totals[k] for k in ["plays","likes","comments","shares"]}

            print(f"  [done] {len(all_results)} results ({len(own)} own, {len(others)} others)", file=sys.stderr)
            print(f"  [stats] OUR: {own_totals}", file=sys.stderr)
            print(f"  [stats] TOTAL: {totals}", file=sys.stderr)
            print(f"{'='*60}\n", file=sys.stderr)

            response = {"metadata": meta, "results": others, "own_posts": own, "total": len(all_results),
                        "totals": totals, "own_totals": own_totals, "other_totals": other_totals}
            cache_set(cache_id, response)
            self.reply(response)

        except Exception as e:
            import traceback; traceback.print_exc()
            self.reply({"error": str(e)}, 500)
    def handle_event_search_LEGACY(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            cache_id = hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest()
            cached = cache_get(cache_id)
            if cached: return self.reply(cached)

            ref_url = body.get('ref_video', '').strip()
            venue = body.get('venue', '').strip()
            date = body.get('date', '').strip()
            ref_data = body.get('ref_data') or {}

            if not ref_url:
                return self.reply({"error": "Reference video URL is required"}, 400)

            print(f"[event-search] Ref: {ref_url[:60]}", file=sys.stderr)
            print(f"[event-search] Venue: {venue}, Date: {date}", file=sys.stderr)

            # ══════════════════════════════════════════════════════
            # STEP 1: Get reference video metadata
            # ══════════════════════════════════════════════════════
            ref_title = ref_data.get('title', '')
            ref_author = ref_data.get('author', '')
            ref_hashtags = ref_data.get('hashtags', [])

            if not ref_title and "tiktok.com" in ref_url:
                try:
                    r = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(ref_url)}", headers=HEADERS, timeout=10)
                    if r.ok:
                        d = r.json()
                        ref_title = d.get("title", "")
                        ref_author = d.get("author_name", "")
                        ref_hashtags = re.findall(r'#(\w+)', ref_title)
                except Exception as e:
                    print(f"  [step1] oEmbed failed: {e}", file=sys.stderr)

            print(f"  [ref] Title: {ref_title[:80]}", file=sys.stderr)
            print(f"  [ref] Author: {ref_author}", file=sys.stderr)
            print(f"  [ref] Hashtags: {ref_hashtags}", file=sys.stderr)

            seen_urls = {ref_url}
            all_results = []

            # ══════════════════════════════════════════════════════
            # STEP 2: Anthropic web search (with PROPER tool_use loop)
            # This is the ONLY discovery method — it actually works
            # because it searches the real web, not TikTok's algorithm
            # ══════════════════════════════════════════════════════
            print(f"  [step2] AI web search (with tool_use loop)...", file=sys.stderr)

            hashtag_str = ' '.join(f'#{t}' for t in ref_hashtags)
            venue_short = venue.split(',')[0].strip() if venue else ''
            title_clean = re.sub(r'#\w+', '', ref_title).strip()

            # Key own accounts to search for specifically
            key_accounts = ['oklahomanoutlaw', 'greatamericanbarscene', 'zachbryanarchive',
                            'morezachbryan', 'americanharddrive', 'withheavenontok']

            system = """You are a social media researcher. Find as many TikTok videos, Instagram posts, YouTube videos, and news articles as possible about a specific event.

RULES:
- ONLY include: TikTok videos, Instagram posts/reels, YouTube videos, news articles
- DO NOT include: ticketing sites, Spotify, setlist sites, Wikipedia, non-content pages
- For each result write: @account - description - FULL URL
- Do at least 6-8 different searches to maximize results
- Aim for 20-30+ results"""

            prompt = f"""Find every TikTok video, Instagram post, YouTube video, and news article about this event:

REFERENCE VIDEO: {ref_url}
TITLE: {ref_title}
AUTHOR: @{ref_author}
VENUE: {venue}
DATE: {date}
HASHTAGS: {hashtag_str}

Do ALL of these searches:
1. {' '.join(ref_hashtags[:2])} {venue_short} tiktok videos
2. site:tiktok.com {' '.join(ref_hashtags[:2])} {venue_short}
3. {title_clean[:40]} {venue_short} instagram
4. site:instagram.com {' '.join(ref_hashtags[:2])} {venue_short}
5. {' '.join(ref_hashtags)} {venue_short} news article
6. oklahomanoutlaw OR zachbryanarchive OR greatamericanbarscene {venue_short} {ref_hashtags[0] if ref_hashtags else ''}
7. morezachbryan OR americanharddrive OR withheavenontok {venue_short} {ref_hashtags[0] if ref_hashtags else ''}
8. "{title_clean[:30]}" youtube

List every URL with the @account name. Find at least 20 results."""

            text = anthropic_web_search(system, prompt)
            if text:
                print(f"  [step2] Got {len(text)} chars from web search", file=sys.stderr)
                print(f"  [step2] Preview: {text[:300]}", file=sys.stderr)

                # ── Extract URLs + nearby context (for account names) ──
                # Grab each URL with surrounding text so we can find the account name
                url_context = {}  # url -> nearby text
                for m in re.finditer(r'(?:@([\w.]+)\s*[-—:]*\s*)?(https?://(?:www\.)?(?:tiktok\.com|instagram\.com|youtube\.com|youtu\.be|twitter\.com|x\.com|facebook\.com|[\w.-]+\.(?:com|org|net))/[^\s\)"\'<>\]]*)', text):
                    url = m.group(2).rstrip('.,;:)')
                    nearby_account = m.group(1)  # captured @account before URL if present
                    url_context[url] = nearby_account

                # Also try to extract @mentions near URLs from the full text
                for m in re.finditer(r'@([\w.]+)[^\n]*?(https?://[^\s\)"\'<>\]]+)', text):
                    url = m.group(2).rstrip('.,;:)')
                    if url not in url_context or not url_context[url]:
                        url_context[url] = m.group(1)

                url_pattern = r'https?://(?:www\.)?(?:tiktok\.com|instagram\.com|youtube\.com|youtu\.be|twitter\.com|x\.com|facebook\.com|[\w.-]+\.(?:com|org|net))/[^\s\)"\'<>\]]*'
                raw_urls = list(set(re.findall(url_pattern, text)))
                raw_urls = [u.rstrip('.,;:)') for u in raw_urls]

                # Filter out junk domains (ticketing, seating, setlists, etc)
                junk_domains = {'stubhub.com', 'seatgeek.com', 'ticketmaster.com', 'vividseats.com',
                                'axs.com', 'setlist.fm', 'songkick.com', 'bandsintown.com',
                                'livenation.com', 'shazam.com', 'spotify.com', 'apple.com',
                                'genius.com', 'google.com', 'bing.com', 'wikipedia.org',
                                'anthropic.com', 'claude.ai'}
                found_urls = []
                for u in raw_urls:
                    domain = urlparse(u).netloc.lower().replace('www.', '')
                    if domain not in junk_domains:
                        found_urls.append(u)
                    else:
                        print(f"    [filter] Skipped junk: {domain}", file=sys.stderr)

                print(f"  [step2] Extracted {len(found_urls)} content URLs ({len(raw_urls) - len(found_urls)} junk filtered)", file=sys.stderr)

                # Also try JSON parsing in case Claude returned structured data
                json_results = parse_json_results(text)
                for jr in json_results:
                    u = jr.get("url", "")
                    if u and u not in found_urls:
                        found_urls.append(u)

                # ── Enrich each URL with oEmbed ──
                for url in found_urls:
                    if url in seen_urls:
                        continue
                    # Skip non-content URLs (search pages, discover pages, etc)
                    if '/discover/' in url or '/search/' in url or '/explore/' in url:
                        continue
                    seen_urls.add(url)

                    platform = "other"
                    thumbnail = ""
                    account = "@unknown"
                    description = ""

                    # ── Extract account name from URL as fallback ──
                    def account_from_url(u):
                        """Extract @username from URL path."""
                        path = urlparse(u).path
                        # TikTok: /@username/video/123
                        m = re.search(r'/@([^/]+)', path)
                        if m: return "@" + m.group(1)
                        # Instagram: /username/ or /p/xxx/ (can't get user from /p/ URLs)
                        parts = path.strip('/').split('/')
                        if parts and parts[0] not in ('p', 'reel', 'reels', 'tv', 'stories', 'explore'):
                            return "@" + parts[0]
                        return None

                    # Check if we got an account name from the text near the URL
                    context_account = url_context.get(url)
                    if context_account:
                        account = "@" + context_account

                    if "tiktok.com" in url:
                        platform = "tiktok"
                        url_account = account_from_url(url)
                        if url_account:
                            account = url_account
                        try:
                            oembed = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(url)}", headers=HEADERS, timeout=8)
                            if oembed.ok:
                                od = oembed.json()
                                thumbnail = od.get("thumbnail_url", "")
                                account = "@" + od.get("author_name", account.lstrip("@"))
                                description = od.get("title", "")
                                print(f"    [enrich] TikTok {account}: {description[:50]}", file=sys.stderr)
                            else:
                                print(f"    [enrich] TikTok oEmbed {oembed.status_code}, using URL: {account}", file=sys.stderr)
                        except Exception as e:
                            print(f"    [enrich] TikTok error: {e}, using URL: {account}", file=sys.stderr)
                    elif "instagram.com" in url:
                        platform = "instagram"
                        url_account = account_from_url(url)
                        if url_account:
                            account = url_account
                        try:
                            oembed = requests.get(f"https://api.instagram.com/oembed/?url={quote_plus(url)}", headers=HEADERS, timeout=8)
                            if oembed.ok:
                                od = oembed.json()
                                thumbnail = od.get("thumbnail_url", "")
                                account = "@" + od.get("author_name", account.lstrip("@"))
                                description = od.get("title", "")
                                print(f"    [enrich] Instagram {account}: {description[:50]}", file=sys.stderr)
                        except Exception:
                            print(f"    [enrich] Instagram oEmbed failed, using URL: {account}", file=sys.stderr)
                    elif "youtube.com" in url or "youtu.be" in url:
                        platform = "youtube"
                    elif "twitter.com" in url or "x.com" in url:
                        platform = "twitter"
                        url_account = account_from_url(url)
                        if url_account:
                            account = url_account
                    else:
                        # News article — use domain as account name
                        platform = "other"
                        domain = urlparse(url).netloc.replace('www.', '')
                        account = domain
                        description = domain

                    all_results.append({
                        "platform": platform,
                        "account_name": account,
                        "url": url,
                        "description": description,
                        "thumbnail": thumbnail,
                        "stats": {},
                        "vision_match": "YES",
                    })

                print(f"  [step2] {len(all_results)} total results after enrichment", file=sys.stderr)
            else:
                print(f"  [step2] Web search returned no text!", file=sys.stderr)

            # ══════════════════════════════════════════════════════
            # STEP 3a: Text filter — drop results obviously from wrong cities/events
            # ══════════════════════════════════════════════════════
            venue_lower = venue.lower() if venue else ""
            venue_words = set()
            if venue_lower:
                for w in venue_lower.replace(',', ' ').split():
                    if len(w) > 2:
                        venue_words.add(w)

            # Known wrong-city indicators — if a description mentions one of these
            # and NONE of the venue words, it's probably from a different show
            other_cities = {"louisville", "kentucky", "michigan", "notre dame", "atlanta",
                           "tulsa", "oklahoma city", "nashville", "pittsburgh", "denver",
                           "chicago", "detroit", "columbus", "stagecoach", "coachella",
                           "big house", "ann arbor"}

            before_filter = len(all_results)
            filtered_results = []
            for r in all_results:
                desc = (r.get("description") or "").lower()
                # If description mentions another city but NOT our venue, drop it
                mentions_other = any(city in desc for city in other_cities)
                mentions_venue = any(vw in desc for vw in venue_words) if venue_words else True
                if mentions_other and not mentions_venue:
                    print(f"    [filter] Dropped wrong city: {r.get('account_name','?')} — {desc[:50]}", file=sys.stderr)
                else:
                    filtered_results.append(r)
            all_results = filtered_results
            print(f"  [step3a] Text filter: {before_filter} → {len(all_results)} (dropped {before_filter - len(all_results)} wrong-city results)", file=sys.stderr)

            # ══════════════════════════════════════════════════════
            # STEP 3b: Vision cross-reference — scan remaining thumbnails
            # Lenient check: "could this be from the same EVENT" not "same exact frame"
            # ══════════════════════════════════════════════════════
            ref_vision = ref_data.get('vision_description', '')
            thumbnails_to_scan = [r for r in all_results if r.get("thumbnail") and r.get("platform") == "tiktok"]

            if ref_vision and API_KEY and thumbnails_to_scan:
                print(f"  [step3] Vision cross-referencing {len(thumbnails_to_scan)} thumbnails...", file=sys.stderr)
                print(f"  [step3] Reference scene: {ref_vision[:80]}", file=sys.stderr)

                def vision_compare(r):
                    thumb_url = r.get("thumbnail", "")
                    if not thumb_url:
                        return r, "NO_THUMB"
                    b64 = download_thumbnail(thumb_url)
                    if not b64:
                        return r, "DL_FAIL"
                    mt = "image/webp" if ".webp" in thumb_url.lower() else "image/jpeg"
                    try:
                        resp = requests.post(
                            "https://api.anthropic.com/v1/messages",
                            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 10,
                                  "messages": [{"role": "user", "content": [
                                      {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}},
                                      {"type": "text", "text": f"The reference video is from a concert at {venue or 'a stadium'}. It shows: {ref_vision}\n\nCould THIS thumbnail be from the SAME concert/event? It doesn't need to show the exact same moment — it could be a different angle, crowd shot, or different point in the show. Look for: similar stadium/stage setting, concert atmosphere, or any of the same performers.\n\nRespond YES if it could be from the same event, MAYBE if uncertain, NO only if it's clearly a different event or location."}
                                  ]}]},
                            timeout=30,
                        )
                        if resp.status_code == 200:
                            return r, resp.json().get("content", [{}])[0].get("text", "").strip().upper()
                        elif resp.status_code == 429:
                            return r, "RATE_LIMITED"
                        else:
                            return r, "SKIP"
                    except Exception:
                        return r, "SKIP"

                # Scan with limited concurrency to avoid rate limits
                scanned = 0
                for r in thumbnails_to_scan:
                    if scanned >= 15:  # cap at 15 vision calls per search
                        print(f"  [step3] Hit vision cap (15), stopping", file=sys.stderr)
                        break
                    result_item, verdict = vision_compare(r)
                    acct = result_item.get("account_name", "?")
                    if verdict in ("YES", "MAYBE"):
                        result_item["vision_match"] = verdict
                        print(f"  [step3] ✓ {verdict}: {acct}", file=sys.stderr)
                    elif verdict == "NO":
                        result_item["vision_match"] = "NO"
                        print(f"  [step3] ✗ NO: {acct}", file=sys.stderr)
                    elif verdict == "RATE_LIMITED":
                        print(f"  [step3] ⚠ Rate limited, stopping vision", file=sys.stderr)
                        break
                    else:
                        result_item["vision_match"] = "UNKNOWN"
                    scanned += 1
                    time.sleep(0.5)  # small delay between calls

                print(f"  [step3] Scanned {scanned} thumbnails", file=sys.stderr)
            else:
                if not ref_vision:
                    print(f"  [step3] No vision description from reference — skipping cross-reference", file=sys.stderr)

            # ══════════════════════════════════════════════════════
            # STEP 4: Scrape TikTok URLs via Apify for real stats
            # Only scrape videos that passed vision (YES/MAYBE/UNKNOWN)
            # oEmbed doesn't return views/likes — Apify does
            # ══════════════════════════════════════════════════════
            # Filter out vision NO results before stats scrape
            all_results = [r for r in all_results if r.get("vision_match", "UNKNOWN") != "NO"]
            tiktok_urls = [r["url"] for r in all_results if r.get("platform") == "tiktok" and "/video/" in r.get("url", "")]
            if APIFY_TOKEN and tiktok_urls:
                print(f"  [step3] Scraping stats for {len(tiktok_urls)} TikTok URLs via Apify...", file=sys.stderr)
                try:
                    items = run_apify_actor("clockworks~tiktok-scraper",
                        {"postURLs": tiktok_urls,
                         "shouldDownloadCovers": False, "shouldDownloadVideos": False,
                         "shouldDownloadSlideshowImages": False},
                        label="stats scrape", poll_interval=3, max_polls=30)

                    # Build a lookup from URL to stats
                    stats_map = {}
                    for item in items:
                        video_url = item.get("webVideoUrl") or item.get("videoUrl") or item.get("url") or ""
                        if video_url:
                            stats_map[video_url] = extract_stats(item)

                    # Apply stats to our results
                    matched_stats = 0
                    for r in all_results:
                        url = r.get("url", "")
                        if url in stats_map:
                            r["stats"] = stats_map[url]
                            matched_stats += 1
                        else:
                            # Try matching without query params
                            clean = re.sub(r'\?.*$', '', url)
                            for su, st in stats_map.items():
                                if re.sub(r'\?.*$', '', su) == clean:
                                    r["stats"] = st
                                    matched_stats += 1
                                    break

                    print(f"  [step3] Got stats for {matched_stats}/{len(tiktok_urls)} videos", file=sys.stderr)
                    if stats_map:
                        sample = list(stats_map.values())[0]
                        print(f"  [step3] Sample: plays={sample.get('plays')}, likes={sample.get('likes')}", file=sys.stderr)
                except Exception as e:
                    print(f"  [step3] Stats scrape error: {e}", file=sys.stderr)
            else:
                if not tiktok_urls:
                    print(f"  [step3] No TikTok URLs to scrape stats for", file=sys.stderr)
                elif not APIFY_TOKEN:
                    print(f"  [step3] No Apify token — can't scrape stats", file=sys.stderr)

            # ══════════════════════════════════════════════════════
            # STEP 4: Apply learned feedback, then split and tally
            # ══════════════════════════════════════════════════════
            all_results = apply_feedback(all_results)
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
