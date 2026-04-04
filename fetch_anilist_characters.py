"""
Query AniList GraphQL API for top female characters by favourites count.
Uses browser-like headers to avoid Cloudflare blocking.
"""
import json
import urllib.request
import urllib.error
import sys
import io

# Set stdout to UTF-8 to avoid Windows GBK encoding issues
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API_URL = "https://graphql.anilist.co"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Origin": "https://anilist.co",
    "Referer": "https://anilist.co/",
}

def query_anilist(query, variables):
    """Execute a GraphQL query against the AniList API."""
    data = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers=HEADERS,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP Error {e.code}: {body[:300]}")
        return None
    except Exception as e:
        print(f"Request error: {e}")
        return None

QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo {
      total
      currentPage
      hasNextPage
    }
    characters(sort: FAVOURITES_DESC) {
      id
      name {
        full
        native
        alternative
        alternativeSpoiler
      }
      gender
      favourites
      media(page: 1, perPage: 3, sort: [POPULARITY_DESC]) {
        nodes {
          title {
            romaji
            english
            native
          }
          type
          format
          startDate {
            year
          }
        }
      }
    }
  }
}
"""

def main():
    all_characters = []
    page = 1
    per_page = 50
    
    while True:
        print(f"Fetching page {page}...")
        result = query_anilist(QUERY, {"page": page, "perPage": per_page})
        if not result or "data" not in result:
            print("Failed to fetch page", page)
            break
        
        page_data = result["data"]["Page"]
        chars = page_data["characters"]
        if not chars:
            break
        all_characters.extend(chars)
        
        print(f"  Got {len(chars)} characters. Total: {len(all_characters)}")
        
        if not page_data["pageInfo"]["hasNextPage"] or len(all_characters) >= 150:
            break
        page += 1
    
    # Filter female characters
    female_chars = []
    for c in all_characters:
        gender = (c.get("gender") or "").strip().lower()
        if gender in ("female", "f"):
            female_chars.append(c)
    
    print(f"\nTotal fetched: {len(all_characters)}")
    print(f"Female characters: {len(female_chars)}")
    
    output = []
    for rank, c in enumerate(female_chars[:60], 1):
        name = c.get("name", {})
        media_nodes = c.get("media", {}).get("nodes", [])
        media_info = []
        for m in media_nodes:
            t = m.get("title", {})
            media_info.append({
                "romaji": t.get("romaji", ""),
                "english": t.get("english", ""),
                "type": m.get("type", ""),
                "format": m.get("format", ""),
                "year": m.get("startDate", {}).get("year") if m.get("startDate") else None
            })
        
        output.append({
            "rank": rank,
            "name_en": name.get("full", ""),
            "name_native": name.get("native", ""),
            "alternatives": name.get("alternative", []) + name.get("alternativeSpoiler", []),
            "gender": c.get("gender"),
            "favourites": c.get("favourites", 0),
            "media": media_info,
            "id": c.get("id")
        })
    
    with open("D:\\NewProjects\\G3KU\\anilist_female_characters.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\nTop {len(output)} female characters saved to anilist_female_characters.json")
    # Print top 20 for quick view
    for c in output[:20]:
        media_name = ""
        if c['media']:
            m = c['media'][0]
            media_name = m.get('english') or m.get('romaji') or "?"
        print(f"{c['rank']}. {c['name_en']} - {c['favourites']} favourites - {media_name}")

if __name__ == "__main__":
    main()
