import sqlite3, requests, re, json
from bs4 import BeautifulSoup

DB_PATH = r"C:\Users\Munib Raza\OneDrive\Desktop\hianime_output\hianime.db"
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

try:
    c.execute("ALTER TABLE anime ADD COLUMN seasons_json TEXT")
except Exception:
    pass

c.execute("""
CREATE TABLE IF NOT EXISTS anime_seasons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_anime_id INTEGER,
    season_name     TEXT,
    season_anime_id INTEGER,
    season_url      TEXT,
    season_poster   TEXT,
    is_current      INTEGER DEFAULT 0,
    FOREIGN KEY(parent_anime_id) REFERENCES anime(anime_id)
);
""")
c.execute("CREATE INDEX IF NOT EXISTS idx_seasons_parent ON anime_seasons(parent_anime_id);")
conn.commit()

c.execute("SELECT anime_id, hianime_url FROM anime")
rows = c.fetchall()
print(f"Backfilling seasons for {len(rows)} anime...")
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

for aid, url in rows:
    if not url:
        continue
    try:
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "lxml")
        seasons = []
        block = soup.select_one(".block_area-seasons")
        if block:
            for item in block.select(".os-item"):
                s_title = item.select_one(".title")
                s_name = s_title.get_text(strip=True) if s_title else item.get_text(strip=True)
                s_href = item.get("href", "")
                s_id_m = re.search(r"-(\d+)$", s_href)
                s_id = int(s_id_m.group(1)) if s_id_m else None
                poster_m = re.search(r"url\((.*?)\)", str(item))
                s_poster = poster_m.group(1).strip("'\"") if poster_m else ""
                seasons.append({
                    "season_name": s_name,
                    "season_anime_id": s_id,
                    "season_url": s_href,
                    "season_poster": s_poster,
                    "is_current": 1 if "active" in item.get("class", []) else 0
                })
        c.execute("UPDATE anime SET seasons_json = ? WHERE anime_id = ?", (json.dumps(seasons), aid))
        for s in seasons:
            c.execute("""
            INSERT OR IGNORE INTO anime_seasons
                (parent_anime_id, season_name, season_anime_id, season_url, season_poster, is_current)
            VALUES (?,?,?,?,?,?)
            """, (aid, s["season_name"], s["season_anime_id"], s["season_url"], s["season_poster"], s["is_current"]))
        print(f"  [Anime {aid}] added {len(seasons)} seasons")
    except Exception as e:
        print(f"  [Anime {aid}] err: {e}")

conn.commit()
c.execute("SELECT COUNT(*) FROM anime_seasons")
print(f"Total seasons rows in DB now: {c.fetchone()[0]}")
conn.close()
