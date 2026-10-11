"""网关自己的本地文件：长回复落盘 + 唤醒游标。

为什么长在网关而不是运行时：同一份 `reply.final` 也投给渠道桥（qq-official 把它
当用户消息发出去），渠道要的必须是全文；"上下文被几万字占满"只是 MCP 调用方这一侧
的问题，所以裁剪落在消费侧，别处的出站形状一字不动。

落盘阈值按实测尾部定：ext 会话 655 条 assistant 回复 p50=468 / p90=1508 / p99=5121、
最大 6287 字，>8000 字当前 **0 条**。这是一条保险道，不是常态通道。

唤醒游标也必须落盘：客户端的 `_last_seq`/会话映射是进程内存，网关一重启就归零，
而"你上次问到哪儿"恰恰不能跟着连接一起丢。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPLY_SPILL_MIN_CHARS = 8000
REPLY_SPILL_PREVIEW_CHARS = 600
# 落盘目录是保险不是仓库：同一会话只留最近这么多份，超出按 mtime 删最旧。
REPLY_SPILL_KEEP_FILES = 20
CURSOR_FILENAME = "wake-cursor.json"

_ALLOWED_SEGMENT_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def local_store_root() -> Path:
    raw = str(os.environ.get("NEGI_GATEWAY_DIR") or os.environ.get("G3KU_GATEWAY_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".g3ku" / "mcp-gateway"


def safe_path_segment(value: Any, *, fallback: str) -> str:
    """把外来标识洗成单个安全文件名片段。

    路径里不出现调用方可控的输入：`session_id` 由服务端裁决后返回、`turn_id` 由服务端
    生成，但两者仍按不可信处理——`.` 在白名单里，所以 `..` 会原样穿过字符过滤，
    必须单独塌掉。
    """
    cleaned = "".join(
        char if char in _ALLOWED_SEGMENT_CHARS else "_" for char in str(value or "").strip()
    )
    if ".." in cleaned or cleaned.strip(".") == "":
        cleaned = cleaned.replace(".", "_")
    cleaned = cleaned.strip("_.")
    return (cleaned or fallback)[:80]


def _prune(directory: Path) -> None:
    try:
        files = sorted(
            (item for item in directory.glob("*.md") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[REPLY_SPILL_KEEP_FILES:]:
        try:
            stale.unlink()
        except OSError:
            continue


def spill_reply(session_id: str, turn_id: str, text: str) -> dict[str, Any]:
    """命中阈值时返回预览 + 文件路径；没命中或写失败都回全文。

    写失败不许吃掉答案：磁盘满/只读目录时原样带回 `reply`，只多一个
    `reply_spill_error`——调用方宁可被长文本占上下文，也不能拿不到回复。
    """
    body = str(text or "")
    if len(body) < REPLY_SPILL_MIN_CHARS:
        return {"reply": body}
    directory = local_store_root() / "replies" / safe_path_segment(session_id, fallback="session")
    filename = f"{safe_path_segment(turn_id, fallback='turn')}.md"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_text(body, encoding="utf-8")
        _prune(directory)
    except OSError as exc:
        return {"reply": body, "reply_spill_error": str(exc)}
    preview = body[:REPLY_SPILL_PREVIEW_CHARS]
    return {
        "reply": f"{preview}…（共 {len(body)} 字，全文：{path}）",
        "reply_spilled": True,
        "reply_file": str(path),
        "reply_chars": len(body),
    }


def _cursor_path() -> Path:
    return local_store_root() / CURSOR_FILENAME


def read_wake_state(session_id: str) -> tuple[str, list[str]]:
    """返回 `(游标, 已在该游标秒报过的身份)`。读不到就是没有历史：("", [])。"""
    try:
        data = json.loads(_cursor_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", []
    if not isinstance(data, dict):
        return "", []
    value = data.get(str(session_id or "").strip())
    if isinstance(value, str):
        # 旧形状只有游标串。这是一份本地缓存不是合同，读出形状要能容下它自己以前写过的样子。
        return value, []
    if not isinstance(value, dict):
        return "", []
    return str(value.get("since") or ""), [str(i) for i in list(value.get("reported") or [])]


def write_wake_state(session_id: str, since: str, reported: list[str]) -> None:
    """尽力而为：游标写不进磁盘只意味着下次从"没有历史"重新开始，不许弄失败这次唤醒。"""
    key = str(session_id or "").strip()
    value = str(since or "").strip()
    if not key or not value:
        return
    path = _cursor_path()
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    data[key] = {"since": value, "reported": [str(i) for i in list(reported or [])][:200]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError:
        return
