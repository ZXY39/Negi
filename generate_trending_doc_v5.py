#!/usr/bin/env python3
"""Fetch GitHub Trending weekly Top 10 and generate Chinese Markdown doc. v5"""
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
        # Repo link: h2 > a
        link_tag = art.select_one("h2 a")
        repo_path = link_tag.get("href", "").strip("/") if link_tag else ""
        # Description: first <p> that's not inside h2
        desc_tag = art.select_one("p.col-9") or art.select_one("p")
        desc = desc_tag.get_text(strip=True) if desc_tag else ""
        # Language
        lang_span = art.select_one("span[itemprop='programmingLanguage']")
        lang = lang_span.get_text(strip=True) if lang_span else "N/A"
        # Total stars: a[href*="/stargazers"]
        star_link = art.select_one('a[href*="/stargazers"]')
        total_stars = None
        if star_link:
            m = re.search(r"([\d,]+)", star_link.get_text())
            if m:
                total_stars = int(m.group(1).replace(",", ""))
        # Weekly stars: span.d-inline-block.float-sm-right
        wk_tag = art.select_one("span.d-inline-block.float-sm-right")
        wk_stars = None
        if wk_tag:
            m2 = re.search(r"([\d,]+)\s*stars?\s+this\s+week", wk_tag.get_text(), re.I)
            if m2:
                wk_stars = int(m2.group(1).replace(",", ""))
        # Forks
        fork_link = art.select_one('a[href*="/forks"]')
        forks = None
        if fork_link:
            m3 = re.search(r"([\d,]+)", fork_link.get_text())
            if m3:
                forks = int(m3.group(1).replace(",", ""))
        results.append({
            "repo_name": repo_path,
            "repo_url": f"https://github.com/{repo_path}",
            "description": desc,
            "language": lang,
            "weekly_stars": wk_stars,
            "total_stars": total_stars,
            "forks": forks,
        })
    return results


def fetch_repo_detail(repo_name):
    url = f"https://github.com/{repo_name}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # Topics
        topics = [a.get_text(strip=True) for a in soup.select("a.topic-tag")]
        # License
        license_tag = soup.select_one('li:has(a[href*="license"]) a') or soup.select_one('a[href*="license"]')
        license_name = license_tag.get_text(strip=True) if license_tag else "N/A"
        # Latest release
        rel_link = soup.select_one('a[data-testid="latest-release"]') or soup.select_one('a[href*="/releases/tag"]')
        latest_release = ""
        if rel_link:
            latest_release = rel_link.get_text(strip=True)
        return {"topics": topics[:10], "license": license_name, "latest_release": latest_release}
    except Exception as e:
        return {"topics": [], "license": "N/A", "latest_release": "", "error": str(e)}


def categorize_project(repo, detail):
    topics = [t.lower() for t in detail.get("topics", [])]
    desc = (repo.get("description") or "").lower()
    name = repo.get("repo_name", "").lower()
    if any(k in name for k in ["claude-howto", "oh-my-codex", "vibevoice", "openscreen"]):
        if any(k in name or k in topics for k in ["claude", "codex", "code-assistant", "voice", "demo"]):
            return "AI开发工具", "AI开发者、程序员、技术爱好者"
    if any(k in name for k in ["hermes-agent", "last30days", "ai-scientist"]):
        return "AI智能体", "AI研究者、算法工程师、数据科学家"
    if any(k in topics for k in ["llm", "ai", "time-series", "forecasting"]):
        return "AI/机器学习", "AI研究者、数据工程师"
    return "综合工具", "开发者、技术爱好者"


def gen_markdown(trending_repos, repo_details_list):
    lines = []
    now = datetime.now().strftime("%Y年%m月%d日")
    lines.append("# GitHub 过去一周热门项目 Top 10")
    lines.append("")
    lines.append(f"> **生成日期**：{now}")
    lines.append(f"> **数据来源**：[GitHub Trending Weekly](https://github.com/trending?since=weekly)")
    lines.append(f"> **统计口径**：基于 GitHub Trending 的 Weekly 榜单，"
                 "排序依据为项目在过去 7 天内获得的 Star 增长数。"
                 "各项目总 Star 数、Fork 数来自 Trending 页面快照，"
                 "Topics、License 等元数据均从对应 GitHub 仓库页面直接抓取。"
                 "由于 GitHub Trending 数据会实时更新，本文数值为抓取时刻的快照。")
    lines.append("")

    # Overview table
    lines.append("## 📊 Top 10 总览")
    lines.append("")
    lines.append("| 排名 | 项目名 | 本周 Star 增长 | 累计 Star 数 | Fork 数 | 主要语言 |")
    lines.append("|------|--------|---------------|-------------|---------|----------|")
    for i, repo in enumerate(trending_repos):
        repo_short = repo["repo_name"].split("/")[-1]
        wk = f"{repo['weekly_stars']:,}" if repo.get('weekly_stars') else "?"
        ts = f"{repo['total_stars']:,}" if repo.get('total_stars') else "?"
        fk = f"{repo['forks']:,}" if repo.get('forks') else "?"
        lines.append(f"| {i+1} | [{repo_short}]({repo['repo_url']}) | {wk} | {ts} | {fk} | {repo.get('language', 'N/A')} |")
    lines.append("")

    # Individual sections
    for i, (repo, detail) in enumerate(zip(trending_repos, repo_details_list)):
        repo_short = repo["repo_name"].split("/")[-1]
        full_name = repo["repo_name"]
        cat = categorize_project(repo, detail)
        wk = f"{repo['weekly_stars']:,}" if repo.get('weekly_stars') else "?"
        ts = f"{repo['total_stars']:,}" if repo.get('total_stars') else "?"
        fk = f"{repo['forks']:,}" if repo.get('forks') else "?"
        desc = repo.get("description") or "暂无描述"
        topics_str = ", ".join(detail.get("topics", [])) or "暂无 Topics 信息"
        license_val = detail.get("license", "N/A")
        latest_rel = detail.get("latest_release", "")

        # Hot reason analysis
        reasons = []
        reasons.append(f"本周新增 Star 约 **{wk}** 个，在 GitHub Trending Weekly 榜单中排名第 **{i+1}**。")
        if repo.get('total_stars') and repo['total_stars'] > 5000:
            reasons.append(f"累计 Star 数已达 **{ts}**，属于高关注度的成熟项目。")
        elif repo.get('total_stars') and repo['total_stars'] < 10000:
            reasons.append(f"累计 Star 数 **{ts}**，属于快速上升期的新星项目。")
        if latest_rel:
            reasons.append(f"最新版本 **{latest_rel}**，项目保持活跃迭代。")
        if repo.get('forks') and repo['forks'] > 500:
            reasons.append(f"Fork 数 **{fk}**，社区参与度高。")
        reasons.append(f"作为**{cat[0]}**类项目，{cat[1]}群体关注度高。")

        lines.append(f"### {i+1}. [{repo_short}]({repo['repo_url']})")
        lines.append("")
        lines.append(f"- **一句话定位**：{desc}")
        lines.append(f"- **仓库**：`{full_name}` | [GitHub]({repo['repo_url']})")
        lines.append(f"- **本周 Star 增长**：**{wk}**")
        lines.append(f"- **累计 Star 数**：**{ts}**")
        lines.append(f"- **Fork 数**：**{fk}**")
        lines.append(f"- **主要语言**：{repo.get('language', 'N/A')}")
        lines.append(f"- **Topics**：{topics_str}")
        lines.append(f"- **许可证**：{license_val}")
        lines.append("")
        lines.append(f"**主要做什么**")
        lines.append(f"{desc}")
        lines.append("")
        lines.append(f"**为什么最近热门**")
        for reason in reasons:
            lines.append(f"- {reason}")
        lines.append("")
        lines.append(f"**适合关注人群**：{cat[1]}")
        if latest_rel:
            lines.append(f"**最新版本**：{latest_rel}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Summary
    lines.append("## 📝 简短总结")
    lines.append("")
    lines.append("本周 GitHub Trending 榜单反映出以下趋势：")
    lines.append("")
    cats = {}
    for repo, detail in zip(trending_repos, repo_details_list):
        cat = categorize_project(repo, detail)[0]
        cats[cat] = cats.get(cat, 0) + 1
    for cat_name, count in sorted(cats.items(), key=lambda x: -x[1]):
        lines.append(f"- **{cat_name}**（{count} 个项目）：开发者持续保持高度关注，AI 工具链和智能体方向尤为活跃。")
    lines.append("")
    top_repo = trending_repos[0]
    top_wk = f"{top_repo['weekly_stars']:,}" if top_repo.get('weekly_stars') else "?"
    lines.append(f"🏆 本周冠军 **[{top_repo['repo_name'].split('/')[-1]}]({top_repo['repo_url']})** "
                 f"以 **{top_wk}** 个新增 Star 领跑。" if top_repo.get('weekly_stars') else "领跑。")
    lines.append("")
    lines.append("> 本文数据来源于 GitHub 公开页面，所有 Star 数值为页面抓取时刻的快照。"
                 "GitHub Trending 数据会实时变化，具体数值请以 [GitHub Trending 页面](https://github.com/trending) 为准。")
    return "\n".join(lines)


def main():
    print("=== Step 1: Fetching GitHub Trending (weekly) ===")
    trending = parse_trending()
    print(f"Found {len(trending)} repos")
    for r in trending:
        print(f"  - {r['repo_name']}: weekly={r.get('weekly_stars')}, total={r.get('total_stars')}, forks={r.get('forks')}, "
              "lang={r.get('language')}, desc={r['description'][:70]}...")
    if not trending:
        print("ERROR: No repos found!")
        sys.exit(1)

    print("\n=== Step 2: Fetching repo details (topics, license, release) ===")
    results = [None] * len(trending)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_repo_detail, r["repo_name"]): i for i, r in enumerate(trending)}
        for f in futures:
            idx = futures[f]
            try:
                results[idx] = f.result()
            except Exception as e:
                results[idx] = {"topics": [], "license": "N/A", "latest_release": "", "error": str(e)}
            print(f"  {trending[idx]['repo_name']}: topics={results[idx].get('topics', [])[:3]}, "
                  "license={results[idx].get('license')}, release={results[idx].get('latest_release')}")

    print(f"\n=== Step 3: Generate document ===")
    md = gen_markdown(trending, results)
    filepath = os.path.join(DESKTOP, "GitHub_Trending_Weekly_Top10.md")
    with open(filepath, "w", encoding="utf-8-sig") as f:
        f.write(md)
    print(f"Saved to {filepath} ({os.path.getsize(filepath)} bytes, {md.count(chr(10))+1} lines)")
    print(f"Projects count: {len(trending)}")

    # Validate
    print("\n=== Validation ===")
    missing = []
    for i, r in enumerate(trending):
        issues = []
        if not r.get('weekly_stars'): issues.append("weekly_stars missing")
        if not r.get('total_stars'): issues.append("total_stars missing")
        if not r.get('forks'): issues.append("forks missing")
        if issues:
            missing.append(f"  #{i+1} {r['repo_name']}: {', '.join(issues)}")
    for i, d in enumerate(results):
        if not d.get('topics'):
            missing.append(f"  #{i+1} topics missing")
        if d.get('license') == 'N/A':
            missing.append(f"  #{i+1} license not found on page (may still exist)")
    if missing:
        print("Validation warnings:")
        for m in missing: print(m)
    else:
        print("All fields validated successfully!")


if __name__ == "__main__":
    main()
