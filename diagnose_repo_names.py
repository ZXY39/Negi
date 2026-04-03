#!/usr/bin/env python3
"""Diagnose trending page structure for repo name extraction."""
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
r = requests.get("https://github.com/trending?since=weekly", headers=HEADERS, timeout=20)
r.raise_for_status()
soup = BeautifulSoup(r.text, "html.parser")
articles = soup.select("article.Box-row") or soup.select("article.border")

for i, art in enumerate(articles[:3]):
    print(f"\n=== Article {i+1} ===")
    # Find ALL links
    for idx, a in enumerate(art.find_all("a", href=True)):
        href = a.get("href", "")
        txt = a.get_text(strip=True)[:80]
        parent_tag = a.parent.name if a.parent else "none"
        parent_classes = a.parent.get("class", []) if a.parent else []
        classes = a.get("class", [])
        print(f"  Link {idx}: href={href} | text='{txt}' | tag={a.name} | classes={classes} | parent={parent_tag}.{parent_classes}")
    # Find h2 elements
    h2s = art.find_all("h2")
    for h in h2s:
        print(f"  H2: classes={h.get('class', [])} | text='{h.get_text(strip=True)[:100]}' | attrs={h.attrs}")
    # Find repo link specifically
    repo_link = art.select_one("h2 a") or art.select_one("h2.link-h2 a") or art.select_one('a[href^="/"]')
    for selector in ["h2 a", "h2.link-h2 a", 'a[href^="/"]', "article>h2>a", "Box-row>h2>a"]:
        match = art.select_one(selector)
        if match:
            print(f"  select '{selector}': href={match.get('href')} text='{match.get_text(strip=True)[:60]}'")
