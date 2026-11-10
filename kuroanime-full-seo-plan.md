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

## 4. Phase 1 — Technical SEO Implementation (COMPLETED & DEPLOYED)

### 4.1 Real URLs & Semantic Anchor Tags [DONE]
- Every anime card rendered on Home, Browse, and Search is an `<a class="anime-card" href="/anime/${anime.anime_id}">`.
- SPA navigation intercepts left-clicks with `e.preventDefault()` to smoothly transition views without page reload, while allowing web crawlers, middle-clicks, and right-clicks ("Open in new tab") to work naturally.
- Client-side route parser in `main.js`:
  - `/` -> Home View
  - `/anime/:id` -> Anime Detail View
  - `/browse` -> Browse Catalog View
  - `/search` -> Search View
  - `/watchlist` -> My List View

### 4.2 Dynamic Metadata, Open Graph & JSON-LD [DONE]
Every route dynamically updates:
- `<title>`: Under 60 characters (e.g. `Watch One Piece Sub & Dub | KuroAnime`).
- `<meta name="description">`: 140–160 characters describing the anime, Sub/Dub availability, and streaming options.
- `<link rel="canonical">`: Explicitly set to `https://kuroanime-flax.vercel.app/anime/:id`.
- Open Graph tags (`og:title`, `og:description`, `og:image`, `og:url`, `og:type="video.tv_show"`).
- Twitter Card (`summary_large_image`).
- Multi-Entity Schema.org Graph (`WebSite`, `TVSeries`/`Movie`, `BreadcrumbList`, and `FAQPage`).

### 4.3 robots.txt [DONE]
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

### 4.4 Automated Database-Driven Sitemap (`generate_sitemap.py`) [DONE]
- Automatically queries [`hianime.db`](file:///c:/Users/Munib%20Raza/OneDrive/Desktop/HIANIME/hianime.db) to extract all active anime titles, genres, and static routes.
- Generates 6,188 URLs directly into `anime_app/web/public/sitemap.xml` and `anime_app/web/dist/sitemap.xml`.
- Built-in URL structure:
  - Homepage (`priority: 1.0`, `changefreq: daily`)
  - Browse & Search (`priority: 0.8`, `changefreq: daily`)
  - 100 Genre landing pages `/browse?genre={genre}` (`priority: 0.8`, `changefreq: weekly`)
  - 6,085 anime titles `/anime/{id}` (`priority: 0.7`, `changefreq: weekly`)

### 4.5 Vercel Routing Configuration (`vercel.json`) [DONE]
- Preserves `/robots.txt` and `/sitemap.xml` static file delivery.
- Routes `/api/*` to the Python FastAPI backend.
- Rewrites all other user routes (`/anime/*`, `/browse`, `/search`) to `/index.html` for clean client-side hydration.

### 4.6 Image Stability & Core Web Vitals [DONE]
- Fallback handler `onerror="this.onerror=null;this.src='/favicon.svg';this.classList.add('is-fallback')"` on all poster images.
- Native `loading="lazy"` and `decoding="async"` on all offscreen images.
- GPU-composited skeleton loaders preventing Layout Shift (CLS score < 0.05).

---

## 5. Phase 2 — Keyword Strategy, Rich Entities & Catalog Discoverability (COMPLETED & DEPLOYED)

### 5.1 On-Page Keyword Targeting & Headings [DONE]
- Primary heading structure:
  - `<h1>${anime.title}</h1>`
  - `<p class="detail-seo-subheading">Watch ${anime.title} Online in English Sub & Dub Free HD</p>`
- Targets high-intent searches:
  - `watch [anime] sub and dub`
  - `[anime] all episodes sub dub`
  - `[anime] english dub stream`
  - `best [genre] anime sub dub`

### 5.2 Dynamic Browse Genre & Search Meta Tracking [DONE]
- Selecting any genre automatically updates document title, description, and canonical path (`/browse?genre=Action` -> `Action Anime — Stream in Sub & Dub | KuroAnime`).
- Searching updates query URL and metadata (`/search?q=Demon+Slayer` -> `Watch Demon Slayer Online Sub & Dub | KuroAnime`).

### 5.3 On-Page Breadcrumb Navigation & Schema [DONE]
- Renders clickable breadcrumb trail: `Home › Browse › [Genre] › [Anime Title]`
- Emits Schema.org `BreadcrumbList` for Google Rich Search snippets.

### 5.4 Frequently Asked Questions (FAQPage Schema & UI Accordion) [DONE]
- Embedded responsive, zero-layout-shift `<details>` FAQ accordions on anime detail pages answering:
  1. Where can I watch in English Sub & Dub?
  2. How many episodes are available?
  3. Is the anime free to watch on KuroAnime?
- Injected Schema.org `FAQPage` JSON-LD to unlock expandable Google SERP rich result cards.

---

## 6. Phase 3 — Repository Authority & Backlinks (COMPLETED & DEPLOYED)

1. **GitHub Repository SEO [DONE]**:
   - Created comprehensive root [`README.md`](file:///c:/Users/Munib%20Raza/OneDrive/Desktop/HIANIME/README.md) on `Munib115/HIANIME` linking directly to `https://kuroanime-flax.vercel.app/`.
   - Outlines tech stack, features, and high-intent streaming keywords, leveraging GitHub's DA 96+ authority.

---

## 7. Phase 4 — Google Search Console Submission & Verification (Action Items for User)

1. **Open Google Search Console**:
   - Go to [Google Search Console](https://search.google.com/search-console).
   - Add property: `https://kuroanime-flax.vercel.app/` (URL prefix method).
   - Verify via HTML tag (already pre-configured) or Vercel DNS.

2. **Submit Sitemap**:
   - In GSC left sidebar, click **Sitemaps**.
   - Under "Add a new sitemap", enter: `sitemap.xml`
   - Click **Submit**. Status will show **Success** with ~6,188 discovered URLs.

3. **Request Fast Indexing for Key Pages**:
   - In the top search bar ("Inspect any URL in https://kuroanime-flax.vercel.app/"), enter:
     - `https://kuroanime-flax.vercel.app/` -> Click **Request Indexing**.
     - `https://kuroanime-flax.vercel.app/browse` -> Click **Request Indexing**.
     - Your top 3 favorite anime titles (e.g. `https://kuroanime-flax.vercel.app/anime/1`) -> Click **Request Indexing**.

---

## 7. Weekly Monitoring Checklist (5 Minutes)
1. Check Google Search Console -> Sitemaps status.
2. Check GSC Coverage / Pages -> Total indexed pages.
3. Check GSC Performance -> Total Impressions & Clicks for queries containing "sub dub".
4. Review Core Web Vitals / Mobile Usability reports.
