# 🤠 The Idiot Roundup

**Find every repost of your short-form video across the internet.**

Paste a TikTok, Instagram Reel, or YouTube Short link — The Idiot Roundup uses AI-powered web search to track down every account that reposted your content.

---

## Quick Deploy

### 1. Get an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Sign up or log in
3. Navigate to **Settings → API Keys**
4. Click **Create Key** and copy it — you'll need it in step 3

### 2. Push to GitHub

```bash
# Create a new repo on GitHub, then:
git init
git add .
git commit -m "Initial commit - The Idiot Roundup"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/the-idiot-roundup.git
git push -u origin main
```

### 3. Deploy on Vercel

1. Go to [vercel.com](https://vercel.com) and sign in with GitHub
2. Click **"Add New Project"**
3. Import your `the-idiot-roundup` repository
4. Under **Environment Variables**, add:
   - **Name:** `ANTHROPIC_API_KEY`
   - **Value:** your API key from step 1
5. Click **Deploy**

That's it! Your site will be live at `https://the-idiot-roundup.vercel.app` (or similar).

---

## Project Structure

```
the-idiot-roundup/
├── public/
│   └── index.html        # Frontend UI
├── api/
│   └── search.js          # Serverless API (proxies to Anthropic)
├── package.json
├── vercel.json             # Vercel routing config
├── .env.example
└── .gitignore
```

## How It Works

1. **You paste a video URL** → the frontend extracts metadata from the link
2. **Perceptual fingerprint** → generates a hash to identify the video
3. **AI search** → the `/api/search` serverless function calls Claude with web search enabled to find reposts
4. **Results** → matched reposts are displayed with platform, account, confidence score, and direct links

## Local Development

```bash
npm install
cp .env.example .env       # Add your API key
npx vercel dev              # Runs at http://localhost:3000
```

## Future Improvements

- **Real video fingerprinting** with pHash/dHash on actual video frames
- **yt-dlp integration** to download and analyze source videos
- **Google Vision API** for reverse image search on thumbnails
- **Platform APIs** (TikTok, YouTube, Instagram) for deeper searches
- **Database** to cache results and monitor for new reposts over time
- **Email alerts** when new reposts are detected

---

Built with spite and determination. 🤠
