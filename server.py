#!/usr/bin/env python3
"""
The Idiot Roundup — Server with Anthropic web search
Run:  python3 server.py
Open: http://localhost:3000
"""

import json, os, re, sys, time, hashlib
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, quote_plus
import requests

PORT = 3000
TIMEOUT = 10
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# ── Your API key ──
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Cache (saves API credits) ────────────────────────────────
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_TTL = 24 * 60 * 60  # 24 hours

def cache_key(url):
    """Normalize URL and hash it for cache filename."""
    normalized = re.sub(r'\?.*$', '', url).rstrip('/').lower()
    return hashlib.md5(normalized.encode()).hexdigest()

def cache_get(url):
    """Return cached result or None."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_key(url) + ".json")
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < CACHE_TTL:
            with open(path) as f:
                data = json.load(f)
            print(f"  [cache] HIT — {len(data.get('results',[]))} results (age: {int(age/60)}m)", file=sys.stderr)
            return data
        else:
            print(f"  [cache] EXPIRED (age: {int(age/3600)}h)", file=sys.stderr)
    return None

def cache_set(url, data):
    """Save result to cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_key(url) + ".json")
    with open(path, "w") as f:
        json.dump(data, f)
    print(f"  [cache] Saved {len(data.get('results',[]))} results", file=sys.stderr)


# ── Metadata via oEmbed ───────────────────────────────────────

def get_metadata(url, platform):
    try:
        if platform == "tiktok":
            r = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(url)}", headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            return {"title": d.get("title", ""), "author": d.get("author_name", ""), "thumbnail_url": d.get("thumbnail_url", "")}
        if platform == "youtube":
            r = requests.get(f"https://www.youtube.com/oembed?url={quote_plus(url)}&format=json", headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            vid = None
            for pat in [r"shorts/([a-zA-Z0-9_-]{11})", r"v=([a-zA-Z0-9_-]{11})", r"youtu\.be/([a-zA-Z0-9_-]{11})"]:
                m = re.search(pat, url)
                if m: vid = m.group(1); break
            thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else d.get("thumbnail_url", "")
            return {"title": d.get("title", ""), "author": d.get("author_name", ""), "thumbnail_url": thumb}
        if platform == "instagram":
            r = requests.get(f"https://api.instagram.com/oembed/?url={quote_plus(url)}", headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            return {"title": d.get("title", "Instagram Reel"), "author": d.get("author_name", ""), "thumbnail_url": d.get("thumbnail_url", "")}
    except Exception as e:
        print(f"  [meta] oEmbed failed: {e}", file=sys.stderr)
    parts = urlparse(url).path.strip("/").split("/")
    author = parts[0].lstrip("@") if parts else "unknown"
    return {"title": f"{platform.title()} video", "author": author, "thumbnail_url": ""}


# ── Anthropic API Search ──────────────────────────────────────

SYSTEM_PROMPT = """You find reposts of viral videos and news articles about them. Return ONLY a JSON array.

Each object must have: platform (tiktok/instagram/youtube/facebook/twitter/other), account_name (@user or publication name), url (direct link), confidence (high/medium), date_found (YYYY-MM-DD).

Include:
- Video reposts on TikTok, Instagram, YouTube, Facebook, Twitter/X
- News articles, blog posts, and media coverage about the video
- Reaction videos and commentary that include the original clip

Search thoroughly. For viral videos, find 15-30+ results. Return ONLY the JSON array, nothing else."""


def search_with_api(url, platform, metadata):
    """Use Anthropic API with web search to find reposts and articles."""
    title = metadata.get("title", "")
    author = metadata.get("author", "")

    prompt = f"""Find all reposts and news coverage of this video:

URL: {url}
Platform: {platform}
Title: {title}
Author: @{author}

Search for: reposts on other platforms, news articles, blog posts, reaction videos. Be thorough — search by title, author, keywords, and URL. Return as many results as you can find."""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )

    if r.status_code == 429:
        retry_after = r.headers.get("retry-after", "60")
        print(f"  [api] Rate limited. Waiting {retry_after}s...", file=sys.stderr)
        time.sleep(int(retry_after) if retry_after.isdigit() else 60)
        # Retry once
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "system": SYSTEM_PROMPT,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )

    r.raise_for_status()
    data = r.json()

    # Extract text from response
    text = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    print(f"  [api] Response length: {len(text)} chars", file=sys.stderr)

    # Parse JSON array
    results = []
    cleaned = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        try:
            results = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            print(f"  [api] JSON parse error: {e}", file=sys.stderr)

    return results


# ── HTTP Server ───────────────────────────────────────────────

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="public", **kwargs)

    def do_POST(self):
        if self.path == "/api/search":
            self.handle_search()
        else:
            self.send_error(404)

    def handle_search(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            url = body.get("url", "").strip()
            platform = body.get("platform", "")

            if not url or not platform:
                return self.reply({"error": "Missing url or platform"}, 400)
            if not API_KEY:
                return self.reply({"error": "Set your API key in server.py (line 20) or via ANTHROPIC_API_KEY env var"}, 500)

            print(f"\n{'='*60}", file=sys.stderr)
            print(f"[search] {platform}: {url[:80]}", file=sys.stderr)

            # Check cache first
            cached = cache_get(url)
            if cached:
                print(f"{'='*60}\n", file=sys.stderr)
                return self.reply(cached)

            # Get metadata
            meta = get_metadata(url, platform)
            print(f"  [meta] Title: {meta['title'][:80]}", file=sys.stderr)
            print(f"  [meta] Author: {meta['author']}", file=sys.stderr)

            # Search with Anthropic API
            print(f"  [api] Searching (this takes 15-30s)...", file=sys.stderr)
            results = search_with_api(url, platform, meta)
            print(f"  [done] Found {len(results)} results", file=sys.stderr)
            print(f"{'='*60}\n", file=sys.stderr)

            response = {"metadata": meta, "results": results}
            cache_set(url, response)
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
        print("\n⚠️  Set your API key:")
        print("   Option 1: Edit server.py line 20")
        print("   Option 2: ANTHROPIC_API_KEY=sk-ant-... python3 server.py\n")
    else:
        print(f"\n🤠 The Idiot Roundup running at http://localhost:{PORT}")
        print(f"   API key: ...{API_KEY[-8:]}\n")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
