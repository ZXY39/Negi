import urllib.request
import re
import json
from html.parser import HTMLParser

class MyAnimeListParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.anime_list = []
        self.current = {}
        self.in_title = False
        self.in_score = False
        self.in_members = False
        self.in_image = False
        
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get('class', '')
        
        if 'link-title' in cls:
            self.in_title = True
            self.current = {}
        elif 'score-label' in cls or 'score' in cls:
            self.in_score = True
        elif 'members' in cls.lower():
            self.in_members = True
        if tag == 'img' and 'data-src' in attrs_dict:
            self.in_image = True
            
    def handle_data(self, data):
        data = data.strip()
        if not data:
            return
        if self.in_title:
            self.current['title'] = data
            self.in_title = False
        if self.in_score and data:
            try:
                self.current['score'] = float(data)
            except:
                pass
            self.in_score = False
        if self.in_members and data:
            self.current['members'] = data
            self.in_members = False
            
    def handle_endtag(self, tag):
        if self.current.get('title'):
            # When we find closing tags, save if we have enough data
            pass

def fetch_with_ua(url):
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"[FAIL] {url} - {e}")
        return None

# 1. Get MAL Spring 2025 - extract titles with scores using regex on raw HTML
print("=== MAL Spring 2025 Detailed ===")
html = fetch_with_ua('https://myanimelist.net/anime/season/2025/spring')
if html:
    # Save full HTML for analysis
    with open(r'D:\NewProjects\G3KU\mal_spring2025.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Saved MAL page: {len(html)} chars")
    
    # Try different patterns for extracting anime info
    # Pattern for anime items on season page
    items = re.findall(r'<div class="seasonal-anime.*?>(.*?)</div>\s*</div>', html, re.DOTALL)
    print(f"Found {len(items)} seasonal anime blocks")
    
    # Simple title extraction (already worked)
    title_re = re.compile(r'<a[^>]*class="link-title"[^>]*>([^<]+)</a>')
    titles = title_re.findall(html)
    
    # Score extraction - try various patterns
    score_patterns = [
        re.compile(r'<span[^>]*class="score-label[^"]*"[^>]*>(\d+\.\d+)'),
        re.compile(r'<span[^>]*class="[^"]*score[^"]*"[^>]*>(\d+\.\d+)'),
        re.compile(r'<span[^>]*>(\d+\.\d+)</span>.*?score'),
    ]
    
    for i, pat in enumerate(score_patterns):
        scores = pat.findall(html)
        print(f"Score pattern {i}: found {len(scores)} scores")
        if scores:
            print(f"  First 5: {scores[:5]}")
    
    # Print all titles
    print(f"\nAll {len(titles)} Spring 2025 anime titles:")
    for i, t in enumerate(titles):
        print(f"  {i+1}. {t}")

# 2. Try to get seasonal anime from Anilist API
print("\n=== Anilist API (GraphQL) ===")
try:
    query = """
    {
        Page(perPage: 50) {
            media(season: SPRING, seasonYear: 2025, type: ANIME, sort: POPULARITY_DESC) {
                id
                title {
                    romaji
                    english
                    native
                }
                averageScore
                meanScore
                favourites
                siteUrl
                genres
            }
        }
    }
    """
    req = urllib.request.Request(
        'https://graphql.anilist.co',
        data=json.dumps({'query': query}).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    
    if 'data' in data:
        anime_list = data['data']['Page']['media']
        print(f"Found {len(anime_list)} anime from Anilist (sorted by popularity)")
        for i, anime in enumerate(anime_list[:30]):
            title = anime['title']['english'] or anime['title']['romaji']
            avg = anime.get('averageScore', 'N/A')
            favs = anime.get('favourites', 0)
            genres = ', '.join(anime.get('genres', [])[:3])
            print(f"  {i+1}. {title} (Score: {avg}, Favs: {favs}, Genres: {genres})")
            print(f"      URL: {anime['siteUrl']}")
except Exception as e:
    print(f"Anilist API error: {e}")

# 3. Try Anime News Network for Spring 2025 overview
print("\n=== Anime News Network ===")
urls_ann = [
    'https://www.animenewsnetwork.com/encyclopedia/releases-index.php',
    'https://www.animenewsnetwork.com/news',
]
for url in urls_ann:
    html = fetch_with_ua(url)
    if html:
        print(f"[OK] {url} - {len(html)} chars")
        # Search for Spring 2025 references
        if 'Spring 2025' in html or 'spring 2025' in html:
            idx = html.lower().find('spring 2025')
            print(f"  Found Spring 2025 reference")

print("\nDone!")
