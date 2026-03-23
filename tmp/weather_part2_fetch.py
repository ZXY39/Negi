import json, urllib.parse, urllib.request, ssl
from pathlib import Path

cities = [
    {"region":"湖南省","representative_city_cn":"长沙","city_query":"Changsha"},
    {"region":"广东省","representative_city_cn":"广州","city_query":"Guangzhou"},
    {"region":"海南省","representative_city_cn":"海口","city_query":"Haikou"},
    {"region":"四川省","representative_city_cn":"成都","city_query":"Chengdu"},
    {"region":"贵州省","representative_city_cn":"贵阳","city_query":"Guiyang"},
    {"region":"云南省","representative_city_cn":"昆明","city_query":"Kunming"},
    {"region":"陕西省","representative_city_cn":"西安","city_query":"Xi-an"},
    {"region":"甘肃省","representative_city_cn":"兰州","city_query":"Lanzhou"},
    {"region":"青海省","representative_city_cn":"西宁","city_query":"Xining"},
    {"region":"台湾省","representative_city_cn":"台北","city_query":"Taipei"},
    {"region":"内蒙古自治区","representative_city_cn":"呼和浩特","city_query":"Hohhot"},
    {"region":"广西壮族自治区","representative_city_cn":"南宁","city_query":"Nanning"},
    {"region":"西藏自治区","representative_city_cn":"拉萨","city_query":"Lhasa"},
    {"region":"宁夏回族自治区","representative_city_cn":"银川","city_query":"Yinchuan"},
    {"region":"新疆维吾尔自治区","representative_city_cn":"乌鲁木齐","city_query":"Urumqi"},
    {"region":"香港特别行政区","representative_city_cn":"香港","city_query":"Hong Kong"},
    {"region":"澳门特别行政区","representative_city_cn":"澳门","city_query":"Macau"},
]

ctx = ssl.create_default_context()
out = []
for item in cities:
    q = urllib.parse.quote(item['city_query'])
    url = f"https://wttr.in/{q}?format=j1"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    weather = data.get('weather', [])[:7]
    if len(weather) < 7:
        raise RuntimeError(f"insufficient weather days for {item['city_query']}: {len(weather)}")
    days = []
    for d in weather:
        astronomy = (d.get('astronomy') or [{}])[0]
        hourly = d.get('hourly') or [{}]
        desc = ''
        for h in hourly:
            arr = h.get('weatherDesc') or []
            if arr and arr[0].get('value'):
                desc = arr[0]['value']
                break
        chance_of_rain = None
        for h in hourly:
            cor = h.get('chanceofrain')
            if cor not in (None, ''):
                try:
                    corv = int(cor)
                except Exception:
                    continue
                chance_of_rain = corv if chance_of_rain is None else max(chance_of_rain, corv)
        days.append({
            'date': d.get('date'),
            'weather_desc': desc,
            'min_temp_c': d.get('mintempC'),
            'max_temp_c': d.get('maxtempC'),
            'chance_of_rain': chance_of_rain,
            'sunrise': astronomy.get('sunrise'),
            'sunset': astronomy.get('sunset'),
        })
    out.append({
        'region': item['region'],
        'representative_city_cn': item['representative_city_cn'],
        'city_query': item['city_query'],
        'days': days,
    })

path = Path(r'E:\Program\G3KU\tmp\weather_part2.json')
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'WROTE {path} {len(out)} records')
