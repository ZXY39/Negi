import requests
from bs4 import BeautifulSoup
import json
import time

repos = [
    "luongnv89/claude-howto",
    "microsoft/VibeVoice",
    "Yeachan-Heo/oh-my-codex",
    "Yeachan-Heo/oh-my-claudecode",
    "hacksider/Deep-Live-Cam",
    "siddharthvaddem/openscreen",
    "google-research/timesfm",
    "NousResearch/hermes-agent",
    "SakanaAI/AI-Scientist-v2",
    "mvanhorn/last30days-skill"
]

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
results = []

for repo in repos:
    url = f"https://github.com/{repo}"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    
    desc_el = soup.select_one("p.mb-3.f4.mt-3")
    desc = desc_el.text.strip() if desc_el else "No description"
    
    star_el = soup.select_one("#repo-stars-counter-star")
    if not star_el:
        star_el = soup.select_one('a[href$="/stargazers"] .Counter')
    total_stars = star_el["title"] if star_el and star_el.get("title") else "N/A"
    
    lic_el = soup.select_one('a[aria-label*="license"]')
    license_text = lic_el.text.strip() if lic_el else "N/A"
    
    topics = [t.text.strip() for t in soup.select(".topic-tag")][:5]
    
    lang_el = soup.select_one('[itemprop="programmingLanguage"]')
    lang = lang_el.text.strip() if lang_el else "N/A"
    
    # try to get recent releases or commit activity
    releases_text = ""
    rel_el = soup.select_one('a[href$="/releases"]')
    if rel_el:
        releases_text = rel_el.text.strip()
    
    results.append({
        "repo": repo,
        "desc": desc,
        "total_stars": total_stars,
        "license": license_text,
        "topics": topics,
        "language": lang,
        "recent_releases": releases_text
    })
    print(f"Got: {repo} | {desc[:60]}")
    time.sleep(1)

print("\n---JSON_OUTPUT---")
print(json.dumps(results, ensure_ascii=False, indent=2))
