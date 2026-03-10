#!/usr/bin/env python3
"""Quick diagnostic — run this to see what's happening with searches."""
import requests, re
from urllib.parse import unquote, urlparse, parse_qs, quote_plus

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# Step 1: Test oEmbed
print("=" * 60)
print("STEP 1: Testing TikTok oEmbed metadata...")
print("=" * 60)
tiktok_url = "https://www.tiktok.com/@greatamericanbarscene/video/7386165232609119519"
try:
    r = requests.get(f"https://www.tiktok.com/oembed?url={quote_plus(tiktok_url)}", headers=HEADERS, timeout=12)
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        print(f"Title: {d.get('title', 'N/A')}")
        print(f"Author: {d.get('author_name', 'N/A')}")
        print(f"Thumbnail: {d.get('thumbnail_url', 'N/A')[:80]}")
    else:
        print(f"Response: {r.text[:300]}")
except Exception as e:
    print(f"Error: {e}")

# Step 2: Test DuckDuckGo search
print("\n" + "=" * 60)
print("STEP 2: Testing DuckDuckGo search...")
print("=" * 60)
query = "greatamericanbarscene tiktok viral video"
try:
    r = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query, "b": ""},
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=12,
    )
    print(f"Status: {r.status_code}")
    print(f"Response size: {len(r.text)} chars")

    # Show first 500 chars to see what DDG returns
    print(f"\nFirst 500 chars of response:")
    print(r.text[:500])

    # Try to find result links
    print(f"\nSearching for result__a links...")
    matches = re.findall(r'class="result__a"', r.text)
    print(f"Found {len(matches)} result__a matches")

    # Try alternate patterns
    print(f"\nSearching for ANY <a> with href...")
    all_links = re.findall(r'href="(https?://[^"]+)"', r.text)
    print(f"Found {len(all_links)} total links")
    for link in all_links[:10]:
        print(f"  {link[:100]}")

    # Check if DDG is showing a captcha or block page
    if "robot" in r.text.lower() or "captcha" in r.text.lower():
        print("\n⚠️  DDG appears to be showing a CAPTCHA/bot check!")
    if "no results" in r.text.lower():
        print("\n⚠️  DDG says 'no results'")

except Exception as e:
    print(f"Error: {e}")

# Step 3: Test Google as alternative
print("\n" + "=" * 60)
print("STEP 3: Testing Google search...")
print("=" * 60)
try:
    r = requests.get(
        "https://www.google.com/search",
        params={"q": query, "num": 10},
        headers={**HEADERS, "Accept": "text/html"},
        timeout=12,
    )
    print(f"Status: {r.status_code}")
    print(f"Response size: {len(r.text)} chars")

    # Find links
    all_links = re.findall(r'href="(https?://[^"]+)"', r.text)
    print(f"Found {len(all_links)} total links")
    for link in all_links[:10]:
        if "google" not in link:
            print(f"  {link[:100]}")

except Exception as e:
    print(f"Error: {e}")

# Step 4: Test Bing as alternative
print("\n" + "=" * 60)
print("STEP 4: Testing Bing search...")
print("=" * 60)
try:
    r = requests.get(
        "https://www.bing.com/search",
        params={"q": query},
        headers=HEADERS,
        timeout=12,
    )
    print(f"Status: {r.status_code}")
    print(f"Response size: {len(r.text)} chars")

    all_links = re.findall(r'href="(https?://[^"]+)"', r.text)
    print(f"Found {len(all_links)} total links")
    for link in all_links[:10]:
        if "bing" not in link and "microsoft" not in link:
            print(f"  {link[:100]}")

except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 60)
print("DONE — paste the output above back to me")
print("=" * 60)
