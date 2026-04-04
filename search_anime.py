import urllib.request
import ssl
import re
from urllib.parse import quote

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
}

search_queries = [
    'AnimeJapan 2025 キャラクター 投票 結果',
    'NHK アニメ 投票 2025 2026',
    'AnimeJapan 2025 女性キャラクター ランキング',
]

for q in search_queries:
    url = f'https://www.google.com/search?q={quote(q)}&num=15&hl=ja'
    print(f'=== Search: {q} ===')
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, context=ctx, timeout=15)
    html = resp.read().decode('utf-8', errors='replace')
    links = re.findall(r'/url\?q=(https?://[^"&]+)', html)
    seen = set()
    for link in links[:15]:
        if 'google' not in link and link not in seen:
            seen.add(link)
            print(f'  {link}')
    print()
