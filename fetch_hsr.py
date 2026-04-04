import requests
import json

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

urls = [
    'https://www.reddit.com/r/HonkaiStarRail/search/?q=best+girl+2025&type=link',
    'https://wiki.hsr.moe/',
    'https://honkai-star-rail.fandom.com/wiki/Characters',
]

for url in urls:
    print(f'--- Trying: {url} ---')
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        print(f'Status: {r.status_code}')
        print(f'Final URL: {r.url}')
        print(r.text[:2000])
        print('---END---')
    except Exception as e:
        print(f'ERROR: {e}')
    print()
