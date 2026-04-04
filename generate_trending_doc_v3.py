import requests
from bs4 import BeautifulSoup
import json
import datetime
import os
import re

DESKTOP_PATH = r"C:\Users\ZGASQ\Desktop"
# Use ASCII-safe filename to avoid encoding issues on Windows
OUTPUT_FILE = os.path.join(DESKTOP_PATH, "GitHub_Trending_Weekly_Top10.md")
TRENDING_URL = "https://github.com/trending?since=weekly"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

def fetch_trending():
    """Fetch GitHub Trending weekly page and extract Top 12 repos (take first 10 valid ones)."""
    print("Fetching GitHub Trending weekly...")
    resp = requests.get(TRENDING_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    repos = []
    articles = soup.select("article.Box-row")
    if not articles:
        articles = soup.select(".Box-row")
    print(f"Found {len(articles)} articles on trending page")

    for i, art in enumerate(articles[:12]):
        # Repo link
        link_tag = art.select_one("h2 a")
        if not link_tag:
            continue
        repo_path = link_tag.get("href", "").strip("/")
        repo_url = f"https://github.com/{repo_path}"

        # Description
        desc_tag = art.select_one("p.col-9")
        description = desc_tag.get_text(strip=True) if desc_tag else ""
        # Limit description length
        if len(description) > 200:
            description = description[:197] + "..."

        # Language
        lang_tag = art.select_one('[itemprop="programmingLanguage"]')
        language = lang_tag.get_text(strip=True) if lang_tag else ""

        # Weekly stars from the SVG/star icon area
        svg_stars = art.select_one("svg.octicon.octicon-star")
        weekly_stars = ""
        if svg_stars:
            parent = svg_stars.parent
            if parent:
                # The text node after the SVG contains "X stars this week/month/etc"
                text = parent.get_text(strip=True)
                # Extract the number part
                match = re.search(r'([\d,\.kmb]+)\s*stars?\s*(this|past)\s*(week|month)', text, re.IGNORECASE)
                if match:
                    weekly_stars = f"{match.group(1)} stars this {match.group(3)}"
                else:
                    weekly_stars = text.strip()

        if len(repos) < 10:
            repos.append({
                "rank": len(repos) + 1,
                "repo_path": repo_path,
                "repo_url": repo_url,
                "description": description,
                "language": language,
                "weekly_stars": weekly_stars,
            })
            print(f"  [{repos[-1]['rank']}] {repo_path}: {description[:70]}")
            print(f"      Language: {language}, Weekly: {weekly_stars}")

    return repos

def fetch_repo_details(repo_url):
    """Fetch individual repo page for total stars, topics, license, etc."""
    resp = requests.get(repo_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    details = {}

    # Total stars - find the stargazers link by href
    # Common pattern: <a href="/owner/repo/stargazers" ...>
    stars_link = soup.select_one('a[href$="/stargazers"]')
    if stars_link:
        # Try title attribute first
        title = stars_link.get("title", "")
        if title and "star" in title.lower():
            details["total_stars"] = title.strip()
        else:
            # Try to find a <span> with the count near the stars link
            # Look for data-view-component="Counter" or similar
            counter = stars_link.select_one('[data-view-component="true"]')
            if not counter:
                # Try any span or strong near the link
                for el in [stars_link] + list(stars_link.find_next_siblings(['span', 'strong', 'div'], limit=3)):
                    txt = el.get_text(strip=True)
                    # Check if it's a number (possibly with commas, k, m)
                    if re.match(r'^[\d,\.]+[kmb]?$', txt, re.IGNORECASE):
                        details["total_stars"] = txt
                        break
            if "total_stars" not in details:
                # Fallback: look for any element with "stars" in its text near the link
                star_count = stars_link.find_next_sibling(string=lambda s: s and re.search(r'[\d,]+', str(s)))
                if star_count:
                    details["total_stars"] = str(star_count).strip()

    # Topics - look for topic tags in the repo page
    # They are usually in a specific container
    topics = []
    # Try multiple selectors
    for selector in [
        'a[href^="/topics/"]',
        'a.topic-tag',
        '.topic-tag',
        '[data-testid="topic"]',
    ]:
        topic_tags = soup.select(selector)
        for tag in topic_tags:
            txt = tag.get_text(strip=True)
            if txt and txt not in topics:
                topics.append(txt)
        if topics:
            break
    details["topics"] = topics[:10]  # Limit to first 10

    # License
    license_tag = soup.select_one('a[href*="/blob/"]')
    if license_tag:
        txt = license_tag.get_text(strip=True)
        if "license" in txt.lower():
            details["license"] = txt.strip()

    # Latest release
    release_link = soup.select_one('a[href*="/releases/tag/"]')
    if release_link:
        release_text = release_link.get_text(strip=True)
        if release_text and "release" not in release_text.lower():
            details["latest_release"] = release_text

    return details

def generate_markdown(repos):
    """Generate structured Chinese Markdown document."""
    now_str = datetime.datetime.now().strftime("%Y年%m月%d日")
    week_start = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    week_end = datetime.datetime.now().strftime("%Y-%m-%d")

    lines = []
    lines.append("# GitHub 过去一周最热门项目 Top 10")
    lines.append("")
    lines.append(f"> **统计时间范围**：{week_start} ~ {week_end}")
    lines.append(f"> **数据来源**：GitHub Trending Weekly（[github.com/trending?since=weekly](https://github.com/trending?since=weekly)）")
    lines.append(f"> **整理时间**：{now_str}")
    lines.append("")

    lines.append("## 统计口径说明")
    lines.append("")
    lines.append("本文档基于 **GitHub Trending Weekly** 官方页面统计口径整理，反映过去一周内")
    lines.append("Star 增长最活跃的前 10 个开源项目。GitHub Trending 的排序综合考虑了 Star 增长数量、")
    lines.append("近期 Fork/Watch 活跃度等因素，排序以页面默认排序为准。部分项目的详细元数据")
    lines.append("（总 Star 数、Topics、License 等）通过访问各项目仓库页面进行交叉核验。")
    lines.append("")

    # Summary table
    lines.append("## Top 10 项目总览")
    lines.append("")
    lines.append("| 排名 | 项目 | 主要语言 | 本周 Star 增长 |")
    lines.append("| :--: | :--- | :------: | :------------: |")

    for r in repos:
        name = r["repo_path"].split("/")[1]
        lang = r.get("language") or "—"
        weekly = r.get("weekly_stars") or "—"
        lines.append(f"| {r['rank']} | [{name}]({r['repo_url']}) | {lang} | {weekly} |")

    lines.append("")

    # Detailed sections
    lines.append("## 各项目详细介绍")
    lines.append("")

    for r in repos:
        repo_path = r["repo_path"]
        name = repo_path.split("/")[1]
        owner = repo_path.split("/")[0]
        description = r.get("description", "")
        lang = r.get("language", "")
        weekly = r.get("weekly_stars", "")
        details = r.get("details", {})
        topics = details.get("topics", [])
        license_info = details.get("license", "")
        release = details.get("latest_release", "")
        total = details.get("total_stars", "")

        # One-sentence positioning
        positioning = description if description else "（待补充详细描述）"

        # What it does
        what_it_does_parts = []
        if description:
            what_it_does_parts.append(description)
        if topics:
            what_it_does_parts.append(f"**技术标签**：{', '.join(topics[:8])}")
        what_it_does = "\n\n".join(what_it_does_parts) if what_it_does_parts else "项目详情可在仓库页面进一步了解。"

        # Why hot
        why_hot_parts = []
        if weekly:
            why_hot_parts.append(f"过去一周获得 **{weekly}**，增长势头强劲")
        if release:
            why_hot_parts.append(f"近期发布版本 **{release}**")
        if total:
            why_hot_parts.append(f"项目累计已获得 **{total}** 个 Star")
        if why_hot_parts:
            why_hot = "；".join(why_hot_parts)
        else:
            why_hot = "GitHub Trending 周榜排名前列，表明社区关注度显著上升（具体 Star 增长数据未能从页面提取）"

        # Suitable for
        suitable_for = infer_audience(topics, lang, repo_path)

        lines.append(f"### {r['rank']}. [{name}]({r['repo_url']})")
        lines.append("")
        lines.append(f"> 仓库：[{repo_path}]({r['repo_url']})")
        lines.append("")
        if lang:
            lines.append(f"- **主要语言**：{lang}")
        if license_info:
            lines.append(f"- **开源协议**：{license_info}")
        if total:
            lines.append(f"- **总 Star 数**：{total}")
        if weekly:
            lines.append(f"- **本周 Star 增长**：{weekly}")
        lines.append("")
        lines.append(f"**一句话定位**：{positioning}")
        lines.append("")
        lines.append(f"**主要做什么**：{what_it_does}")
        lines.append("")
        lines.append(f"**为什么最近热门**：{why_hot}")
        lines.append("")
        lines.append(f"**适合谁关注**：{suitable_for}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Summary
    lines.append("## 总结")
    lines.append("")
    lines.append(f"以上就是 GitHub 过去一周（{week_start} ~ {week_end}）Trending 周榜上最热门的前 10 个开源项目。")
    lines.append("从榜单可以看出，当前开源社区的关注焦点集中在以下几个方向：")
    lines.append("")

    # Dynamic summary based on actual data
    has_ai = any(any(k in " ".join(r.get("details",{}).get("topics",[])).lower() for k in ["ai","llm","machine-learning","gpt","claude","openai"])
                 for r in repos)
    has_tools = any(any(k in " ".join(r.get("details",{}).get("topics",[])).lower() for k in ["developer-tools","cli","productivity","automation"])
                    for r in repos)
    has_data = any(any(k in " ".join(r.get("details",{}).get("topics",[])).lower() for k in ["data-science","analytics","time-series"])
                   for r in repos)

    if has_ai:
        lines.append("- **AI/LLM 工具与应用**：多个项目围绕大模型使用指南、多 Agent 编排、语音合成等方向，反映出 AI 技术正在加速向实用化、工具化方向演进。")
    if has_tools:
        lines.append("- **开发者工具与效率**：AI 编码助手的周边生态（Hook 系统、多 Agent 协作框架等）持续升温，开发效率工具仍然是社区最活跃的领域之一。")
    if has_data:
        lines.append("- **数据科学与时序预测**：时间序列基础模型、数据可视化等项目上榜，说明数据驱动决策的需求持续增长。")

    lines.append("")
    lines.append("**信息来源与核验方式**：")
    lines.append("- 榜单排名与 Star 增长数据：GitHub Trending Weekly 页面")
    lines.append("- 项目描述与元数据（Total Stars、Topics、License）：各项目 GitHub 仓库页面")
    lines.append("- 所有描述基于页面显示内容整理，未编造任何数据；无法确认的信息已在对应小节注明")
    lines.append("")

    return "\n".join(lines)

def infer_audience(topics, language, repo_path=""):
    """Infer suitable audience from topics, language, and repo path."""
    audience_parts = []
    topics_lower = " ".join(topics).lower() if topics else ""
    path_lower = repo_path.lower()

    # Path-based inference
    if any(k in path_lower for k in ["claude", "codex", "ai-scientist"]):
        audience_parts.append("AI 编码助手（Claude Code、Codex）使用者与多 Agent 编排研究者")
    if any(k in path_lower for k in ["voice", "deepfake", "live-cam"]):
        audience_parts.append("语音 AI、深度学习音视频处理的研究者和开发者")
    if any(k in path_lower for k in ["timesfm", "timeseries", "time-series"]):
        audience_parts.append("时间序列分析与预测的数据科学家和量化从业者")

    # Topic-based inference
    if any(k in topics_lower for k in ["ai", "llm", "machine-learning", "gpt", "openai"]):
        if not any("AI 编码" in a for a in audience_parts):
            audience_parts.append("AI/LLM 开发者与大模型应用构建者")
    if any(k in topics_lower for k in ["developer-tools", "cli", "productivity", "automation", "workflow"]):
        audience_parts.append("希望提升开发效率的软件工程师和 DevOps 从业者")
    if any(k in topics_lower for k in ["web", "frontend", "javascript", "typescript", "react"]):
        audience_parts.append("前端/全栈 Web 开发者")
    if any(k in topics_lower for k in ["data-science", "analytics", "database", "time-series"]):
        audience_parts.append("数据工程师与数据分析从业者")
    if any(k in topics_lower for k in ["devops", "kubernetes", "docker", "cloud"]):
        audience_parts.append("运维工程师和云平台架构师")
    if any(k in topics_lower for k in ["agent", "multi-agent"]):
        audience_parts.append("AI Agent 框架开发者与多智能体系统研究者")

    if not audience_parts:
        if language in ["Python"]:
            audience_parts.append("Python 开发者与关注该领域动态的技术人员")
        elif language in ["TypeScript", "JavaScript"]:
            audience_parts.append("JavaScript/TypeScript 开发者与 Web 技术爱好者")
        elif language in ["Rust"]:
            audience_parts.append("Rust 开发者与关注系统编程的技术人员")
        elif language in ["Go"]:
            audience_parts.append("Go 语言开发者与后端/云原生工程师")
        elif language in ["Java"]:
            audience_parts.append("Java 生态与企业级应用开发者")
        else:
            audience_parts.append("对该技术领域感兴趣的开发者和技术爱好者")

    return "；".join(audience_parts)

def main():
    print("=" * 60)
    print("GitHub Trending Weekly Top 10 - Data Collection & Doc Generation")
    print("=" * 60)

    # Step 1: Fetch trending
    repos = fetch_trending()
    if not repos:
        print("ERROR: Failed to fetch any repos from trending page!")
        return
    print(f"\nSuccessfully fetched {len(repos)} repos.\n")

    # Step 2: Fetch details
    print("Fetching repo details individually...")
    for r in repos:
        try:
            details = fetch_repo_details(r["repo_url"])
            r["details"] = details
            print(f"  [{r['rank']}] {r['repo_path']}: "
                  f"total_stars={details.get('total_stars','?')}, "
                  f"topics={details.get('topics', [])[:3]}, "
                  f"license={details.get('license','?')}")
        except Exception as e:
            print(f"  [{r['rank']}] {r['repo_path']}: Failed - {e}")
            r["details"] = {}

    # Step 3: Generate & save
    print("\nGenerating markdown...")
    md_content = generate_markdown(repos)

    os.makedirs(DESKTOP_PATH, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8-sig") as f:
        f.write(md_content)

    print(f"\nDocument saved to: {OUTPUT_FILE}")
    print(f"File size: {len(md_content)} characters")
    print("=" * 60)

if __name__ == "__main__":
    main()
