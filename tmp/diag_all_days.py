import requests, urllib.parse
cities=['Beijing','Tianjin','Shanghai','Chongqing','Shijiazhuang','Taiyuan','Shenyang','Changchun','Harbin','Nanjing','Hangzhou','Hefei','Fuzhou','Nanchang','Jinan','Zhengzhou','Wuhan']
for c in cities:
    s=requests.Session(); s.trust_env=False; s.headers.update({'User-Agent':'Mozilla/5.0'})
    r=s.get(f'https://wttr.in/{urllib.parse.quote(c)}?format=j1', timeout=60)
    w=(r.json().get('weather') or [])
    print(c, len(w), [x.get('date') for x in w])
