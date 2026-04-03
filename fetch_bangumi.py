import requests
import json
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}

# Try Bangumi API endpoints
api_urls = [
    ('POST', 'https://api.bgm.tv/v0/search/subjects', {'keyword': '', 'sort': 'rank', 'filter': {'type': [2]}, 'limit': 20}),
    ('POST', 'https://next.bgm.tv/p1/v0/search/subjects', {'keyword': '', 'sort': 'rank', 'filter': {'type': [2]}, 'limit': 20}),
]

for method, url, data in api_urls:
    try:
        print('\n=== Testing %s %s ===' % (method, url))
        resp = requests.request(method, url, headers=headers, json=data, timeout=15, verify=False)
        print('Status: %s' % resp.status_code)
        ct = resp.headers.get('content-type', 'unknown')
        print('Content-Type: %s' % ct)
        if resp.status_code == 200:
            json_data = resp.json()
            if 'data' in json_data and len(json_data.get('data', [])) > 0:
                print('Got %d results' % len(json_data['data']))
                for i, item in enumerate(json_data['data'][:5]):
                    print('  [%d] %s (rank: %s, score: %s)' % (
                        i+1,
                        item.get('name', item.get('name_cn', '')),
                        item.get('rank', ''),
                        item.get('score', '')
                    ))
            else:
                print(json.dumps(json_data, ensure_ascii=False, indent=2)[:2000])
        else:
            print(resp.text[:500])
    except Exception as e:
        print('Error: %s' % e)
