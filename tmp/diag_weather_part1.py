import requests, urllib.parse, json, time
cities=['Beijing','Tianjin','Shanghai','Chongqing','Shijiazhuang','Taiyuan','Shenyang','Changchun','Harbin','Nanjing','Hangzhou','Hefei','Fuzhou','Nanchang','Jinan','Zhengzhou','Wuhan']
for c in cities:
    ok=False
    last=''
    for i in range(3):
        try:
            s=requests.Session(); s.trust_env=False; s.headers.update({'User-Agent':'Mozilla/5.0'})
            r=s.get(f'https://wttr.in/{urllib.parse.quote(c)}?format=j1', timeout=60)
            w=(r.json().get('weather') or [])
            print(c, 'status', r.status_code, 'days', len(w), 'date0', w[0].get('date') if w else None)
            ok=True
            break
        except Exception as e:
            last=repr(e)
            time.sleep(1)
    if not ok:
        print(c, 'ERROR', last)
