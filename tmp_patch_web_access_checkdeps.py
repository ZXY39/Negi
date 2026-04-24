from pathlib import Path

skill = Path("skills/web-access/SKILL.md")
text = skill.read_text(encoding="utf-8")
extra = """- `check-deps.mjs` 若在短时限内超时，不要立刻把它等同于“skill 不可用”；先区分“脚本慢/卡住”和“CDP 不可用”。至少补做快速验证：Chrome 是否开启 remote debugging、9222 端口是否监听、proxy 是否已启动、是否能访问其健康接口/targets。若这些快速验证通过，可继续排查脚本耗时来源；若关键项不通过，再判定为前置依赖未满足。
- 当任务强制要求依赖 web-access/CDP 时，`check-deps.mjs` 超时本身就是高风险信号。除非后续用其他等价验证手段明确确认 Chrome remote-debugging、CDP proxy 与基础页面连接都正常，否则不要臆测继续产出结果。
- Chrome 侧的关键排障点要明确写入：在 `chrome://inspect/#remote-debugging` 中启用 **Allow remote debugging for this browser instance**，必要时重启 Chrome；若任务失败原因是 CDP 可用性未验证，应优先提示这一项。
- 建议把“快速复核”作为 check-deps 超时后的固定补救动作：检查 Node 可用、9222 端口、proxy 存活、是否能列出 targets；仅确认脚本文件存在不算验证通过。

"""
anchor = "- 批量任务务必加节流：对多关键词轮询请求时，建议在每次请求/关键词切换之间增加 1~3 秒等待，并内置失败重试，降低间歇性错误与触发风控的概率。\n\n"
if "check-deps.mjs` 若在短时限内超时" not in text:
    text = text.replace(anchor, anchor + extra, 1)
    skill.write_text(text, encoding="utf-8")

ref = Path("skills/web-access/references/site-patterns/google.com.md")
if ref.exists():
    g = ref.read_text(encoding="utf-8")
    add = """- 若前置 `check-deps.mjs` 超时，不要只看脚本文件是否存在；至少补查 Chrome remote debugging 开关、9222 端口、proxy 健康状态与 targets 列表。
- 若这些快速复核项不能明确通过，就应把失败归因为 CDP 可用性未可靠验证，而不是继续假定浏览器链路可用。
"""
    marker = "## 已知陷阱\n"
    if "check-deps.mjs` 超时" not in g:
        g = g.replace(marker, marker + add, 1)
        ref.write_text(g, encoding="utf-8")
print("patched")
