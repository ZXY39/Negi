#!/usr/bin/env python3
"""Fetch GitHub Trending weekly Top 10 and generate Chinese Markdown doc."""
import re, requests, json, os, sys
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
DESKTOP = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")

TRENDING_URL = "https://github.com/trending?since=weekly"

def parse_trending():
    r = requests.get(TRENDING_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    articles = soup.select("article.Box-row") or soup.select("article.border")
    results = []
    for art in articles[:10]:
        # Repo link: first <h2> -> <a href="/owner/name">
        link_tag = art.select_one("h2 a, article a[href]")
        repo_path = link_tag.get("href", "").strip("/") if link_tag else ""
        # Repo description: <p class="col-9 ..."> or any <p> within article
        desc_tag = art.select_one("p.col-9, p[class*='col']") or art.select_one("p")
        desc = desc_tag.get_text(strip=True) if desc_tag else ""
        # Language
        lang_span = art.select_one("span[itemprop='programmingLanguage']")
        lang = lang_span.get_text(strip=True) if lang_span else "N/A"
        # Weekly stars: span.d-inline-block.float-sm-right
        wk_tag = art.select_one("span.d-inline-block.float-sm-right")
        wk_stars_text = wk_tag.get_text(strip=True) if wk_tag else ""
        m = re.search(r"([\d,]+)\s*stars?\s+this\s+week", wk_stars_text, re.I)
        wk_stars = int(m.group(1).replace(",", "")) if m else None
        # Today stars from svg parent if available
        today_tag = art.select_one("svg.octicon.octicon-star")
        today_stars = None
        if today_tag and today_tag.parent:
            tm = re.search(r"([\d,]+)\s*stars?\s+today", today_tag.parent.get_text(), re.I)
            if tm:
                today_stars = int(tm.group(1).replace(",", ""))
        results.append({
            "repo_name": repo_path,
            "repo_url": f"https://github.com/{repo_path}",
            "description": desc,
            "language": lang,
            "weekly_stars": wk_stars,
            "today_stars": today_stars,
        })
    return results


def fetch_repo_detail(repo):
    url = f"https://github.com/{repo['repo_name']}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # Total stars: multiple possible selectors
        total_stars = "?"
        # Try: <li> with "Stars" label in details page
        for li in soup.select("ul.pagehead-actions li:has(a.d-inline-block), li.d-flex:has(svg.octicon-star), li:has(text:-soup-contains('stars'))"):
            txt = li.get_text().replace(",", "").strip()
            m = re.search(r"([\d]+)\s*Stars?", txt)
            if m and int(m.group(1)) > 50:
                total_stars = int(m.group(1))
                break
        if total_stars == "?":
            # Try a.Link--muted.mr-3 pattern
            for a in soup.select("a.Link--muted, a[data-hovercard-repository]"):
                txt = a.get_text().replace(",", "").strip()
                m = re.search(r"([\d]+)\s*stars?", txt, re.I)
                if m and int(m.group(1)) > 50:
                    total_stars = int(m.group(1))
                    break
        if total_stars == "?":
            # Try meta section
            for span in soup.select("span.d-block"):
                txt = span.get_text().replace(",", "").strip()
                m = re.search(r"([\d]+)\s*stars?", txt, re.I)
                if m and int(m.group(1)) > 50:
                    total_stars = int(m.group(1))
                    break
        # Topics
        topics = [a.get_text(strip=True) for a in soup.select("a.topic-tag")]
        # License
        license_tag = soup.select_one('a[href*="license"]')
        license_name = license_tag.get_text(strip=True) if license_tag else "N/A"
        # Latest release
        rel_tag = soup.select_one('a.Link--muted[href*="/releases"], a[href*="/releases/tag"]')
        latest_release = ""
        if rel_tag:
            rspan = rel_tag.select_one("span, time")
            if rspan:
                latest_release = rspan.get_text(strip=True)
            if not latest_release:
                m2 = re.search(r"([\d.]+(?:\.\d+)*)", rel_tag.get_text())
                if m2:
                    latest_release = m2.group(1)
        # Open source badge
        os_badge = soup.select_one("span.Label--success:has-text('Open Source')")
        os_label = os_badge.get_text(strip=True) if os_badge else ""
        return {
            "total_stars": total_stars,
            "topics": topics[:8],
            "license": license_name,
            "latest_release": latest_release,
            "os_badge": os_label,
        }
    except Exception as e:
        return {"total_stars": "?", "topics": [], "license": "N/A", "latest_release": "", "os_badge": "", "error": str(e)}


def categorize_project(repo, detail):
    topics = [t.lower() for t in detail.get("topics", [])]
    desc = (repo.get("description") or "").lower()
    name = repo.get("repo_name", "").lower()
    if any(k in name or k in topics for k in ["claude", "agent", "copilot", "ai", "llm", "code-assistant", "code-gen"]):
        if any(k in topics for k in ["editor", "ide", "code"]):
            return "AI编程助手", {"dev": "AI辅助开发者、程序员、技术博主", "reason": "AI 编程工具持续迭代，提升开发效率"}
        return "AI应用", {"dev": "AI开发者、产品经理、创业者", "reason": "大模型应用爆发，开发者关注度极高"}
    if any(k in topics for k in ["llm", "ai", "language-model", "generative", "nlp"]):
        return "大模型/LLM", {"dev": "AI研究者、NLP工程师、算法工程师", "reason": "大模型训练推理框架竞争激烈"}
    if any(k in topics for k in ["framework", "web", "react", "vue", "frontend"]):
        return "开发框架", {"dev": "前端工程师、全栈开发者", "reason": "前端框架生态活跃，新特性吸引关注"}
    return "综合/其他", {"dev": "技术爱好者、学生、相关开发者", "reason": "跨领域工具，具有广泛适用性"}


def gen_markdown(trending_repos, repo_details_list):
    lines = []
    now = datetime.now().strftime("%Y年%m月%d日")
    lines.append(f"#  GitHub 过去一周热门项目 Top 10")
    lines.append("")
    lines.append(f"> **统计时间**：{now}")
    lines.append(f"> **数据来源**：[GitHub Trending (Weekly)](https://github.com/trending?since=weekly)")
    lines.append(f"> **统计口径**：基于 GitHub Trending 的 Weekly 榜单，"
                 "排序依据为项目在本周内获得的 Star 增长数。"
                 "每个项目的总 Star 数、Topics、License 等元数据均从对应 GitHub 仓库页面直接抓取。")
    lines.append("")
    lines.append("## 📊 Top 10 总览")
    lines.append("")
    lines.append("| 排名 | 项目名 | 本周 Star 增长 | 累计 Star 数 | 主要语言 |")
    lines.append("|------|--------|---------------|-------------|----------|")
    for i, (repo, detail) in enumerate(zip(trending_repos, repo_details_list)):
        repo_short = repo["repo_name"].split("/")[-1]
        wk = f"{repo.get('weekly_stars', 'N/A'):,}" if repo.get('weekly_stars') else "?"
        ts = f"{detail.get('total_stars', '?'):,}" if isinstance(detail.get('total_stars'), int) else "?"
        lines.append(f"| {i+1} | [{repo_short}]({repo['repo_url']}) | {wk} | {ts} | {repo.get('language', 'N/A')} |")
    lines.append("")

    for i, (repo, detail) in enumerate(zip(trending_repos, repo_details_list)):
        repo_short = repo["repo_name"].split("/")[-1]
        full_name = repo["repo_name"]
        cat, info = categorize_project(repo, detail)
        wk = f"{repo.get('weekly_stars', 'N/A'):,}" if repo.get('weekly_stars') else "?"
        ts = f"{detail.get('total_stars', '?'):,}" if isinstance(detail.get('total_stars'), int) else "?"
        desc = repo.get("description") or "暂无描述"
        topics_str = ", ".join(detail.get("topics", [])) or "暂无 Topics 信息"
        license_val = detail.get("license", "N/A")
        reason = f"{cat}类项目近期热门。本周新增 Star 约 {wk} 个（数据来源：GitHub Trending Weekly）。"
        if repo.get("today_stars"):
            reason += f" 今日热度趋势：约 {repo['today_stars']:,} 新增/日。"
        if detail.get("latest_release"):
            reason += f" 最新版本：{detail['latest_release']}。"
        reason += f" {info['reason']}"

        lines.append(f"### {i+1}. [{repo_short}]({repo['repo_url']})")
        lines.append("")
        lines.append(f"**项目定位**：{desc}")
        lines.append("")
        lines.append(f"**核心信息**")
        lines.append(f"- 仓库：`{full_name}` | [查看]({repo['repo_url']})")
        lines.append(f"- 本周 Star 增长：**{wk}**")
        lines.append(f"- 累计 Star 数：**{ts}**")
        lines.append(f"- 主要语言：{repo.get('language', 'N/A')}")
        lines.append(f"- Topics：{topics_str}")
        lines.append(f"- 许可证：{license_val}")
        lines.append("")
        lines.append(f"**主要做什么**")
        lines.append(f"一个{cat}类型的开源项目。{desc}")
        lines.append("")
        lines.append(f"**为什么热门**")
        lines.append(reason)
        lines.append("")
        lines.append(f"**适合谁关注**：{info['dev']}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## 📝 总结")
    lines.append("")
    lines.append("本周 GitHub Trending 榜单反映出以下趋势：")
    lines.append("")
    cats_found = set()
    for repo, detail in zip(trending_repos, repo_details_list):
        cat, _ = categorize_project(repo, detail)
        cats_found.add(cat)
    for cat in sorted(cats_found):
        lines.append(f"- **{cat}** 类项目持续占据热门位置，说明开发者社区对该方向保持高度关注。")
    lines.append("")
    lines.append("> 本文数据来源于 GitHub 公开页面，统计口径已在文中说明。"
                 "由于 GitHub Trending 数据会实时更新，文中 Star 数值为抓取时点的快照。")

    return "\n".join(lines)


def main():
    print("=== Step 1: Fetching GitHub Trending (weekly) ===")
    trending = parse_trending()
    print(f"Found {len(trending)} repos")
    for r in trending:
        wk = r.get('weekly_stars')
        print(f"  - {r['repo_name']}: weekly={wk}, today={r.get('today_stars')}, lang={r.get('language')}, desc={r['description'][:60]}...")

    print("\n=== Step 2: Fetching repo details ===")
    results = [None] * len(trending)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_repo_detail, r): i for i, r in enumerate(trending)}
        for f in futures:
            idx = futures[f]
            try:
                results[idx] = f.result()
            except Exception as e:
                results[idx] = {"total_stars": "?", "topics": [], "license": "N/A", "latest_release": "", "error": str(e)}
            d = results[idx]
            print(f"  {trending[idx]['repo_name']}: stars={d.get('total_stars')}, topics={d.get('topics', [])[:3]}")

    print(f"\n=== Step 3: Generate document ===")
    md = gen_markdown(trending, results)
    filepath = os.path.join(DESKTOP, "GitHub_Trending_Weekly_Top10.md")
    with open(filepath, "w", encoding="utf-8-sig") as f:
        f.write(md)
    print(f"Saved to {filepath} ({os.path.getsize(filepath)} bytes)")
    print(f"Projects count: {len(trending)}")
    # Validate
    missing = []
    for i, (r, d) in enumerate(zip(trending, results)):
        issues = []
        if not r.get('weekly_stars'):
            issues.append("weekly_stars missing")
        if d.get('total_stars') == "?":
            issues.append("total_stars missing")
        if not d.get('topics'):
            issues.append("topics missing")
        if issues:
            missing.append(f"  #{i+1} {r['repo_name']}: {', '.join(issues)}")
    if missing:
        print("Validation warnings:")
        for m in missing:
            print(m)
    else:
        print("All fields validated successfully!")


if __name__ == "__main__":
    main()
