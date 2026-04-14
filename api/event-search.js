import Anthropic from "@anthropic-ai/sdk";
import crypto from "crypto";

// ── In-memory cache ──
const cache = new Map();
const CACHE_TTL = 24 * 60 * 60 * 1000;

function getCacheKey(body) {
  const normalized = JSON.stringify(body, Object.keys(body).sort());
  return crypto.createHash("md5").update(normalized).digest("hex");
}

// ── Master account list ──
const OWN_ACCOUNTS = new Set([
  "americanharddrive","greatamericanbarscene","morezachbryan","oklahomanoutlaw",
  "runnyeggsz","withheavenontok","zachbryanarchive","harleycarmichael",
  "ellalangleyarchive","ellalangleyextras","ellalangleylately","fellas4ella","langleyloyalists",
  "moreole60","ole60archive","ole60fans","ole60vault",
  "isabelledavis97","joshuaslonearchive","joshuaslonenation","morejoshuaslone",
  "phil.kane.hq","philkanehq",
  "gabriellarosearchive",
  "barnburners","colt.johnsontx","folktunez","graciekahan","gunnarhendo",
  "harmonyandtwang","kaylaalist","maddiespamzzzz99","oklahomasmokeshow02",
  "roadshowrecap","spamrynnnn"
]);

// ── Own account detection ──
function splitResults(results) {
  const own = [];
  const others = [];
  for (const r of results) {
    const name = (r.account_name || "").replace(/^@/, "").toLowerCase();
    const url = (r.url || "").toLowerCase();
    let isOwn = false;
    for (const acct of OWN_ACCOUNTS) {
      if (name.includes(acct) || url.includes(`/@${acct}`) || url.includes(`/${acct}`)) {
        isOwn = true;
        break;
      }
    }
    (isOwn ? own : others).push(r);
  }
  return { own, others };
}

// ── Tally stats ──
function tallyStats(results) {
  const t = { plays: 0, likes: 0, comments: 0, shares: 0, count: results.length };
  for (const r of results) {
    const s = r.stats || {};
    t.plays += s.plays || 0;
    t.likes += s.likes || 0;
    t.comments += s.comments || 0;
    t.shares += s.shares || 0;
  }
  return t;
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
    const venue = (body.venue || "").trim();
    const date = (body.date || "").trim();
    const description = (body.description || "").trim();
    const keywords = (body.keywords || "").trim();
    const timeframe = (body.timeframe || "").trim();

    if (!event || !description) {
      return res.status(400).json({ error: "Missing event name or description" });
    }

    // ── Check cache ──
    const key = getCacheKey(body);
    const cached = cache.get(key);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
      return res.status(200).json(cached.data);
    }

    // ── Build a STRICT, location-enforced prompt ──
    const client = new Anthropic({ apiKey });

    const systemPrompt = `You find videos and coverage of ONE SPECIFIC event at ONE SPECIFIC venue. You must ONLY return results from this exact event — not from other dates, other cities, or other venues.

CRITICAL RULES:
- ONLY include results that are specifically about the event at the venue and date specified
- REJECT any result from a different city, venue, or date — even if it involves the same artist
- If a video is from a different show on a different date, DO NOT include it
- When in doubt, leave it out — accuracy matters more than quantity

Return ONLY a JSON array. Each object must have:
- platform (tiktok/instagram/youtube/facebook/twitter/other)
- account_name (@user or publication name)
- url (direct link to the specific video/article)
- description (brief description mentioning the venue/city)
- confidence (high/medium — only include high confidence matches)
- date_found (YYYY-MM-DD)`;

    const userPrompt = `Find ALL videos and coverage of this SPECIFIC event:

EVENT: ${event}
VENUE: ${venue}
DATE: ${date}
TIMEFRAME: ${timeframe}
WHAT HAPPENED: ${description}
KEYWORDS: ${keywords}

IMPORTANT: I ONLY want results from ${venue}${date ? ` on ${date}` : ""}. Do NOT include videos from other shows, other cities, or other dates. Search TikTok, Instagram, YouTube, Twitter/X, and news sites.

Search for these specific terms:
${keywords ? keywords.split(",").map(k => `- "${k.trim()}${venue ? ` ${venue.split(",")[0].trim()}` : ""}"`).join("\n") : ""}
- "${event} ${venue}"
${description ? `- "${description.split(" ").slice(0, 5).join(" ")}"` : ""}

Return ONLY videos/articles confirmed to be from ${venue}. If you're not sure a result is from this specific event, do NOT include it.`;

    console.log(`[event-search] Searching: ${event} at ${venue}`);

    const message = await client.messages.create({
      model: "claude-sonnet-4-20250514",
      max_tokens: 8192,
      system: systemPrompt,
      messages: [{ role: "user", content: userPrompt }],
      tools: [{ type: "web_search_20250305", name: "web_search" }],
    });

    // Extract results
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

    console.log(`[event-search] Found ${results.length} results`);

    // ── Split own vs others ──
    const { own, others } = splitResults(results);
    const ownTotals = tallyStats(own);
    const otherTotals = tallyStats(others);
    const totals = {
      plays: ownTotals.plays + otherTotals.plays,
      likes: ownTotals.likes + otherTotals.likes,
      comments: ownTotals.comments + otherTotals.comments,
      shares: ownTotals.shares + otherTotals.shares,
    };

    const response = {
      results: others,
      own_posts: own,
      total: results.length,
      totals,
      own_totals: ownTotals,
      other_totals: otherTotals,
    };

    // ── Cache ──
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
