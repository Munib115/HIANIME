import sqlite3, requests, bs4, re, time
from scrape_hentai import scrape_single_anime, sync_db_copies, DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("SELECT anime_id FROM anime")
db_ids = set(r[0] for r in c.fetchall())

erotica_targets = []
for p in range(1, 4):
    r = requests.get(f"https://hianime.at/genres/erotica?page={p}", headers={"User-Agent": "Mozilla/5.0"})
    soup = bs4.BeautifulSoup(r.text, "html.parser")
    for it in soup.select(".film_list-wrap .flw-item"):
        a = it.select_one(".film-name a")
        if not a: continue
        href = a["href"]
        full_url = href if href.startswith("http") else "https://hianime.at" + href
        m = re.search(r"-(\d+)$", href)
        if m and int(m.group(1)) not in db_ids:
            erotica_targets.append({
                "anime_id": int(m.group(1)),
                "title": a.text.strip(),
                "url": full_url
            })

print(f"Scraping {len(erotica_targets)} missing erotica anime...")
for item in erotica_targets:
    res = scrape_single_anime(item)
    if res:
        aid, meta, eps, streams, al = res
        genres_str = ", ".join(meta.get("genres", []))
        c.execute("""
            INSERT OR REPLACE INTO anime (
                anime_id, hianime_url, title, japanese_name, synonyms, type, status,
                aired, premiered, mal_score, studios, producers, duration, genres,
                synopsis, poster_url, category, listing_page, episode_count,
                anilist_id, title_romaji, title_english, title_native,
                anilist_score, anilist_popularity, poster_large, banner_image,
                anilist_status, al_episodes, season, season_year, al_genres,
                al_description, scraped_at, enriched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            aid, meta.get("hianime_url", ""), meta["title"], meta["japanese_name"], meta["synonyms"],
            meta["type"], meta["status"], meta["aired"], meta["premiered"], meta["mal_score"],
            meta["studios"], meta["producers"], meta["duration"], genres_str, meta["synopsis"],
            meta["poster_url"], "popular", 1, len(eps),
            al.get("anilist_id") if al else None, al.get("title_romaji", "") if al else "",
            al.get("title_english", "") if al else "", al.get("title_native", "") if al else "",
            al.get("anilist_score") if al else None, al.get("anilist_popularity") if al else None,
            al.get("poster_large", "") if al else "", al.get("banner_image", "") if al else "",
            al.get("anilist_status") if al else None, al.get("episodes") if al else None,
            al.get("season", "") if al else "", al.get("season_year") if al else None,
            al.get("al_genres", "") if al else "", al.get("al_description", "") if al else "",
            int(time.time()), int(time.time()) if al else None
        ))
        for ep in eps:
            c.execute("INSERT OR IGNORE INTO episodes (anime_id, episode_id, episode_number, title, japanese_title, watch_url) VALUES (?,?,?,?,?,?)",
                (aid, ep["episode_id"], ep["episode_number"], ep["title"], ep["japanese_title"], ep["watch_url"]))
        for s in streams:
            c.execute("INSERT OR IGNORE INTO streams (anime_id, anime_title, episode_id, episode_number, episode_title, type, server_name, provider, embed_url) VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, meta["title"], s["episode_id"], s["episode_number"], s["episode_title"], s["type"], s["server_name"], s["provider"], s["embed_url"]))
        conn.commit()
        print(f"Added: {meta['title']} ({len(eps)} eps, {len(streams)} streams)")

conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
conn.close()
sync_db_copies()
print("Erotica sync complete!")
