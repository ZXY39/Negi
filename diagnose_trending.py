import requests
from bs4 import BeautifulSoup
import json

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
resp = requests.get('https://github.com/trending?since=weekly', headers=HEADERS, timeout=30)
soup = BeautifulSoup(resp.text, 'html.parser')
articles = soup.select('article.Box-row')[:3]

for i, art in enumerate(articles):
    print(f'=== Article {i} ===')
    # Find star-related text
    for elem in art.find_all(string=lambda text: text and 'star' in text.lower()):
        parent = elem.parent
        print(f'  STAR TEXT: [{elem.strip()}]')
        print(f'  Parent tag: {parent.name}, class: {parent.get("class")}')
        print(f'  Full parent text: [{parent.get_text(strip=True)}]')
    
    # Try to find the fork/star links area
    links = art.select('a.Link--muted.d-inline-block.mr-3, a.Link--muted.mr-3, .d-inline-block.mr-3, .Link--muted')
    for a in links:
        txt = a.get_text(strip=True)
        if 'star' in txt.lower():
            print(f'  LINK STAR: [{txt}] href={a.get("href")}')
    
    # Dump all Link--muted anchors
    for a in art.select('a.Link--muted'):
        txt = a.get_text(strip=True)
        if txt:
            print(f'  LINK: [{txt}] classes={a.get("class")}')
    
    # Also try d-flex flex-items-center
    flex = art.select_one('svg.octicon-star')
    if flex:
        p = flex.parent
        while p and p.name != 'article':
            print(f'  ANCESTOR: {p.name} class={p.get("class")} text=[{p.get_text(strip=True)[:200]}]')
            p = p.parent
    print()
