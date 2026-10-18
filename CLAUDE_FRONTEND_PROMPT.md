# Master Frontend Redesign Prompt & Architecture Specification
> **Instructions for Claude:** Copy and paste the prompt below into Claude 3.5 Sonnet / Opus. It gives Claude the complete backend API contract, data structures, UI/UX design tokens, and functional requirements needed to generate an ultra-premium, modern, mobile-and-desktop anime streaming frontend.

---

```markdown
You are an elite Lead UI/UX Frontend Engineer and Creative Director. Your task is to design and build an ultra-premium, modern, responsive anime streaming web application called **"KUROANIME"** (or another refined brand name) for both Desktop (PC) and Mobile devices.

The frontend must connect directly to our working FastAPI backend (endpoints and JSON schemas are provided below). The resulting application should evoke the visual polish of modern streaming platforms like Netflix, Crunchyroll, Disney+, and Apple TV+.

---

## 1. Visual Identity & Design System

### 🎨 Color Palette (OLED Cinematic Dark Theme)
- **Background Primary:** `#07080B` (Deep OLED Pitch Black)
- **Background Elevated / Surface:** `#0F1117` (Deep slate for cards and panels)
- **Background Glass / Overlay:** `rgba(18, 21, 31, 0.72)` with `backdrop-filter: blur(16px)`
- **Accent Primary (Neon Crimson):** `#FF2A4D` to `#FF5A3C` (Gradient for brand, primary actions, active indicators)
- **Accent Secondary (Electric Violet/Indigo):** `#6366F1` (Subtle secondary badges, ratings, tags)
- **Text Primary:** `#F3F4F8` (Crisp high-contrast white)
- **Text Secondary / Muted:** `#8F96A9` (Subtitles, episode metadata, timestamps)
- **Borders & Dividers:** `rgba(255, 255, 255, 0.08)` (Subtle, sleek outlines)
- **Card Shadows & Glows:** `0 8px 32px rgba(0, 0, 0, 0.6)`, with glowing hover halos: `0 0 25px rgba(255, 42, 77, 0.25)`

### 🔤 Typography & Hierarchy
- **Font Family:** `Satoshi`, `Plus Jakarta Sans`, or `Inter` (Sans-serif)
- **Hero Title:** Bold/Black `2.75rem` - `3.5rem` (clamp for mobile `1.75rem` - `2.25rem`)
- **Rail Section Headers:** Semi-Bold `1.25rem` with glowing pill counters or view-all arrows
- **Card Titles:** Medium `0.92rem` (clamped to 2 lines with `-webkit-line-clamp: 2`)
- **Metadata Badges:** Bold `0.7rem` (all-caps tracking `0.05em`)

### ⚡ Animation & Interactions
- Card hover scale: `transform: translateY(-6px) scale(1.02); transition: all 0.28s cubic-bezier(0.16, 1, 0.3, 1)`
- Glassmorphic header with scroll spy (becomes darker and frosted on scroll)
- Smooth skeleton loaders with shimmering gradients (`skeleton-pulse`)
- Haptic-like micro-animations on clicks and tab switching

---

## 2. Platform Architecture (Mobile & Desktop)

### 🖥️ Desktop (PC) Experience
1. **Sticky Glass Navigation Bar:**
   - Brand mark + Animated logo (`KUROANIME HD`)
   - Navigation links: `Home`, `Browse`, `Search`, `My Collection`
   - Real-time debounced global search bar with instant drop-down results
   - Bookmark icon with live saved-count badge
2. **Cinematic Hero Spotlight (Billboard Slider):**
   - High-resolution wide banner backdrop with dark multi-layer gradients (fade to bottom and left)
   - Badges: `#1 in Catalog`, Score (`★ 8.8`), Format (`TV Series`), Audio Availability (`SUB`, `DUB`)
   - Large title, expandable synopsis (truncated to 3 lines)
   - Dual CTA: `[▶ Watch Episode 1]` (Primary Crimson) and `[+ Save to Watchlist]` / `[ⓘ Details]`
   - Mini thumbnail preview switcher or indicator dots
3. **Netflix-style Content Rails:**
   - Categories: `🔥 Trending Now`, `⭐ Top Rated Anime`, `📺 Airing Releases`, `🎬 Blockbuster Movies`, `✨ Recently Added`
   - Smooth horizontal scroll buttons (`<` and `>`) that fade in on track hover
   - Poster cards displaying:
     - 2:3 aspect ratio anime poster
     - Audio badges: `SUB` (Indigo pill) & `DUB` (Crimson pill)
     - Score pill: `★ 8.5` (Gold)
     - Episode count badge: `Ep 24`
     - Hover quick-action buttons (Play instant, Save to list)
4. **Theater Mode Video Player View:**
   - Immersive 16:9 iframe video player with ambient background glow (canvas blur)
   - Server Switcher Toolbar:
     - Audio toggle: `SUB (Japanese)` vs `DUB (English)`
     - Server selector chips: `Server 1: ZokoAnime (Default HD)`, `Server 2: HD-1 (MegaPlay)`, `Server 3`
     - Reload stream button, Next/Previous episode shortcuts
   - Sequels & Seasons Carousel (instantly switch to Season 2, Season 3, Movies)
   - Horizontal chunked episode drawer (e.g. `1 - 50`, `51 - 100`, etc.) with episode numbers and sub/dub availability indicators
5. **Collection Screen:**
   - Segmented control: `Continue Watching (with progress bar)` vs `Saved Watchlist`
   - Stored in `localStorage` (`kuro_history` and `kuro_watchlist`)

### 📱 Mobile Experience (PWA-Ready Touch First)
1. **Mobile Bottom Tab Bar (Fixed at bottom with safe-area inset):**
   - 4 Touch Items: `Home`, `Browse`, `Search`, `My List` with active glow
2. **Swipeable Touch Carousels:**
   - Elastic scroll with momentum snapping for rails and seasons
3. **Bottom Sheet & Drawers:**
   - When tapping an episode or server, open a clean mobile-friendly bottom sheet
4. **Portrait / Landscape Player:**
   - Responsive player that automatically maximizes in landscape mode with minimal chrome

---

## 3. Backend API Contract (FastAPI on `/api`)

Your code will fetch directly from `/api` (or `import.meta.env.VITE_API_BASE || '/api'`):

### A. Catalog Stats: `GET /api/stats`
```json
{
  "anime": 1056,
  "episodes": 21369,
  "streams": 39849
}
```

### B. Home Spotlight & Rails: `GET /api/home`
```json
{
  "spotlight": [
    {
      "anime_id": 240,
      "title": "Attack on Titan",
      "title_english": "Attack on Titan",
      "type": "TV",
      "status": "Finished Airing",
      "episode_count": 25,
      "anilist_score": 85,
      "mal_score": "8.54",
      "season_year": 2013,
      "poster": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/extralarge/bx16498-C6FPmWm59CyP.jpg",
      "banner_image": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/16498-8jpFCOcDmnei.jpg",
      "synopsis": "Centuries ago, mankind was slaughtered nearly to extinction by monstrous humanoid creatures called Titans...",
      "genres": "Action, Drama, Fantasy, Mystery",
      "sub": true,
      "dub": true
    }
  ],
  "rails": {
    "trending": [ /* Array of card objects */ ],
    "top_rated": [ /* Array of card objects */ ],
    "airing": [ /* Array of card objects */ ],
    "movies": [ /* Array of card objects */ ],
    "recent": [ /* Array of card objects */ ]
  }
}
```

### C. Anime Full Details: `GET /api/anime/{anime_id}`
```json
{
  "anime_id": 240,
  "title": "Attack on Titan",
  "title_english": "Attack on Titan",
  "japanese_name": "進撃の巨人",
  "synonyms": "Shingeki no Kyojin",
  "type": "TV",
  "status": "Finished Airing",
  "aired": "Apr 7, 2013 to Sep 29, 2013",
  "premiered": "Spring 2013",
  "mal_score": "8.54",
  "anilist_score": 85,
  "studios": "Wit Studio",
  "duration": "24m",
  "genres": "Action, Drama, Fantasy",
  "synopsis": "...",
  "poster": "https://...",
  "banner_image": "https://...",
  "episode_count": 25,
  "sub": true,
  "dub": true,
  "seasons": [
    {
      "season_name": "Season 1",
      "season_anime_id": 240,
      "season_poster": "https://...",
      "is_current": 1,
      "available": true
    },
    {
      "season_name": "Season 2",
      "season_anime_id": 112,
      "season_poster": "https://...",
      "is_current": 0,
      "available": true
    }
  ]
}
```

### D. Episode List: `GET /api/anime/{anime_id}/episodes`
```json
{
  "anime_id": 240,
  "episodes": [
    {
      "episode_id": "4402",
      "episode_number": 1,
      "title": "To You, in 2000 Years: The Fall of Shiganshina, Part 1",
      "japanese_title": "二千年後の君へ",
      "sub": 1,
      "dub": 1
    }
  ]
}
```

### E. Video Stream Embeds: `GET /api/episode/{episode_id}/streams?anime_id={anime_id}`
```json
{
  "sub": [
    {
      "type": "sub",
      "server_name": "ZokoAnime",
      "provider": "zokoanime.video",
      "embed_url": "https://zokoanime.video/stream/mal/16498/1/sub"
    }
  ],
  "dub": [
    {
      "type": "dub",
      "server_name": "ZokoAnime",
      "provider": "zokoanime.video",
      "embed_url": "https://zokoanime.video/stream/mal/16498/1/dub"
    }
  ]
}
```
*(Note: Only verified working ZokoAnime stream servers are used).*


### F. Global Search: `GET /api/search?q={query}`
Returns: `{"results": [ /* anime cards */ ]}`

### G. Filter / Catalog Browse: `GET /api/browse?genre={genre}&type={type}&sort={popular|score|az|new}&page={page}`
Returns: `{"page": 1, "results": [ /* anime cards */ ]}`

### H. Genre List: `GET /api/genres`
Returns: `{"genres": ["Action", "Adventure", "Comedy", "Fantasy", ... ]}`

---

## 4. Technical Constraints & Deliverables
- **Framework:** Standard HTML5, Modular JavaScript (ES Modules), and pure Vanilla CSS.
- **Icons:** Use Lucide icons (via CDN or npm `lucide`).
- **Resilience:** If the API fails, show an elegant retry card with diagnostic feedback instead of a frozen screen.
- **Player Security / Sandboxing:** The video player should use `<iframe class="video-iframe" src="..." allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; fullscreen" allowfullscreen scrolling="no"></iframe>`. Include `<meta name="referrer" content="no-referrer" />` in `<head>`.
- **Zero Placeholder Broken Images:** Fall back gracefully to `anime.poster || anime.poster_large || anime.poster_url`.

Please generate:
1. Complete, polished `index.html`
2. Complete, stunning `style.css` (with responsive breakpoints for mobile <= 768px and desktop >= 1024px)
3. Complete, bug-free `main.js` with full view routing, state management, audio/server switcher, and watch history tracking.
```
