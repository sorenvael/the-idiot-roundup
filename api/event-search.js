import Anthropic from "@anthropic-ai/sdk";
import crypto from "crypto";

// ── In-memory cache (persists per serverless instance) ──
const cache = new Map();
const CACHE_TTL = 24 * 60 * 60 * 1000; // 24 hours

function getCacheKey(body) {
  // Hash ALL fields so different prompts = different results
  const normalized = JSON.stringify(body, Object.keys(body).sort());
  return crypto.createHash("md5").update(normalized).digest("hex");
}

// ── Dynamic query builder ──
function buildSearchQueries(eventInfo) {
  const event = (eventInfo.event || "").trim();
  const venue = (eventInfo.venue || "").trim();
  const description = (eventInfo.description || "").trim();
  const keywords = (eventInfo.keywords || "")
    .split(",")
    .map((k) => k.trim())
    .filter(Boolean);

  const queries = [];
  const seen = new Set();

  function add(q) {
    q = q.trim();
    if (q && !seen.has(q.toLowerCase())) {
      seen.add(q.toLowerCase());
      queries.push(q);
    }
  }

  if (event) add(event);
  if (event && venue) {
    const venueShort = venue.split(",")[0].trim();
    add(`${event} ${venueShort}`);
  }
  for (const kw of keywords) {
    add(kw);
    if (event) add(`${event} ${kw}`);
  }
  // Extract short phrases from description
  if (description) {
    const phrases = description
      .split(/[,.]/)
      .map((p) => p.trim())
      .filter((p) => p.length > 3);
    for (const phrase of phrases.slice(0, 3)) {
      const words = phrase.split(/\s+/).slice(0, 4);
      add(words.join(" "));
    }
  }

  return queries;
}

// ── Anthropic search for event coverage ──
const SYSTEM_PROMPT = `You find videos and coverage of specific events/moments. Return ONLY a JSON array.

Each object must have: platform (tiktok/instagram/youtube/facebook/twitter/other), account_name (@user or publication name), url (direct link), confidence (high/medium), description (brief), date_found (YYYY-MM-DD).

Include:
- Videos from the event on TikTok, Instagram, YouTube, Facebook, Twitter/X
- News articles, blog posts, and media coverage about the event/moment
- Fan-recorded videos and reactions

Search thoroughly. Return ONLY the JSON array, nothing else.`;

async function searchEvent(eventInfo, apiKey) {
  const client = new Anthropic({ apiKey });
  const event = eventInfo.event || "";
  const venue = eventInfo.venue || "";
  const description = eventInfo.description || "";
  const keywords = eventInfo.keywords || "";
  const date = eventInfo.date || "";

  const prompt = `Find all videos and coverage of this event/moment:

Event: ${event}
Venue: ${venue}
Date: ${date}
Description: ${description}
Keywords: ${keywords}

Search for: videos from different angles, news articles, fan recordings, reaction videos. Be thorough — search by event name, venue, keywords, and description. Return as many results as you can find.`;

  const message = await client.messages.create({
    model: "claude-sonnet-4-20250514",
    max_tokens: 4096,
    system: SYSTEM_PROMPT,
    messages: [{ role: "user", content: prompt }],
    tools: [{ type: "web_search_20250305", name: "web_search" }],
  });

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

  return results;
}

// ── Own account detection ──
function splitResults(results, ownAccountsStr) {
  const ownAccounts = new Set(
    (ownAccountsStr || "")
      .split(",")
      .map((a) => a.trim().replace(/^@/, "").toLowerCase())
      .filter(Boolean)
  );

  const own = [];
  const others = [];
  for (const r of results) {
    const name = (r.account_name || "").replace(/^@/, "").toLowerCase();
    const url = (r.url || "").toLowerCase();
    let isOwn = false;
    for (const acct of ownAccounts) {
      if (name.includes(acct) || url.includes(`/@${acct}`) || url.includes(`/${acct}`)) {
        isOwn = true;
        break;
      }
    }
    (isOwn ? own : others).push(r);
  }
  return { own, others };
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return res.status(500).json({ error: "ANTHROPIC_API_KEY not configured" });
  }

  try {
    const body = req.body;
    const event = (body.event || "").trim();
    const description = (body.description || "").trim();

    if (!event || !description) {
      return res.status(400).json({ error: "Missing event name or description" });
    }

    // ── Check cache (uses ALL fields) ──
    const key = getCacheKey(body);
    const cached = cache.get(key);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
      console.log(`[cache] HIT for event "${event.slice(0, 40)}"`);
      return res.status(200).json(cached.data);
    }

    // ── Search with Anthropic API ──
    console.log(`[event-search] Searching for: ${event}`);
    const results = await searchEvent(body, apiKey);
    console.log(`[event-search] Found ${results.length} results`);

    // ── Split own vs others ──
    const { own, others } = splitResults(results, body.own_accounts);

    // ── Calculate totals ──
    const totals = { plays: 0, likes: 0, comments: 0, shares: 0 };
    for (const r of results) {
      const s = r.stats || {};
      totals.plays += s.plays || 0;
      totals.likes += s.likes || 0;
      totals.comments += s.comments || 0;
      totals.shares += s.shares || 0;
    }

    const response = {
      results: others,
      own_posts: own,
      total: results.length,
      totals,
    };

    // ── Save to cache ──
    cache.set(key, { data: response, timestamp: Date.now() });
    if (cache.size > 200) {
      const oldest = [...cache.entries()].sort((a, b) => a[1].timestamp - b[1].timestamp);
      for (let i = 0; i < 50; i++) cache.delete(oldest[i][0]);
    }

    return res.status(200).json(response);
  } catch (err) {
    console.error("Event search error:", err.message);
    return res.status(500).json({ error: err.message });
  }
}
