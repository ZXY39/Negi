import requests, urllib.parse, json
c='Beijing'
s=requests.Session(); s.trust_env=False; s.headers.update({'User-Agent':'Mozilla/5.0'})
r=s.get(f'https://wttr.in/{urllib.parse.quote(c)}?format=j1', timeout=60)
print('status', r.status_code)
data=r.json()
print('keys', sorted(data.keys()))
print('weather_len', len(data.get('weather') or []))
print('dates', [x.get('date') for x in (data.get('weather') or [])])
