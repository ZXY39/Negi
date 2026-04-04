import urllib.request
import re
import json

def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f'[FAIL] {url} - {e}')
        return None

# 1. MAL Spring 2025 page
print("=== Fetching MAL Spring 2025 ===")
html = fetch_url('https://myanimelist.net/anime/season/2025/spring')
if html:
    # Extract anime titles and scores
    title_re = re.compile(r'<a[^>]*class="link-title"[^>]*>([^<]+)')
    titles = title_re.findall(html)
    print(f"Found {len(titles)} titles")
    for i, t in enumerate(titles[:30]):
        print(f"  {i+1}. {t}")
    
    # Try to find score information
    score_re = re.compile(r'<span[^>]*class="score-label[^"]*"[^>]*>([^<]+)</span>')
    scores = score_re.findall(html)
    print(f"\nScores found: {len(scores)}")
    for s in scores[:20]:
        print(f"  {s}")
    
    # Try to find popularity/members
    member_re = re.compile(r'(\d[\d,]*)\s*members')
    members = member_re.findall(html)
    print(f"\nMembers info found: {len(members)}")
    for m in members[:10]:
        print(f"  {m}")

# 2. Try Anime Corner ranking pages
print("\n=== Fetching Anime Corner ===")
ac_urls = [
    'https://animecorner.me/anime-of-the-week/',
    'https://animecorner.me/best-anime-rankings/',
    'https://animecorner.me/currently-airing-anime-rankings/',
]
for url in ac_urls:
    html = fetch_url(url)
    if html:
        print(f"[OK] {url} - length: {len(html)}")
        # Look for 2025 or Spring references
        idx = html.lower().find('spring')
        if idx > 0:
            print(f"  Found 'spring' near: ...{html[max(0,idx-100):idx+100]}...")

# 3. Search for female character rankings
print("\n=== Character popularity searches ===")
char_urls = [
    'https://www.google.com/search?q=Spring+2025+anime+most+popular+female+characters',
    'https://www.google.com/search?q=2025%E6%98%A5%E3%82%A2%E3%83%8B%E3%83%A1+%E4%BA%BA%E6%B0%97%E3%82%AD%E3%83%A3%E3%83%A9+%E5%A5%B3%E6%80%A7',
    'https://www.google.com/search?q=reddit+best+anime+girl+spring+2025',
]
for url in char_urls:
    html = fetch_url(url)
    if html:
        print(f"[OK] {url} - length: {len(html)}")
        # Extract snippet text
        snippet_re = re.compile(r'<div[^>]*class="[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)
        if len(text) > 2000:
            print(f"  Preview: {text[:2000]}")
        else:
            print(f"  Preview: {text}")
