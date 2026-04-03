import requests, re
from bs4 import BeautifulSoup

r = requests.get('https://github.com/luongnv89/claude-howto', headers={'User-Agent':'Mozilla/5.0'}, timeout=20)
soup = BeautifulSoup(r.text, 'html.parser')

# method 1: find stargazers link
star_a = soup.find('a', href=re.compile(r'/stargazers'))
print('=== Method 1: a[href*=/stargazers] ===')
print('exists:', star_a is not None)
if star_a:
    print('html:', repr(str(star_a))[:600])
    # try to find any child with id
    for child in star_a.find_all(True):
        print(f'  child id={child.get("id")} class={child.get("class")}')

# method 2: nav open graph
print('\n=== Method 2: nav ===')
for nav_a in soup.find_all('a', class_=lambda c: c and 'nav-open' in c):
    print('nav:', nav_a.get('href'), nav_a.get_text()[:80])

# method 3: find all star-related
print('\n=== Method 3: all star id tags ===')
for tag in soup.find_all(id=True):
    if 'star' in tag.get('id', '').lower():
        print(f'  id={tag["id"]} text={tag.get_text()[:50]}')

# method 4: meta og tags
print('\n=== Method 4: meta tags ===')
for meta in soup.find_all('meta'):
    name = meta.get('name', meta.get('property', ''))
    if 'star' in name.lower():
        print(f'  {name}={meta.get("content")}')

# method 5: any element with star count pattern
print('\n=== Method 5: search text pattern ===')
for a in soup.find_all('a', href=re.compile(r'/stargazers')):
    print('link text:', repr(a.get_text()[:100]))
    print('link html:', repr(str(a)[:600]))
