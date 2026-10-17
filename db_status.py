import sqlite3, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB_PATH = r"C:\Users\Munib Raza\OneDrive\Desktop\hianime_output\hianime.db"

def inspect_db():
    if not os.path.exists(DB_PATH):
        print(f"Database file not created yet: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    try:
        c.execute("SELECT COUNT(*) FROM anime")
        n_anime = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM episodes")
        n_episodes = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM streams")
        n_streams = c.fetchone()[0]

        try:
            c.execute("SELECT COUNT(*) FROM anime_seasons")
            n_seasons = c.fetchone()[0]
        except Exception:
            n_seasons = 0

        c.execute("SELECT COUNT(*) FROM anime WHERE anilist_id IS NOT NULL")
        n_enriched = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM anime WHERE poster_url IS NOT NULL AND poster_url != ''")
        n_posters = c.fetchone()[0]

        print("=" * 65)
        print("DATABASE STATUS SUMMARY (hianime.db)")
        print("=" * 65)
        print(f"Total Anime Scraped:    {n_anime:,}")
        print(f"Total Seasons Linked:   {n_seasons:,}")
        print(f"Total Episodes Scraped: {n_episodes:,}")
        print(f"Total Stream Links:     {n_streams:,}")
        print(f"AniList Enriched:       {n_enriched:,} / {n_anime:,}")
        print(f"Poster Images Available:{n_posters:,} / {n_anime:,}")

        # Stream breakdowns (Sub vs Dub)
        print("\n--- Streams by Server & Audio Type (Sub vs Dub) ---")
        c.execute("""
            SELECT server_name, type, COUNT(*) 
            FROM streams 
            GROUP BY server_name, type 
            ORDER BY COUNT(*) DESC
        """)
        for sname, stype, cnt in c.fetchall():
            print(f"  • {sname or 'Unknown'} [{stype.upper() if stype else 'SUB'}]: {cnt:,} stream links")

        # Anime Seasons Sample
        print("\n--- Anime Seasons Relations Sample ---")
        c.execute("""
            SELECT a.title, GROUP_CONCAT(s.season_name, ' | ')
            FROM anime a
            JOIN anime_seasons s ON a.anime_id = s.parent_anime_id
            GROUP BY a.anime_id
            LIMIT 4
        """)
        for title, s_names in c.fetchall():
            print(f"  🎬 {title}:")
            print(f"     Seasons: {s_names}")

        # Sample Stream Links
        print("\n--- Sample Stream Links (Sub & Dub) ---")
        c.execute("""
            SELECT anime_title, episode_number, server_name, type, embed_url
            FROM streams
            LIMIT 6
        """)
        for atitle, ep_no, sname, stype, url in c.fetchall():
            print(f"  • {atitle} Ep {ep_no} [{sname} - {stype.upper()}]: {url}")

        print("=" * 65)
    except Exception as e:
        print(f"Error querying DB: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    inspect_db()
