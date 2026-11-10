# KuroAnime — Stream Anime in Sub & Dub

[![Live Website](https://img.shields.io/badge/Live_Site-kuroanime--flax.vercel.app-E50914?style=for-the-badge&logo=vercel)](https://kuroanime-flax.vercel.app/)
[![Vite](https://img.shields.io/badge/Frontend-Vite_SPA-646CFF?style=for-the-badge&logo=vite)](https://vitejs.dev/)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com/)
[![SQLite](https://img.shields.io/badge/Database-SQLite_6000+_Anime-003B57?style=for-the-badge&logo=sqlite)](https://sqlite.org/)
[![Supabase](https://img.shields.io/badge/Realtime-Supabase_Cloud-3FCF8E?style=for-the-badge&logo=supabase)](https://supabase.com/)

> **Watch anime online in HD with Japanese audio + English subtitles and English dub.** 
> Fast multi-server video player, synchronized episode discussions, responsive mobile UI, and zero mandatory subscription paywalls.

🌐 **Production URL:** [https://kuroanime-flax.vercel.app/](https://kuroanime-flax.vercel.app/)

---

## 🌟 Key Features

- ⚡ **6,000+ Anime Titles & 189,000+ Verified Streams**: Stream the latest seasonal releases (One Piece, Bleach: Thousand-Year Blood War, Jujutsu Kaisen, Solo Leveling, Demon Slayer) and retro classics.
- 🎙️ **Dual Audio (Sub & Dub)**: Switch between original Japanese audio with clear English subtitles and English dub streams.
- 💬 **Real-Time Episode Discussions**: Live global episode chat powered by Supabase with spoiler protection, photo attachments, and voice notes.
- 🎯 **Advanced Instant Search & Catalog Discovery**: Search by English title, Romaji, or native Japanese name with genre filters and status sorting.
- 📱 **Mobile-First & Clean UI**: Fully responsive, GPU-accelerated skeleton loaders, and YouTube-style buffering states.
- 🔍 **SEO & Search Engine Optimized**: Semantic anchor routing, dynamic Open Graph & Twitter Cards, Schema.org `TVSeries` & `FAQPage` JSON-LD, and an automated 6,000+ URL XML sitemap.

---

## 🚀 Architecture & Tech Stack

- **Frontend**: Vanilla JavaScript (ES Modules), Vite 8, Lucide Icons, Hls.js
- **Backend API**: Python FastAPI, Uvicorn, SQLite (`hianime.db`)
- **Realtime Discussions & User Auth**: Supabase Auth & Storage
- **Deployment**: Vercel (Edge CDN + Serverless API)

---

## 💻 Local Development

### 1. Clone the repository
```bash
git clone https://github.com/Munib115/HIANIME.git
cd HIANIME
```

### 2. Run the Backend API
```bash
python -m uvicorn anime_app.server.api:app --host 127.0.0.1 --port 8000 --reload
```

### 3. Run the Frontend
```bash
cd anime_app/web
npm install
npm run dev
```

---

## 📄 License & Attribution
Designed and built for anime enthusiasts. Educational and portfolio project.
