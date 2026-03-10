# 🤠 The Idiot Roundup

**Find every repost of your stolen videos across the internet.**

Paste a TikTok, Instagram Reel, or YouTube Short link — The Idiot Roundup uses AI-powered web search to track down every account that reposted your content, plus news articles and reaction videos.

---

## What It Does

- Searches TikTok, Instagram, YouTube, Facebook, Twitter/X for video reposts
- Finds news articles, blog posts, and media coverage about your video
- Pulls real video metadata and thumbnails via oEmbed APIs
- Groups results by platform with confidence scoring
- Caches results so the same URL doesn't burn API credits twice

---

## Quick Deploy on Vercel

### 1. Get an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Navigate to **Settings → API Keys**
3. Click **Create Key** and copy it

### 2. Deploy

1. Push this repo to GitHub
2. Go to [vercel.com/new](https://vercel.com/new) and import the repo
3. Add environment variable: `ANTHROPIC_API_KEY` = your key
4. Click **Deploy**

Your site will be live at `https://the-idiot-roundup.vercel.app` (or similar).

---

## Run Locally

```bash
# Install Python dependency
pip3 install requests

# Set your API key and run
ANTHROPIC_API_KEY=sk-ant-your-key-here python3 server.py
```

Open **http://localhost:3000** and start rounding up idiots.

---

## Project Structure

```
the-idiot-roundup/
├── public/
│   └── index.html          # Frontend UI
├── api/
│   └── search.js           # Vercel serverless function
├── server.py               # Local Python server
├── package.json
├── vercel.json              # Vercel routing config
├── .env.example
└── .gitignore
```

## Tech Stack

- **Frontend:** Single-file HTML/CSS/JS — dark theme, mobile responsive
- **Search:** Anthropic Claude API with web search tool
- **Metadata:** TikTok, YouTube, and Instagram oEmbed APIs
- **Deploy:** Vercel serverless functions
- **Local:** Python stdlib HTTP server + requests

---

Built with spite and determination. 🤠
