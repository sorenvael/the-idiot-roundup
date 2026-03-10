import Anthropic from "@anthropic-ai/sdk";
import crypto from "crypto";

// ── In-memory cache (persists per serverless instance) ──
const cache = new Map();
const CACHE_TTL = 24 * 60 * 60 * 1000; // 24 hours

function getCacheKey(url) {
  const normalized = url.replace(/\?.*$/, "").replace(/\/+$/, "").toLowerCase();
  return crypto.createHash("md5").update(normalized).digest("hex");
}

// ── oEmbed metadata fetching ──
async function getMetadata(url, platform) {
  try {
    if (platform === "tiktok") {
      const r = await fetch(`https://www.tiktok.com/oembed?url=${encodeURIComponent(url)}`);
      if (r.ok) {
        const d = await r.json();
        return { title: d.title || "", author: d.author_name || "", thumbnail_url: d.thumbnail_url || "" };
      }
    }
    if (platform === "youtube") {
      const r = await fetch(`https://www.youtube.com/oembed?url=${encodeURIComponent(url)}&format=json`);
      if (r.ok) {
        const d = await r.json();
        const vidMatch = url.match(/(?:shorts\/|v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
        const thumb = vidMatch ? `https://img.youtube.com/vi/${vidMatch[1]}/hqdefault.jpg` : d.thumbnail_url || "";
        return { title: d.title || "", author: d.author_name || "", thumbnail_url: thumb };
      }
    }
    if (platform === "instagram") {
      const r = await fetch(`https://api.instagram.com/oembed/?url=${encodeURIComponent(url)}`);
      if (r.ok) {
        const d = await r.json();
        return { title: d.title || "Instagram Reel", author: d.author_name || "", thumbnail_url: d.thumbnail_url || "" };
      }
    }
  } catch (e) {
    console.error("oEmbed failed:", e.message);
  }
  const parts = new URL(url).pathname.split("/").filter(Boolean);
  return { title: `${platform} video`, author: (parts[0] || "unknown").replace("@", ""), thumbnail_url: "" };
}

const SYSTEM_PROMPT = `You find reposts of viral videos and news articles about them. Return ONLY a JSON array.

Each object must have: platform (tiktok/instagram/youtube/facebook/twitter/other), account_name (@user or publication name), url (direct link), confidence (high/medium), date_found (YYYY-MM-DD).

Include:
- Video reposts on TikTok, Instagram, YouTube, Facebook, Twitter/X
- News articles, blog posts, and media coverage about the video
- Reaction videos and commentary that include the original clip

Search thoroughly. For viral videos, find 15-30+ results. Return ONLY the JSON array, nothing else.`;

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return res.status(500).json({ error: "ANTHROPIC_API_KEY not configured" });
  }

  try {
    const { url, platform } = req.body;

    if (!url || !platform) {
      return res.status(400).json({ error: "Missing url or platform" });
    }

    // ── Check cache ──
    const key = getCacheKey(url);
    const cached = cache.get(key);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
      console.log(`[cache] HIT for ${url.slice(0, 60)} (${cached.data.results.length} results)`);
      return res.status(200).json(cached.data);
    }

    // ── Fetch real metadata ──
    const metadata = await getMetadata(url, platform);
    console.log(`[meta] Title: ${metadata.title.slice(0, 60)}, Author: ${metadata.author}`);

    // ── Search with Anthropic API ──
    const client = new Anthropic({ apiKey });

    const message = await client.messages.create({
      model: "claude-sonnet-4-20250514",
      max_tokens: 4096,
      system: SYSTEM_PROMPT,
      messages: [
        {
          role: "user",
          content: `Find all reposts and news coverage of this video:

URL: ${url}
Platform: ${platform}
Title: ${metadata.title}
Author: @${metadata.author}

Search for: reposts on other platforms, news articles, blog posts, reaction videos. Be thorough — search by title, author, keywords, and URL. Return as many results as you can find.`,
        },
      ],
      tools: [{ type: "web_search_20250305", name: "web_search" }],
    });

    // Extract and parse results
    const text = message.content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("\n");

    let results = [];
    try {
      const cleaned = text.replace(/```json|```/g, "").trim();
      const jsonMatch = cleaned.match(/\[[\s\S]*\]/);
      if (jsonMatch) {
        results = JSON.parse(jsonMatch[0]);
      }
    } catch (parseErr) {
      console.error("JSON parse error:", parseErr.message);
    }

    console.log(`[done] Found ${results.length} results`);

    const response = { metadata, results };

    // ── Save to cache ──
    cache.set(key, { data: response, timestamp: Date.now() });

    // Clean old cache entries (keep it from growing forever)
    if (cache.size > 500) {
      const oldest = [...cache.entries()].sort((a, b) => a[1].timestamp - b[1].timestamp);
      for (let i = 0; i < 100; i++) cache.delete(oldest[i][0]);
    }

    return res.status(200).json(response);
  } catch (err) {
    console.error("API error:", err.message);
    return res.status(500).json({ error: err.message });
  }
}
