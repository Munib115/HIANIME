import requests, re, sqlite3
from bs4 import BeautifulSoup

BASE_URL = "https://hianime.at"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

conn = sqlite3.connect("hianime.db")
c = conn.cursor()
c.execute("SELECT anime_id FROM anime")
existing_ids = set(row[0] for row in c.fetchall())

print(f"Total existing anime in DB: {len(existing_ids)}")

def scan_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.text, "html.parser")
        items = []
        for item in soup.select(".film_list-wrap .flw-item"):
            a = item.select_one(".film-name a")
            if a and a.get("href"):
                href = a.get("href")
                m = re.search(r"-(\d+)$", href)
                if m:
                    items.append((int(m.group(1)), a.text.strip(), href if href.startswith("http") else BASE_URL + href))
        return items
    except Exception as e:
        print(f"Error {url}: {e}")
        return []

# Scan /recently-updated pages 1-10
recent_items = []
for p in range(1, 11):
    recent_items.extend(scan_page(f"{BASE_URL}/recently-updated?page={p}"))

# Scan /new-anime pages 1-10
new_items = []
for p in range(1, 11):
    new_items.extend(scan_page(f"{BASE_URL}/new-anime?page={p}"))

all_scanned = {}
for aid, title, url in recent_items + new_items:
    all_scanned[aid] = (title, url)

missing = {aid: info for aid, info in all_scanned.items() if aid not in existing_ids}
print(f"Scanned {len(all_scanned)} unique titles from /recently-updated and /new-anime (pages 1-10 each).")
print(f"Already in DB: {len(all_scanned) - len(missing)}")
print(f"Missing from DB: {len(missing)}")

if missing:
    print("\nSample missing titles:")
    for aid, (title, url) in list(missing.items())[:15]:
        print(f"  [{aid}] {title} -> {url}")
