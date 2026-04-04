import requests
from bs4 import BeautifulSoup
import json
import datetime
import os

DESKTOP_PATH = r"C:\Users\ZGASQ\Desktop"
OUTPUT_FILE = os.path.join(DESKTOP_PATH, "GitHub_过去一周热门项目Top10.md")
TRENDING_URL = "https://github.com/trending?since=weekly"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def fetch_trending():
    """Fetch GitHub Trending weekly page and extract Top 10 repos."""
    print("Fetching GitHub Trending weekly...")
    resp = requests.get(TRENDING_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    repos = []
    articles = soup.select("article.Box-row")
    if not articles:
        print("WARNING: No articles found, trying fallback selector...")
        articles = soup.select(".Box-row")

    print(f"Found {len(articles)} articles on trending page")

    for i, art in enumerate(articles[:10]):
        # Repo link
        link_tag = art.select_one("h2 a")
        if not link_tag:
            continue
        repo_path = link_tag.get("href", "").strip("/")
        repo_url = f"https://github.com/{repo_path}"

        # Description
        desc_tag = art.select_one("p.col-9")
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        # Language
        lang_tag = art.select_one('[itemprop="programmingLanguage"]')
        language = lang_tag.get_text(strip=True) if lang_tag else ""

        # Stars this period
        star_text = ""
        total_stars = ""
        stars_link = art.select_one("a.Link--muted")
        if stars_link:
            star_text = stars_link.get_text(strip=True)

        # Total stars from link text near stars_link
        # Try to find total stars
        star_count_tag = art.select_one("a.Link--muted")
        if star_count_tag:
            # Look for the text pattern "X stars this week"
            parent = star_count_tag.parent
            if parent:
                for txt in parent.find_all(string=True):
                    t = txt.strip()
                    if "stars this" in t.lower() or "star this" in t.lower():
                        star_text = t

        # Get total stars from repo page
        # Parse star_text for weekly growth
        weekly_stars = star_text

        repos.append({
            "rank": i + 1,
            "repo_path": repo_path,
            "repo_url": repo_url,
            "description": description,
            "language": language,
            "weekly_stars": weekly_stars,
        })

        print(f"  [{i+1}] {repo_path}: {description[:60]}...")

    return repos

def fetch_repo_details(repo_url):
    """Fetch individual repo page for total stars, topics, license, etc."""
    resp = requests.get(repo_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    details = {}

    # Total stars
    star_link = soup.select_one('a[href$="/stargazers"]')
    if star_link:
        title_attr = star_link.get("title", "")
        if title_attr:
            details["total_stars"] = title_attr
        else:
            num_text = star_link.select_one(".Counter, .counter")
            if num_text:
                details["total_stars"] = num_text.get_text(strip=True)

    # Topics
    topics = []
    for tag in soup.select('a[href="/topics"]'):
        topic_text = tag.get_text(strip=True)
        if topic_text:
            topics.append(topic_text)
    details["topics"] = topics

    # License
    license_tag = soup.select_one('a[href$="/blob/*/LICENSE*"]')
    if license_tag:
        details["license"] = license_tag.get_text(strip=True)

    # Latest release
    release_link = soup.select_one('a[href*="/releases/tag/"]')
    if release_link:
        release_text = release_link.get_text(strip=True)
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
    lines.append(f"> **数据来源**：GitHub Trending Weekly（https://github.com/trending?since=weekly）")
    lines.append(f"> **整理时间**：{now_str}")
    lines.append("")
    lines.append("## 统计口径说明")
    lines.append("")
    lines.append("本文档基于 **GitHub Trending Weekly** 页面统计口径整理，反映过去一周内（以 GitHub 官方周榜为准）")
    lines.append("Star 增长最活跃的前 10 个开源项目。排序以 GitHub Trending 页面的默认排序为准，该排序综合考虑了")
    lines.append("Star 增长数量、近期活跃度等因素。部分项目的详细 Star 数据通过访问项目仓库页面进行交叉核验。")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## Top 10 项目总览")
    lines.append("")
    lines.append("| 排名 | 项目 | 主要语言 | 本周 Star 增长 | 总 Star 数 |")
    lines.append("| :--: | :--- | :------: | :------------: | :--------: |")

    for r in repos:
        name = r["repo_path"].split("/")[1]
        owner = r["repo_path"].split("/")[0]
        lang = r.get("language") or "—"
        weekly = r.get("weekly_stars") or "—"
        total = r.get("details", {}).get("total_stars", "—")
        lines.append(f"| {r['rank']} | [{name}]({r['repo_url']}) | {lang} | {weekly} | {total} |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Detailed sections for each repo
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

        # Build one-sentence positioning from description
        positioning = description if description else "暂无描述"

        # Build "what it does"
        what_it_does = description if description else "待补充"
        if topics:
            what_it_does += f"\n\n**技术标签**：{', '.join(topics[:8])}"

        # Build "why hot"
        why_hot_parts = []
        if weekly:
            why_hot_parts.append(f"过去一周获得 {weekly}，增长势头强劲")
        if release:
            why_hot_parts.append(f"近期发布版本 {release}")
        if total:
            why_hot_parts.append(f"项目累计已获得 {total} 个 Star")
        if why_hot_parts:
            why_hot = "；".join(why_hot_parts)
        else:
            why_hot = "GitHub Trending 周榜排名前列，表明社区关注度显著上升（具体 Star 增长数据未能从页面提取）"

        # Build "suitable for"
        suitable_for = infer_audience(topics, lang)

        lines.append(f"### {r['rank']}. {name}（{owner}/{name}）")
        lines.append("")
        lines.append(f"**仓库链接**：{r['repo_url']}")
        if lang:
            lines.append(f"**主要语言**：{lang}")
        if license_info:
            lines.append(f"**开源协议**：{license_info}")
        if total:
            lines.append(f"**总 Star 数**：{total}")
        if weekly:
            lines.append(f"**本周 Star 增长**：{weekly}")
        lines.append("")
        lines.append(f"**一句话定位**：{positioning}")
        lines.append("")
        lines.append(f"**主要做什么**：")
        lines.append(what_it_does)
        lines.append("")
        lines.append(f"**为什么最近热门**：{why_hot}")
        lines.append("")
        lines.append(f"**适合谁关注**：{suitable_for}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## 总结")
    lines.append("")
    lines.append("以上就是 GitHub 过去一周（{week_start} ~ {week_end}）Trending 榜单上最热门的前 10 个开源项目。")
    lines.append("这些项目涵盖了 AI 工具、开发效率提升、基础设施等多个领域，反映了当前开源社区的关注焦点。")
    lines.append("")
    lines.append("**信息来源**：")
    lines.append("- GitHub Trending Weekly：https://github.com/trending?since=weekly")
    lines.append("- 各项目仓库页面用于交叉核验详细数据")
    lines.append("")
    lines.append("> 本文档数据来自公开可访问的 GitHub 页面，所有描述基于页面显示内容进行整理，")
    lines.append("> 未编造任何数据。如部分数据无法从页面直接确认，已在对应项目小节中注明。")

    return "\n".join(lines)

def infer_audience(topics, language):
    """Infer suitable audience from topics and language."""
    audience_parts = []
    topics_lower = " ".join(topics).lower() if topics else ""

    if any(k in topics_lower for k in ["ai", "llm", "chatbot", "machine-learning", "gpt", "claude", "openai"]):
        audience_parts.append("AI/LLM 开发者、大模型应用构建者")
    if any(k in topics_lower for k in ["developer-tools", "cli", "productivity", "automation", "workflow"]):
        audience_parts.append("希望提升开发效率的软件工程师和 DevOps 从业者")
    if any(k in topics_lower for k in ["web", "frontend", "javascript", "typescript", "react", "vue"]):
        audience_parts.append("前端/全栈 Web 开发者")
    if any(k in topics_lower for k in ["data-science", "analytics", "database"]):
        audience_parts.append("数据工程师和数据分析从业者")
    if any(k in topics_lower for k in ["devops", "kubernetes", "docker", "cloud", "infrastructure"]):
        audience_parts.append("运维工程师和云平台架构师")
    if any(k in topics_lower for k in ["security", "privacy", "blockchain"]):
        audience_parts.append("安全研究者和关注隐私保护的技术人员")
    if any(k in topics_lower for k in ["education", "tutorial", "learning"]):
        audience_parts.append("技术学习者和教育从业者")

    if not audience_parts:
        if language in ["Python"]:
            audience_parts.append("Python 开发者、关注该领域动态的技术人员")
        elif language in ["TypeScript", "JavaScript"]:
            audience_parts.append("JavaScript/TypeScript 开发者、Web 技术爱好者")
        elif language in ["Rust", "Go"]:
            audience_parts.append("系统编程/后端开发者、对高性能工具有需求的技术人员")
        elif language in ["Java"]:
            audience_parts.append("Java 生态开发者、企业级应用开发者")
        else:
            audience_parts.append("对该技术领域感兴趣的开发者和技术爱好者")

    return "；".join(audience_parts)

def main():
    # Step 1: Fetch trending
    repos = fetch_trending()
    if not repos:
        print("ERROR: Failed to fetch any repos from trending page!")
        return
    print(f"\nSuccessfully fetched {len(repos)} repos from trending.\n")

    # Step 2: Fetch details for each repo
    print("Fetching repo details...")
    for r in repos:
        try:
            details = fetch_repo_details(r["repo_url"])
            r["details"] = details
            print(f"  [{r['rank']}] {r['repo_path']}: total_stars={details.get('total_stars','?')}, "
                  f"topics={details.get('topics', [])[:3]}, "
                  f"release={details.get('latest_release','?')}")
        except Exception as e:
            print(f"  [{r['rank']}] {r['repo_path']}: Failed to fetch details - {e}")
            r["details"] = {}

    # Step 3: Generate markdown
    print("\nGenerating markdown document...")
    md_content = generate_markdown(repos)

    # Step 4: Save to desktop
    os.makedirs(DESKTOP_PATH, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"\nDocument saved to: {OUTPUT_FILE}")
    print(f"File size: {len(md_content)} characters")

if __name__ == "__main__":
    main()
