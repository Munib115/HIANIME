import sqlite3
import urllib.request
import urllib.parse
import json
import time
import shutil
import sys

# Ensure UTF-8 output on Windows
sys.stdout.reconfigure(encoding='utf-8')

API_KEY = "15d2ea6d0dc1d476efbca3eba2b9bbfb"
DB_PATH = "hianime.db"

def search_tmdb_backdrop(title, title_eng, atype):
    query = title_eng or title
    # Clean up title for search
    q = query.split('(')[0].split('Season')[0].split(':')[0].strip()
    is_movie = (atype == 'Movie')
    search_type = 'movie' if is_movie else 'tv'
    
    url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={API_KEY}&query={urllib.parse.quote(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    try:
        res = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        results = res.get("results", [])
        if results and results[0].get("backdrop_path"):
            return results[0]["backdrop_path"]
        
        # If TV search yielded no backdrop, fallback to movie search
        if not is_movie:
            m_url = f"https://api.themoviedb.org/3/search/movie?api_key={API_KEY}&query={urllib.parse.quote(q)}"
            m_res = json.loads(urllib.request.urlopen(urllib.request.Request(m_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=5).read().decode())
            m_results = m_res.get("results", [])
            if m_results and m_results[0].get("backdrop_path"):
                return m_results[0]["backdrop_path"]
    except Exception as e:
        pass
    return None

def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    
    rows = cur.execute("""
        SELECT anime_id, title, title_english, type, banner_image
        FROM anime
        WHERE anilist_popularity IS NOT NULL
        ORDER BY anilist_popularity DESC
        LIMIT 150
    """).fetchall()
    
    print(f"Enriching top {len(rows)} anime with high-resolution 1080p/4K TMDB backdrops...")
    
    updated = 0
    for aid, title, title_eng, atype, current_banner in rows:
        name = title_eng or title
        # Check if already using TMDB
        if current_banner and "image.tmdb.org" in current_banner:
            updated += 1
            continue
            
        backdrop_path = search_tmdb_backdrop(title, title_eng, atype)
        if backdrop_path:
            high_res_url = f"https://image.tmdb.org/t/p/w1920_and_h1080_multi_faces{backdrop_path}"
            cur.execute("UPDATE anime SET banner_image = ? WHERE anime_id = ?", (high_res_url, aid))
            con.commit()
            updated += 1
            print(f"[{updated:3}] OK: {name[:32]:32} -> {backdrop_path}")
        else:
            print(f"[ - ] NO: {name[:32]:32} (kept current)")
        time.sleep(0.05)
    
    con.commit()
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")
    con.close()
    
    # Sync to deployment databases
    shutil.copy2(DB_PATH, "anime_app/web/api/hianime.db")
    shutil.copy2(DB_PATH, "api/hianime.db")
    print(f"\nDone! Enriched {updated} anime with pristine 1080p/4K backdrops and synced all databases.")

if __name__ == "__main__":
    main()
