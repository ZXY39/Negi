"""Node 宿主 cron 隐藏契约的 Python 镜像与剥离工具。

Node 宿主（subsystems/china_channels_host/src/shared/cron/index.ts）曾在入站
消息命中定时意图时，把 ``CRON_HIDDEN_PROMPT`` 整块追加到 ``CommandBody``，导致该
契约被引擎当作**用户消息内容**持久化并显示在会话里（影响美观、污染上下文、且
破坏 frontdoor「当前用户精确匹配」，造成答非所问）。

这里提供与 TS 侧一致的契约文本与剥离函数，供引擎在持久化/显示/匹配前把隐藏
契约从用户内容中剥除。cron 的正确使用规范改为放进 cron 工具的 toolskill 按需
加载（见 tools/cron/toolskills/SKILL.md），不再逐条注入用户消息。
"""

from __future__ import annotations

# 与 Node 宿主 CRON_HIDDEN_PROMPT 对齐的起始标记。剥离以「首次出现」为准，
# 容忍宿主侧文本的细微差异（版本漂移）。
CRON_HIDDEN_PROMPT_MARKER = "When creating or updating a cron task,"

CRON_HIDDEN_PROMPT = """
When creating or updating a cron task, always store a fixed delivery target in the job itself.
- Use the built-in cron tool (action=add/update). Do not run shell commands.
- Must use sessionTarget="isolated" for reminder jobs.
- payload.kind="agentTurn"
- payload.message must be plain user-visible reminder text only.
- You must encode runtime guardrails directly into payload.message so the cron run can follow them without extra context.
- Runtime guardrails to encode in payload.message:
  - return plain text only
  - never call any tool
  - never call the message tool
  - never send manually; delivery is handled by cron delivery settings
- Do not include tool directives, "NO_REPLY", or heartbeat markers in payload.message.
- Job name is never a message target.
- During cron run, must return plain text only and never call the message tool.
- Use top-level delivery with announce mode:
  delivery.mode="announce"
  delivery.channel=<OriginatingChannel> (example: "qqbot")
  delivery.to=<OriginatingTo> (examples: "user:<openid>" / "group:<group_openid>")
  delivery.accountId=<AccountId> when available
- Never set delivery.channel="last" for multi-channel environments.
- If OriginatingChannel/OriginatingTo are unavailable, ask a concise follow-up for channel and target.
- Do not call the message tool to send"""


def split_cron_hidden_prompt(text: str) -> tuple[str, str]:
    """把用户内容拆成 (base, hidden_prompt)。未含契约时 hidden 为空。"""
    raw = str(text or "")
    idx = raw.find(CRON_HIDDEN_PROMPT_MARKER)
    if idx == -1:
        return raw, ""
    base = raw[:idx].rstrip()
    return base, raw[idx:].strip()


def strip_cron_hidden_prompt(text: str) -> str:
    """剥离隐藏契约，仅保留用户原始内容。"""
    base, _hidden = split_cron_hidden_prompt(text)
    return base
