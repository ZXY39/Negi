import urllib.request
import sys

targets = [
    'https://www.reddit.com/r/AMV/search?q=most+viewed&sort=top&t=year',
    'https://myanimelist.net/topanime.php?type=bypopularity',
]

for url in targets:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode('utf-8', errors='replace')
        print(f'URL: {url}')
        print(f'Status: {resp.status}, Length: {len(data)}')
        print(data[:3000])
        print('---END---')
    except Exception as e:
        print(f'Error: {url}: {e}')
