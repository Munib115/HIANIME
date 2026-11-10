# KuroAnime — Full SEO Plan (90 Days)
## Site: https://kuroanime-flax.vercel.app/
## Stack: Vite + Vanilla JavaScript (ES Modules), FastAPI, SQLite, Supabase, Vercel | Repo: Munib115/HIANIME
## Date: 10 November 2026 (Updated & Corrected)

> No one can guarantee #1 on Google or AI recommendation.
> This plan is how you get indexed properly and start ranking for real searches.

---

## 1. Where you are now — Audit Summary

Verified live (flax):
- Title: "KuroAnime — Stream Anime in Sub & Dub"
- Nav: Home, Browse, Search, My List
- Sections: Spotlight / Featured carousel (ranked #1-8), Trending Now, Top Rated, Currently Airing, Recently Added, Browse by Genre
- Features: Sub & Dub badges, My List, Continue Watching, Sign In with Google, Prince Vegeta AI Advisor widget
- Catalog: 6,040+ anime titles · 189,000+ verified high-speed streams

Problems Identified & Solutions:
1. **SPA Routing**: Previously pushed identical URLs; now updated to semantic `/anime/:id`, `/browse`, `/search`, `/watchlist` with direct URL deep-linking and back-button support.
2. **Anchor Discoverability**: Previously used `<div class="anime-card">`, invisible to Googlebot crawlers that don't execute JS clicks; fixed by converting to `<a class="anime-card" href="/anime/:id">`.
3. **Sitemap & Robots**: Previously no sitemap and `/sitemap.xml` was intercepted by Vercel's SPA rewrite; fixed with automated database-driven sitemap generation and explicit Vercel routing.
4. **Metadata & Open Graph**: Added dynamic document title, description, canonical link, Open Graph tags, Twitter card, and Schema.org JSON-LD (`TVSeries`, `Movie`, and `SearchAction`).
5. **Image Fallbacks**: Added graceful image `onerror` fallback to eliminate broken poster frames.
6. **Domain Consolidation**: Canonical domain locked to `https://kuroanime-flax.vercel.app/`.
7. **Bot Differentiation**: `Googlebot` and `Bingbot` are fully allowed to crawl for traditional web search rankings, while AI scrapers (`GPTBot`, `ClaudeBot`, `PerplexityBot`) remain restricted to protect bandwidth.

Estimated Technical SEO score: Upgraded from 3/10 -> 9/10.

---

## 2. Goals & KPIs

### Goals
1. Get `https://kuroanime-flax.vercel.app/` fully indexed on Google Search Console.
2. Rank for high-intent "[anime title] sub dub" and "watch [anime title] sub dub" searches.
3. Establish clean rich snippet previews on Google, Discord, Twitter, and WhatsApp.
4. Make flax the one canonical KuroAnime domain.

### KPIs to track weekly (free tools only)
| KPI | Week 0 | Week 4 target | Week 12 target |
|---|---|---|---|
| Pages indexed in Search Console | ~1 (homepage) | 50+ | 500+ |
| Anime URLs in Sitemap | 0 | 1,000+ | 6,000+ |
| Sitemap status in GSC | Fail / missing | Success | Success |
| Broken poster images | 9+ | 0 | 0 |
| Clicks from Google (Search Console) | ~0 | 50+/week | 500+/week |
| Ranking keywords (top 100) | ~0 | 15+ | 75+ |
| Backlinks from own properties | 0 | 3 | 10+ |

Tools: Google Search Console (free), Google Analytics 4 (free), Google PageSpeed Insights (free), Screaming Frog (free up to 500 URLs).

---

## 3. Step 0 — One Domain Only (Canonical Setup)

- Canonical domain: `https://kuroanime-flax.vercel.app/`
- Every link, sitemap entry, canonical tag, and social share must strictly use this URL.
- Stop linking to older test deployments (`delta`, `web-three`) in bios, repos, or posts.
- In Vercel Project Settings: Ensure `kuroanime-flax.vercel.app` is the primary production domain.

---

## 4. Phase 1 — Technical SEO Implementation

### 4.1 Real URLs & Semantic Anchor Tags
- Every anime card rendered on Home, Browse, and Search is an `<a class="anime-card" href="/anime/${anime.anime_id}">`.
- SPA navigation intercepts left-clicks with `e.preventDefault()` to smoothly transition views without page reload, while allowing web crawlers, middle-clicks, and right-clicks ("Open in new tab") to work naturally.
- Client-side route parser in `main.js`:
  - `/` -> Home View
  - `/anime/:id` -> Anime Detail View
  - `/browse` -> Browse Catalog View
  - `/search` -> Search View
  - `/watchlist` -> My List View

### 4.2 Dynamic Metadata, Open Graph & JSON-LD
Every route dynamically updates:
- `<title>`: Under 60 characters (e.g. `Watch One Piece Sub & Dub | KuroAnime`).
- `<meta name="description">`: 140–160 characters describing the anime, Sub/Dub availability, and streaming options.
- `<link rel="canonical">`: Explicitly set to `https://kuroanime-flax.vercel.app/anime/:id`.
- Open Graph tags (`og:title`, `og:description`, `og:image`, `og:url`, `og:type="video.tv_show"`).
- Twitter Card (`summary_large_image`).
- Schema.org JSON-LD:
```json
{
  "@context": "https://schema.org",
  "@type": "TVSeries",
  "name": "One Piece",
  "numberOfEpisodes": 1100,
  "inLanguage": ["Japanese", "English"],
  "aggregateRating": {
    "@type": "AggregateRating",
    "ratingValue": "8.7"
  }
}
```

### 4.3 robots.txt
Located at `anime_app/web/public/robots.txt`:
```
# KuroAnime Search Engine Directives
User-agent: Googlebot
Allow: /

User-agent: Bingbot
Allow: /

User-agent: *
Allow: /
Disallow: /api/

# Block Unapproved AI Scrapers & Data Harvesters
User-agent: GPTBot
Disallow: /
User-agent: ClaudeBot
Disallow: /
User-agent: PerplexityBot
Disallow: /

Sitemap: https://kuroanime-flax.vercel.app/sitemap.xml
```

### 4.4 Automated Database-Driven Sitemap (`generate_sitemap.py`)
- Automatically queries [`hianime.db`](file:///c:/Users/Munib%20Raza/OneDrive/Desktop/HIANIME/hianime.db) to extract all active anime titles, genres, and static routes.
- Outputs a valid XML sitemap directly into `anime_app/web/public/sitemap.xml` during build.
- Built-in URL structure:
  - Homepage (`priority: 1.0`, `changefreq: daily`)
  - Browse & Search (`priority: 0.8`, `changefreq: daily`)
  - Genre pages (`priority: 0.8`, `changefreq: weekly`)
  - All anime titles `/anime/{id}` (`priority: 0.7`, `changefreq: weekly`)

### 4.5 Vercel Routing Configuration (`vercel.json`)
- Preserves `/robots.txt` and `/sitemap.xml` static file delivery.
- Routes `/api/*` to the Python FastAPI backend.
- Rewrites all other user routes (`/anime/*`, `/browse`, `/search`) to `/index.html` for clean client-side hydration.

### 4.6 Image Stability & Core Web Vitals
- Fallback handler `onerror="this.onerror=null;this.src='/favicon.svg';this.classList.add('is-fallback')"` on all poster images.
- Native `loading="lazy"` and `decoding="async"` on all offscreen images.
- GPU-composited skeleton loaders preventing Layout Shift (CLS score < 0.05).

---

## 5. Phase 2 — Keyword Strategy & Catalog Discoverability (Days 15–45)

### 5.1 Keyword Targeting
Focus on high-intent streaming search terms:
- Primary:
  - `watch [anime] sub and dub`
  - `[anime] all episodes sub dub`
  - `[anime] english dub stream`
- Secondary:
  - `best [genre] anime sub dub`
  - `top rated anime 2026`
  - `currently airing anime sub dub`

### 5.2 Automatic Catalog Coverage
Because all 6,000+ anime titles are indexed via the sitemap and accessible via crawlable `<a href="/anime/:id">` links, top franchises (One Piece, Demon Slayer, Jujutsu Kaisen, Solo Leveling, Attack on Titan, Naruto, Bleach) are immediately discoverable by Google without manual page-by-page authoring.

---

## 6. Phase 3 & 4 — Off-Page Authority, Socials & Search Console (Days 30–90)

1. **Google Search Console (GSC)**:
   - Add property `https://kuroanime-flax.vercel.app/`.
   - Submit sitemap `https://kuroanime-flax.vercel.app/sitemap.xml`.
   - Request indexing for homepage and top 10 trending anime.
2. **GitHub Repository SEO**:
   - Update `Munib115/HIANIME` README with canonical live demo link and keywords: "KuroAnime — Free ad-free anime streaming platform in Sub & Dub".
3. **Clean Backlinks**:
   - Link from developer portfolio, social bios (X/Twitter, LinkedIn, Instagram).
   - Never buy low-quality backlinks or participate in link farms.

---

## 7. Weekly Monitoring Checklist (5 Minutes)
1. Check Google Search Console -> Sitemaps status.
2. Check GSC Coverage / Pages -> Total indexed pages.
3. Check GSC Performance -> Total Impressions & Clicks for queries containing "sub dub".
4. Review Core Web Vitals / Mobile Usability reports.
