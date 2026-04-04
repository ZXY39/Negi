import urllib.request
import ssl
import re
import sys
from urllib.parse import quote

sys.stdout.reconfigure(encoding='utf-8')

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
}

# Direct URLs to try
urls = [
    'https://www.anime-japan.jp/2025/',
    'https://www.anime-japan.jp/2025/news/',
    'https://www.anime-japan.jp/2025/event/',
    'https://www.nhk.or.jp/anime/',
    'https://www.anime-japan.jp/2025/stage/',
]

for url in urls:
    print(f'=== Fetching: {url} ===')
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        html = resp.read(50000).decode('utf-8', errors='replace')
        # Check size
        print(f'  Size: {len(html)} bytes')
        # Look for title
        title_match = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
        if title_match:
            print(f'  Title: {title_match.group(1).strip()[:200]}')
        # Look for voting/character related keywords
        keywords = ['投票', 'voting', 'キャラクター', 'character', '人気', 'popular', 'ranking', 'ランキング', '結果', 'result']
        for kw in keywords:
            if kw in html:
                print(f'  Found keyword: {kw}')
                # Find context
                idx = html.find(kw)
                context = html[max(0,idx-50):idx+100]
                context = re.sub(r'<[^>]+>', ' ', context)
                context = re.sub(r'\s+', ' ', context)
                print(f'    Context: ...{context.strip()}...')
    except Exception as e:
        print(f'  Error: {e}')
    print()
