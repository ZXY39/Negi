# -*- coding: utf-8 -*-
import json, os

base = os.path.dirname(os.path.abspath(__file__))
out = []

# ---- weibo ----
p = os.path.join(base, 'weibo.json')
d = json.load(open(p, encoding='utf-8'))
data = d['data']
hs = data.get('hotgovs', []) + data.get('realtime', [])
out.append('===== WEIBO =====')
for item in hs:
    rank = item.get('rank', item.get('pos', '?'))
    w = (item.get('word') or item.get('name') or '').strip('#')
    out.append(str(rank) + '|' + w)

# ---- baidu ----
p = os.path.join(base, 'baidu.json')
d = json.load(open(p, encoding='utf-8'))
out.append('===== BAIDU =====')
for c in d['data']['cards']:
    for grp in c.get('content', []):
        for it in grp.get('content', []):
            tag = 'TOP' if it.get('isTop') else ''
            out.append(tag + '|' + it.get('word', ''))

# ---- douyin ----
p = os.path.join(base, 'douyin.json')
d = json.load(open(p, encoding='utf-8'))
out.append('===== DOUYIN =====')
cl = d.get('data', {}).get('word_list', []) or d.get('data', {}).get('data', {}).get('word_list', [])
for it in cl:
    out.append(str(it.get('rank', '?')) + '|' + it.get('word', ''))

# ---- bilibili ----
p = os.path.join(base, 'bili_rank.json')
d = json.load(open(p, encoding='utf-8'))
out.append('===== BILI code=' + str(d.get('code')) + ' =====')
for i, it in enumerate(d.get('data', {}).get('list', [])[:40], 1):
    out.append(str(i) + '|' + it.get('title', '') + '|UP:' + it.get('owner', {}).get('name', '') + '|view:' + str(it.get('stat', {}).get('view', '')))

txt = '\n'.join(out)
open(os.path.join(base, 'all_parsed.txt'), 'w', encoding='utf-8').write(txt)
print('WROTE', len(txt))