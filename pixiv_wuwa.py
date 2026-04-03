import urllib.request
import json
import urllib.parse
import time
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://www.pixiv.net/'
}

tag_queries = [
    ('Yinlin', '\u541f\u9716_(\u9cf4\u6f6e)'),
    ('Jinhsi', '\u4eca\u6c50_(\u9cf4\u6f6e)'),
    ('Changli', '\u9577\u96e2_(\u9cf4\u6f6e)'),
    ('Zhezhi', '\u6298\u679d_(\u9cf4\u6f6e)'),
    ('Shorekeeper', '\u5b88\u5cb8\u4eba_(\u9cf4\u6f6e)'),
    ('Camellya', '\u693f_(\u9cf4\u6f6e)'),
    ('Youhu', '\u91c9\u745a_(\u9cf4\u6f6e)'),
    ('Roccia', '\u6d1b\u53ef\u53ef_(\u9cf4\u6f6e)'),
    ('Verina', '\u30f4\u30a7\u30ea\u30fc\u30ca_(\u9cf4\u6f6e)'),
    ('Carlotta', '\u30ab\u30eb\u30ed\u30c3\u30bf_(\u9cf4\u6f6e)'),
    ('Cantarella', '\u30ad\u30e3\u30f3\u30c6\u30ec\u30e9_(\u9cf4\u6f6e)'),
    ('Florezzo', '\u30d5\u30ed\u30ec\u30c3\u30c4\u30a9_(\u9cf4\u6f6e)'),
    ('Lupa', '\u30eb\u30d1_(\u9cf4\u6f6e)'),
    ('Zani', '\u30b6\u30cb_(\u9cf4\u6f6e)'),
    ('Encore', '\u30a2\u30f3\u30b3_(\u9cf4\u6f6e)'),
    ('Danjin', '\u4e39\u747e_(\u9cf4\u6f6e)'),
    ('Taoqi', '\u6843\u7948_(\u9cf4\u6f6e)'),
    ('Yangyang', '\u967d\u967d_(\u9cf4\u6f6e)'),
    ('Lingyang', '\u51cc\u967d_(\u9cf4\u6f6e)'),
    ('Xiangliyao', '\u76f8\u91cc\u8981_(\u9cf4\u6f6e)'),
    ('Jiyan', '\u5fcc\u708e_(\u9cf4\u6f6e)'),
    ('Calcharo', '\u30ab\u30ab\u30ed_(\u9cf4\u6f6e)'),
    ('Rover_M', '\u6f02\u6cca\u8005_(\u9cf4\u6f6e)'),
    ('Jianxin', '\u5805\u5ca9_(\u9cf4\u6f6e)'),
    ('Chixia', '\u79cb\u6c34_(\u9cf4\u6f6e)'),
    ('WutheringWaves', 'WutheringWaves'),
    ('Mingchao_JP', '\u9cf4\u6f6e'),
    ('Mingchao_CN', '\u9e23\u6f6e'),
]

results = {}

def query_tag(tag):
    encoded = urllib.parse.quote(tag)
    url = f'https://www.pixiv.net/ajax/search/artworks/{encoded}?word={encoded}&order=date_d&mode=all&p=1&s_mode=s_tag_full&type=all&lang=zh'
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as f:
            data = json.loads(f.read().decode('utf-8'))
            count = int(data.get('body', {}).get('illustManga', {}).get('total', 0))
            return count
    except Exception as e:
        return None, str(e)[:80]

print('=== Wuthering Waves Character Pixiv Counts ===\n')

for display, tag in tag_queries:
    r = query_tag(tag)
    if r[0] is None and not isinstance(r, int):
        cnt, err = r
        print(f'FAIL: {display:25s} tag={tag:45s} err={err}')
    else:
        if isinstance(r, int):
            cnt = r
        else:
            cnt, err = r
        if display not in results or results[display] < cnt:
            results[display] = cnt
        print(f'OK:   {display:25s} tag={tag:45s} count={cnt}')
    time.sleep(0.3)

print('\n=== Character Ranking ===')
sorted_r = sorted(results.items(), key=lambda x: x[1], reverse=True)
for i, (name, cnt) in enumerate(sorted_r, 1):
    print(f'{i:3d}. {name:25s} = {cnt:,}')
