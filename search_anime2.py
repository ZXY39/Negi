import urllib.request
import ssl
import re
import sys
from urllib.parse import quote

# Fix stdout encoding for Windows console
sys.stdout.reconfigure(encoding='utf-8')

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# Use DuckDuckGo which is easier to parse
search_queries = [
    'AnimeJapan 2025 character ranking results',
    'NHK anime character ranking 2025',
    'AnimeJapan 2025 most popular character',
]

for q in search_queries:
    url = f'https://html.duckduckgo.com/html/?q={quote(q)}'
    print(f'=== Search: {q} ===')
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, context=ctx, timeout=15)
    html = resp.read().decode('utf-8', errors='replace')
    # DuckDuckGo stores result URLs in result__a data-url attribute or href
    links = re.findall(r'href="(https?://[^"]+)"', html)
    seen = set()
    for link in links:
        if any(domain in link for domain in ['duckduckgo', 'imprint', 'feedback']):
            continue
        decoded = urllib.parse.unquote(link)
        if decoded not in seen:
            seen.add(decoded)
            print(f'  {decoded}')
    if not seen:
        print('  No results found')
        # Dump a bit of html for debugging
        snippets = re.findall(r'<a[^>]+class="result[^"]*"[^>]*>(.*?)</a>', html)
        for s in snippets[:3]:
            text = re.sub(r'<[^>]+>', '', s)
            print(f'  Result snippet: {text.strip()[:200]}')
    print()
