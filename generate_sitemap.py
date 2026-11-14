import datetime
import glob
import gzip
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import xml.sax.saxutils as xml_escape

BASE_URL = "https://kuroanime-flax.vercel.app"
TODAY = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_URLS_PER_SITEMAP = 9000

_CANONICAL_MULTI_GENRES = {
    "slice of life": "Slice of Life",
    "martial arts": "Martial Arts",
    "super power": "Super Power",
    "adult cast": "Adult Cast",
    "boys love": "Boys Love",
    "girls love": "Girls Love",
    "mahou shoujo": "Mahou Shoujo",
    "magical girl": "Magical Girl",
    "avant garde": "Avant Garde",
    "award winning": "Award Winning",
    "time travel": "Time Travel",
    "team sports": "Team Sports",
    "combat sports": "Combat Sports",
    "reverse harem": "Reverse Harem",
    "visual arts": "Visual Arts",
    "performing arts": "Performing Arts",
    "high stakes game": "High Stakes Game",
    "video game": "Video Game",
    "school life": "School Life",
    "sci-fi": "Sci-Fi",
    "post-apocalyptic": "Post-Apocalyptic",
}

_KNOWN_SINGLE_GENRES = {
    "action", "adventure", "comedy", "drama", "ecchi", "fantasy", "gore", "harem",
    "hentai", "historical", "horror", "isekai", "josei", "kids", "magic", "mecha",
    "military", "music", "mystery", "mythology", "parody", "police", "psychological",
    "romance", "samurai", "school", "seinen", "shoujo", "shounen", "space", "sports",
    "supernatural", "suspense", "thriller", "vampire", "anthropomorphic", "delinquents",
    "detective", "educational", "erotica", "gourmet", "idols", "medical", "pets",
    "racing", "reincarnation", "showbiz", "survival", "workplace",
}


def slugify_title(text: str) -> str:
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", norm.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def extract_clean_genres(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    seen = set()

    def _add(label: str):
        k = label.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(label.strip())

    for part in str(raw).split(","):
        tok = re.sub(r"\s+", " ", part).strip()
        if not tok or len(tok) > 60 or any(ch in tok for ch in (".", ";", ":", "!", "?")):
            continue
        low = tok.lower()
        if low in _CANONICAL_MULTI_GENRES:
            _add(_CANONICAL_MULTI_GENRES[low])
            continue
        if len(tok) <= 22 and tok.replace("-", "").isalpha() and " " not in tok:
            _add(tok)
            continue
        rem = low
        for m_low, m_canon in _CANONICAL_MULTI_GENRES.items():
            if m_low in rem:
                _add(m_canon)
                rem = rem.replace(m_low, " ")
        for w in rem.split():
            if w in _KNOWN_SINGLE_GENRES:
                _add(w.title())
    return out


def _open_db_with_gz_fallback(db_name: str, gz_candidates: list[str]):
    direct_candidates = [
        os.path.join(ROOT_DIR, db_name),
        os.path.join(ROOT_DIR, "api", db_name),
    ]
    for c in direct_candidates:
        if os.path.exists(c) and os.path.getsize(c) > 0:
            return c, False
    for gz in gz_candidates:
        if os.path.exists(gz) and os.path.getsize(gz) > 0:
            fd, tmp_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            with gzip.open(gz, "rb") as f_in, open(tmp_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            return tmp_path, True
    return None, False


def _load_catalog():
    db1_path, db1_tmp = _open_db_with_gz_fallback(
        "hianime.db",
        [os.path.join(ROOT_DIR, "api", "hianime.db.gz"), os.path.join(ROOT_DIR, "anime_app", "web", "api", "hianime.db.gz")],
    )
    db2_path, db2_tmp = _open_db_with_gz_fallback(
        "munib_server.db",
        [os.path.join(ROOT_DIR, "api", "munib_server.db.gz"), os.path.join(ROOT_DIR, "anime_app", "web", "api", "munib_server.db.gz")],
    )

    records = []
    try:
        if db1_path:
            print(f"[Sitemap] Loading Server 1 DB: {db1_path}")
            conn = sqlite3.connect(db1_path)
            ep_lastmod = {}
            try:
                for aid, ts in conn.execute("SELECT anime_id, MAX(scraped_at) FROM episodes WHERE scraped_at IS NOT NULL GROUP BY anime_id"):
                    if aid and ts:
                        ep_lastmod[int(aid)] = str(ts)[:10]
            except Exception:
                pass
            rows = conn.execute(
                """SELECT anime_id, title, title_english, genres, scraped_at, enriched_at, COALESCE(anilist_popularity, 0)
                   FROM anime
                   ORDER BY COALESCE(anilist_popularity, 0) DESC, anime_id ASC"""
            ).fetchall()
            for r in rows:
                aid = int(r[0])
                raw_ts = ep_lastmod.get(aid) or r[5] or r[4]
                lastmod = str(raw_ts)[:10] if raw_ts and len(str(raw_ts)) >= 10 else TODAY
                records.append({
                    "anime_id": aid,
                    "title": r[1] or "",
                    "title_english": r[2] or "",
                    "genres": r[3] or "",
                    "popularity": int(r[6] or 0),
                    "lastmod": lastmod,
                })
            conn.close()

        seen_ids = {rec["anime_id"] for rec in records}
        if db2_path:
            print(f"[Sitemap] Loading Server 2 DB: {db2_path}")
            conn2 = sqlite3.connect(db2_path)
            rows2 = conn2.execute(
                """SELECT anime_id, title, title_english, genres, scraped_at, COALESCE(anilist_popularity, 0)
                   FROM anime
                   ORDER BY COALESCE(anilist_popularity, 0) DESC, anime_id ASC"""
            ).fetchall()
            for r in rows2:
                aid = int(r[0])
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                raw_ts = r[4]
                lastmod = str(raw_ts)[:10] if raw_ts and len(str(raw_ts)) >= 10 else TODAY
                records.append({
                    "anime_id": aid,
                    "title": r[1] or "",
                    "title_english": r[2] or "",
                    "genres": r[3] or "",
                    "popularity": int(r[5] or 0),
                    "lastmod": lastmod,
                })
            conn2.close()
    finally:
        for p, is_tmp in ((db1_path, db1_tmp), (db2_path, db2_tmp)):
            if is_tmp and p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    records.sort(key=lambda x: (-x["popularity"], x["anime_id"]))
    return records


def _render_urlset(entries: list[tuple[str, str, str, str]], comment: str = "") -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    if comment:
        lines.append(f"  <!-- {comment} -->")
    for loc, lastmod, changefreq, priority in entries:
        lines.extend([
            "  <url>",
            f"    <loc>{xml_escape.escape(loc)}</loc>",
            f"    <lastmod>{xml_escape.escape(lastmod)}</lastmod>",
            f"    <changefreq>{changefreq}</changefreq>",
            f"    <priority>{priority}</priority>",
            "  </url>",
        ])
    lines.append("</urlset>\n")
    return "\n".join(lines)


def generate():
    records = _load_catalog()

    genre_counts: dict[str, int] = {}
    used_slugs: set[str] = set()
    anime_entries: list[tuple[str, str, str, str]] = []

    for rec in records:
        for g in extract_clean_genres(rec["genres"]):
            genre_counts[g] = genre_counts.get(g, 0) + 1

        aid = rec["anime_id"]
        base_title = rec["title"] or rec["title_english"] or f"anime-{aid}"
        base_slug = slugify_title(base_title) or f"anime-{aid}"
        canonical = base_slug if base_slug not in used_slugs else f"{base_slug}-{aid}"
        used_slugs.add(canonical)
        anime_entries.append((f"{BASE_URL}/anime/{canonical}", rec["lastmod"], "weekly", "0.7"))

    clean_genres = sorted((g for g, cnt in genre_counts.items() if cnt >= 2), key=lambda g: -genre_counts[g])

    hub_entries = [
        (f"{BASE_URL}/", TODAY, "daily", "1.0"),
        (f"{BASE_URL}/browse", TODAY, "daily", "0.9"),
        (f"{BASE_URL}/dubbed", TODAY, "daily", "0.9"),
        (f"{BASE_URL}/airing", TODAY, "daily", "0.9"),
        (f"{BASE_URL}/advisor", TODAY, "weekly", "0.85"),
        (f"{BASE_URL}/search", TODAY, "weekly", "0.7"),
    ]
    for genre in clean_genres:
        g_slug = slugify_title(genre)
        if g_slug:
            hub_entries.append((f"{BASE_URL}/genre/{g_slug}", TODAY, "weekly", "0.8"))

    print(f"[Sitemap] Prepared {len(anime_entries)} anime slug URLs & {len(hub_entries)} hub/genre URLs.")

    # Partition anime URLs into <= 9,000-URL files
    anime_chunks = [
        anime_entries[i:i + MAX_URLS_PER_SITEMAP]
        for i in range(0, max(1, len(anime_entries)), MAX_URLS_PER_SITEMAP)
    ] if anime_entries else []

    files_to_write: dict[str, str] = {
        "sitemap-hubs.xml": _render_urlset(hub_entries, "Core Pages, Hubs & Genre Guides"),
    }

    index_items = [("sitemap-hubs.xml", TODAY)]
    for idx, chunk in enumerate(anime_chunks, start=1):
        fname = f"sitemap-anime-{idx}.xml"
        files_to_write[fname] = _render_urlset(chunk, f"Anime Detail Pages Part {idx}")
        index_items.append((fname, TODAY))

    index_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for fname, lm in index_items:
        index_lines.extend([
            "  <sitemap>",
            f"    <loc>{BASE_URL}/{fname}</loc>",
            f"    <lastmod>{lm}</lastmod>",
            "  </sitemap>",
        ])
    index_lines.append("</sitemapindex>\n")
    files_to_write["sitemap.xml"] = "\n".join(index_lines)

    out_dirs = [
        os.path.join(ROOT_DIR, "anime_app", "web", "public"),
        os.path.join(ROOT_DIR, "anime_app", "web", "dist"),
        os.path.join(ROOT_DIR, "public"),
    ]
    os.makedirs(out_dirs[0], exist_ok=True)

    for d in out_dirs:
        if os.path.exists(d):
            for old_f in glob.glob(os.path.join(d, "sitemap-*.xml")):
                try:
                    os.remove(old_f)
                except OSError:
                    pass
            for fname, content in files_to_write.items():
                out_path = os.path.join(d, fname)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(content)
            print(f"[Sitemap] Wrote split sitemap index + {len(files_to_write)-1} child sitemaps to {d} ({len(hub_entries) + len(anime_entries)} total URLs)")


if __name__ == "__main__":
    generate()
