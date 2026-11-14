import datetime
import gzip
import os
import shutil
import sqlite3
import tempfile
import xml.sax.saxutils as xml_escape

BASE_URL = "https://kuroanime-flax.vercel.app"
TODAY = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_db():
    candidates = [
        os.path.join(ROOT_DIR, "hianime.db"),
        os.path.join(ROOT_DIR, "api", "hianime.db"),
        os.path.join(ROOT_DIR, "munib_server.db"),
        os.path.join(ROOT_DIR, "api", "munib_server.db"),
    ]
    for c in candidates:
        if os.path.exists(c) and os.path.getsize(c) > 0:
            return c, False

    gz_candidates = [
        os.path.join(ROOT_DIR, "api", "hianime.db.gz"),
        os.path.join(ROOT_DIR, "anime_app", "web", "api", "hianime.db.gz"),
    ]
    for gz in gz_candidates:
        if os.path.exists(gz) and os.path.getsize(gz) > 0:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
            os.close(tmp_fd)
            with gzip.open(gz, "rb") as f_in, open(tmp_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            return tmp_path, True

    return None, False


def generate():
    db_path, is_temp = find_db()
    if not db_path:
        print("[Sitemap] Warning: No database found, generating minimal static sitemap.")
        anime_rows = []
        genre_set = set()
    else:
        print(f"[Sitemap] Connecting to {db_path}...")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        try:
            cur.execute("SELECT anime_id, title, genres, scraped_at, enriched_at FROM anime ORDER BY anime_id ASC")
            anime_rows = cur.fetchall()
        except Exception as e:
            print(f"[Sitemap] Error querying anime table: {e}")
            anime_rows = []

        genre_set = set()
        for row in anime_rows:
            raw_genres = row[2] or ""
            tokens = [t.strip() for t in raw_genres.replace(",", " ").split() if t.strip()]
            for t in tokens:
                if 2 < len(t) <= 24 and t.isalpha():
                    genre_set.add(t)
        conn.close()
        if is_temp and os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass

    print(f"[Sitemap] Extracted {len(anime_rows)} anime titles and {len(genre_set)} genres.")

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        "  <!-- Core Site Routes -->",
        "  <url>",
        f"    <loc>{BASE_URL}/</loc>",
        f"    <lastmod>{TODAY}</lastmod>",
        "    <changefreq>daily</changefreq>",
        "    <priority>1.0</priority>",
        "  </url>",
        "  <url>",
        f"    <loc>{BASE_URL}/browse</loc>",
        f"    <lastmod>{TODAY}</lastmod>",
        "    <changefreq>daily</changefreq>",
        "    <priority>0.8</priority>",
        "  </url>",
        "  <url>",
        f"    <loc>{BASE_URL}/search</loc>",
        f"    <lastmod>{TODAY}</lastmod>",
        "    <changefreq>daily</changefreq>",
        "    <priority>0.8</priority>",
        "  </url>",
        "  <!-- Genre Catalog Views -->",
    ]

    for genre in sorted(genre_set):
        encoded_genre = xml_escape.escape(genre)
        xml_lines.extend([
            "  <url>",
            f"    <loc>{BASE_URL}/browse?genre={encoded_genre}</loc>",
            f"    <lastmod>{TODAY}</lastmod>",
            "    <changefreq>weekly</changefreq>",
            "    <priority>0.7</priority>",
            "  </url>",
        ])

    xml_lines.append("  <!-- Anime Detail Pages -->")
    for r in anime_rows:
        anime_id = r[0]
        lastmod = TODAY
        raw_ts = r[4] or r[3]
        if raw_ts and isinstance(raw_ts, str) and len(raw_ts) >= 10:
            lastmod = raw_ts[:10]

        xml_lines.extend([
            "  <url>",
            f"    <loc>{BASE_URL}/anime/{anime_id}</loc>",
            f"    <lastmod>{lastmod}</lastmod>",
            "    <changefreq>weekly</changefreq>",
            "    <priority>0.7</priority>",
            "  </url>",
        ])

    xml_lines.append("</urlset>\n")
    sitemap_content = "\n".join(xml_lines)

    out_dirs = [
        os.path.join(ROOT_DIR, "anime_app", "web", "public"),
        os.path.join(ROOT_DIR, "anime_app", "web", "dist"),
        os.path.join(ROOT_DIR, "public"),
    ]
    for d in out_dirs:
        if os.path.exists(d):
            out_file = os.path.join(d, "sitemap.xml")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(sitemap_content)
            print(f"[Sitemap] Successfully wrote {out_file} ({len(anime_rows) + len(genre_set) + 3} URLs)")

    target_primary = os.path.join(ROOT_DIR, "anime_app", "web", "public", "sitemap.xml")
    if not os.path.exists(target_primary):
        os.makedirs(os.path.dirname(target_primary), exist_ok=True)
        with open(target_primary, "w", encoding="utf-8") as f:
            f.write(sitemap_content)
        print(f"[Sitemap] Wrote primary target {target_primary}")


if __name__ == "__main__":
    generate()
