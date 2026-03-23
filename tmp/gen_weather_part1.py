import json, os, requests, urllib.parse, time
cities = [
    ("北京市", "Beijing", "Beijing"),
    ("天津市", "Tianjin", "Tianjin"),
    ("上海市", "Shanghai", "Shanghai"),
    ("重庆市", "Chongqing", "Chongqing"),
    ("河北省", "石家庄", "Shijiazhuang"),
    ("山西省", "太原", "Taiyuan"),
    ("辽宁省", "沈阳", "Shenyang"),
    ("吉林省", "长春", "Changchun"),
    ("黑龙江省", "哈尔滨", "Harbin"),
    ("江苏省", "南京", "Nanjing"),
    ("浙江省", "杭州", "Hangzhou"),
    ("安徽省", "合肥", "Hefei"),
    ("福建省", "福州", "Fuzhou"),
    ("江西省", "南昌", "Nanchang"),
    ("山东省", "济南", "Jinan"),
    ("河南省", "郑州", "Zhengzhou"),
    ("湖北省", "武汉", "Wuhan"),
]
out = []
for region, city_cn, query in cities:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({'User-Agent': 'Mozilla/5.0'})
    url = f"https://wttr.in/{urllib.parse.quote(query)}?format=j1"
    last_err = None
    for attempt in range(3):
        try:
            r = s.get(url, timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            last_err = e
            time.sleep(1)
            s.close()
            s = requests.Session()
            s.trust_env = False
            s.headers.update({'User-Agent': 'Mozilla/5.0'})
    else:
        raise last_err
    weather = (data.get('weather') or [])[:7]
    if len(weather) < 7:
        raise RuntimeError(f"insufficient weather days for {query}: {len(weather)}")
    days = []
    for day in weather:
        astronomy = (day.get('astronomy') or [{}])[0]
        hourly = day.get('hourly') or [{}]
        desc = ''
        for h in hourly:
            arr = h.get('weatherDesc') or []
            if arr and arr[0].get('value'):
                desc = arr[0]['value']
                break
        rain_vals = []
        for h in hourly:
            v = h.get('chanceofrain')
            if v not in (None, ''):
                try:
                    rain_vals.append(int(v))
                except Exception:
                    pass
        days.append({
            'date': day.get('date'),
            'weather_desc': desc,
            'min_temp_c': day.get('mintempC'),
            'max_temp_c': day.get('maxtempC'),
            'chance_of_rain': max(rain_vals) if rain_vals else None,
            'sunrise': astronomy.get('sunrise'),
            'sunset': astronomy.get('sunset'),
        })
    out.append({'region': region, 'representative_city_cn': city_cn, 'city_query': query, 'days': days})
    print(f'fetched {query}')
os.makedirs(r"E:\Program\G3KU\tmp", exist_ok=True)
with open(r"E:\Program\G3KU\tmp\weather_part1.json", 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"wrote {len(out)} records")
