import requests
import json
import sys

# Set stdout encoding to utf-8
sys.stdout.reconfigure(encoding='utf-8')

urls = [
    'https://www.animenewsnetwork.com/news',
    'https://myanimelist.net/news',
    'https://www.crunchyroll.com/news',
]

headers_list = [
    {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
    {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'},
    {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0'},
]

results = {}
for url, headers in zip(urls, headers_list):
    try:
        r = requests.get(url, headers=headers, timeout=15)
        # Look for anime character trends, popularity mentions in first 10000 chars
        text = r.text[:10000]
        results[url] = {
            'status': r.status_code,
            'length': len(r.text),
            'snippet': text[:1000]
        }
        print(f"[OK] {url} - Status: {r.status_code}, Length: {len(r.text)}")
    except Exception as e:
        results[url] = {'status': 'error', 'error': str(e)}
        print(f"[ERR] {url} - {e}")

# Write full results to file for inspection
with open('D:\\NewProjects\\G3KU\\anime_twitter_data.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print("\nResults written to anime_twitter_data.json")
