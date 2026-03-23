import requests
for url in ['https://wttr.in/Beijing?format=j1','https://wttr.in/Beijing?format=j2','https://wttr.in/Beijing?format=j1&num_of_days=7','https://wttr.in/Beijing?format=j1&days=7']:
    s=requests.Session(); s.trust_env=False; s.headers.update({'User-Agent':'Mozilla/5.0'})
    r=s.get(url, timeout=60)
    try:
        data=r.json()
        w=data.get('weather') or []
        print(url, r.status_code, len(w), [x.get('date') for x in w])
    except Exception as e:
        print(url, 'JSONERR', repr(e), r.text[:120])
