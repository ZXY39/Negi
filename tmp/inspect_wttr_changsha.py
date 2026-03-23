import urllib.request, urllib.parse, ssl
q = urllib.parse.quote('Changsha')
url = f'https://wttr.in/{q}?format=j1'
req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=30) as resp:
    txt = resp.read().decode('utf-8')
print(txt[:4000])
