# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
url = 'https://github.com/luongnv89/claude-howto'
resp = requests.get(url, headers=headers, timeout=30)
resp.encoding = 'utf-8'
soup = BeautifulSoup(resp.text, 'html.parser')

print("=== Star Links ===")
for sl in soup.find_all('a', href=lambda h: h and '/stargazers' in h):
    txt = sl.get_text(strip=True)
    cls = sl.get('class', [])
    print(f"  href={sl.get('href')} | class={cls} | text='{txt}'")
    for child in sl.children:
        if hasattr(child, 'name'):
            print(f"    child <{child.name} class={child.get('class',[])}> '{child.get_text(strip=True)}'")

print("\n=== Language ===")
lang = soup.find('span', itemprop='programmingLanguage')
print(f"  By itemprop: {lang}")

print("\n=== All items with class 'js-repo' or 'octicon-star' ===")
for el in soup.find_all(class_=lambda c: c and ('octicon-star' in c or 'js-social' in c or 'stargazers' in c)):
    parent_text = el.parent.get_text(strip=True) if el.parent else ''
    print(f"  <{el.name} class={el.get('class',[])}> text='{el.get_text(strip=True)}'")
    print(f"    parent text: '{parent_text}'")

print("\n=== Sidebar repo-meta-items ===")
for el in soup.find_all('ul', class_='pagehead-actions'):
    print(f"  Found ul.pagehead-actions")
    for li in el.find_all('li'):
        print(f"  li: '{li.get_text(strip=True)[:100]}'")
