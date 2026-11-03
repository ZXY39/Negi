"""模型面内容审查词替换。

词表由操作员手动维护在 `data_root()/.g3ku/content-policy.json`，默认关闭。
开启后只改发往 provider 的 `role=tool` 消息；原始工具工件、转录与旧请求工件保持原文。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any, Iterable

from g3ku.deployment.data_root import data_g3ku_path

POLICY_FILENAME = "content-policy.json"
POLICY_VERSION = 1

REPLACEMENT_MARKER = "***命中违规内容已被Negi替换***"
NOTICE_FIELD = "_content_policy_notice"
NOTICE_PREFIX = "[CONTENT_POLICY_NOTICE]"

_NOTICE_TEMPLATE = (
    NOTICE_PREFIX
    + " 本次工具结果出现了违规词，已替换 {hits} 处；"
    "后续需要避免再次从同一来源（相同 URL/文件/查询）获取，否则会出错停止。"
)

# marker 与提示本身也在下一次请求的扫描范围内；若词表恰好包含其中的词，
# 必须跳过这些片段，否则掩码文本会被逐轮改写。
_PROTECTED_RE = re.compile(
    r"(\*\*\*命中违规内容已被Negi替换\*\*\*|\[CONTENT_POLICY_NOTICE\][^\n]*)"
)

_MASK_CACHE_MAX_ENTRIES = 512
_MASK_CACHE_MAX_BYTES = 16 * 1024 * 1024

_POLICY_LOCK = threading.RLock()
_POLICY_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_MASK_CACHE: "OrderedDict[tuple[str, str], tuple[str, int, str, int]]" = OrderedDict()
_MASK_CACHE_BYTES = 0


def policy_path():
    return data_g3ku_path(POLICY_FILENAME)


def _default_policy() -> dict[str, Any]:
    return {"version": POLICY_VERSION, "enabled": False, "words": [], "updated_at": ""}


def _copy_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": int(policy.get("version") or POLICY_VERSION),
        "enabled": bool(policy.get("enabled")),
        "words": [str(item) for item in list(policy.get("words") or [])],
        "updated_at": str(policy.get("updated_at") or ""),
    }


def _normalize_word_items(items: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        word = item.replace("\ufeff", "").strip()
        if not word:
            continue
        folded = word.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(word)
    return result


def normalize_words(text: str) -> list[str]:
    """按行切分词表：去 BOM、trim、忽略空行、按 casefold 去重，不按逗号切分。"""
    if not isinstance(text, str):
        return []
    return _normalize_word_items(text.replace("\ufeff", "").splitlines())


def format_words(words: Iterable[str]) -> str:
    normalized = _normalize_word_items(list(words or []))
    return "\n".join(normalized) + ("\n" if normalized else "")


def _read_policy(path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _default_policy()
    if not isinstance(data, dict):
        return _default_policy()
    enabled = data.get("enabled")
    words = data.get("words")
    return {
        "version": int(data.get("version") or POLICY_VERSION),
        "enabled": enabled if isinstance(enabled, bool) else False,
        "words": _normalize_word_items(words if isinstance(words, list) else []),
        "updated_at": str(data.get("updated_at") or ""),
    }


def load_policy(*, force: bool = False) -> dict[str, Any]:
    path = policy_path()
    try:
        stat = path.stat()
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        stamp = (0, 0)
    key = str(path)
    with _POLICY_LOCK:
        cached = _POLICY_CACHE.get(key)
        if cached is not None and not force and cached[0] == stamp[0] and cached[1] == stamp[1]:
            return _copy_policy(cached[2])
    policy = _read_policy(path) if stamp != (0, 0) else _default_policy()
    with _POLICY_LOCK:
        _POLICY_CACHE[key] = (stamp[0], stamp[1], policy)
    return _copy_policy(policy)


def save_policy(enabled: bool, words: Iterable[str]) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if isinstance(words, (str, bytes)) or words is None:
        raise ValueError("words must be a list of strings")
    normalized = _normalize_word_items(list(words))
    policy = {
        "version": POLICY_VERSION,
        "enabled": enabled,
        "words": normalized,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        stat = path.stat()
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        stamp = (0, 0)
    with _POLICY_LOCK:
        _POLICY_CACHE[str(path)] = (stamp[0], stamp[1], dict(policy))
    return _copy_policy(policy)


def policy_revision(policy: dict[str, Any] | None = None) -> str:
    current = policy if isinstance(policy, dict) else load_policy()
    words = sorted(str(item).casefold() for item in list(current.get("words") or []))
    digest = hashlib.sha1("\n".join(words).encode("utf-8", "surrogatepass"))
    digest.update(b"\x01" if current.get("enabled") else b"\x00")
    return digest.hexdigest()


def compile_matcher(words: Iterable[str]) -> re.Pattern[str] | None:
    normalized = _normalize_word_items(list(words or []))
    if not normalized:
        return None
    # 长词优先：re 取最左的备选，短词排前面会把长词吃成半截。
    normalized.sort(key=lambda item: (-len(item), item.casefold()))
    pattern = "|".join(re.escape(word) for word in normalized)
    return re.compile(pattern, re.IGNORECASE)


def _marker_replacement(_match: re.Match[str]) -> str:
    return REPLACEMENT_MARKER


def replace_text(text: str, matcher: re.Pattern[str] | None) -> tuple[str, int]:
    if matcher is None or not text:
        return text, 0
    parts = _PROTECTED_RE.split(text)
    hits = 0
    for index, part in enumerate(parts):
        if index % 2 == 1 or not part:
            continue
        replaced, count = matcher.subn(_marker_replacement, part)
        if count:
            parts[index] = replaced
            hits += count
    if hits <= 0:
        return text, 0
    return "".join(parts), hits


def _replace_json_value(value: Any, matcher: re.Pattern[str], counter: list[int]) -> Any:
    if isinstance(value, str):
        replaced, hits = replace_text(value, matcher)
        counter[0] += hits
        return replaced
    if isinstance(value, list):
        return [_replace_json_value(item, matcher, counter) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                item
                if key == NOTICE_FIELD
                else _replace_json_value(item, matcher, counter)
            )
            for key, item in value.items()
        }
    return value


def _mask_content(content: str, matcher: re.Pattern[str]) -> tuple[str, int, str]:
    """返回 (掩码后的原文, 替换次数, 顶层形态)。提示不在这一步插入。"""
    stripped = content.lstrip()
    if not stripped or stripped[:1] not in {"{", "["}:
        replaced, hits = replace_text(content, matcher)
        return replaced, hits, "other"
    try:
        parsed = json.loads(content)
    except ValueError:
        replaced, hits = replace_text(content, matcher)
        return replaced, hits, "other"
    if isinstance(parsed, dict):
        counter = [0]
        replaced = _replace_json_value(parsed, matcher, counter)
        if counter[0] <= 0:
            return content, 0, "dict"
        return json.dumps(replaced, ensure_ascii=False), counter[0], "dict"
    replaced, hits = replace_text(content, matcher)
    if hits <= 0:
        return content, 0, "other"
    try:
        reparsed = json.loads(replaced)
    except ValueError:
        return replaced, hits, "other"
    return json.dumps(reparsed, ensure_ascii=False), hits, "other"


def _mask_content_cached(
    content: str,
    matcher: re.Pattern[str],
    revision: str,
) -> tuple[str, int, str]:
    global _MASK_CACHE_BYTES
    key = (hashlib.sha1(content.encode("utf-8", "surrogatepass")).hexdigest(), revision)
    with _POLICY_LOCK:
        cached = _MASK_CACHE.get(key)
        if cached is not None:
            _MASK_CACHE.move_to_end(key)
            return cached[0], cached[1], cached[2]
    result = _mask_content(content, matcher)
    cost = len(content) + len(result[0])
    with _POLICY_LOCK:
        if key not in _MASK_CACHE:
            _MASK_CACHE[key] = (*result, cost)
            _MASK_CACHE_BYTES += cost
            while (
                len(_MASK_CACHE) > _MASK_CACHE_MAX_ENTRIES
                or _MASK_CACHE_BYTES > _MASK_CACHE_MAX_BYTES
            ):
                _old_key, old_value = _MASK_CACHE.popitem(last=False)
                _MASK_CACHE_BYTES -= old_value[3]
    return result


def _notice_text(hits: int) -> str:
    return _NOTICE_TEMPLATE.format(hits=int(hits))


def replace_content(
    content: str,
    matcher: re.Pattern[str] | None,
    *,
    notice: bool,
    revision: str = "",
) -> tuple[str, int]:
    if matcher is None or not content:
        return content, 0
    revision = revision or policy_revision()
    masked, hits, kind = _mask_content_cached(content, matcher, revision)
    if hits <= 0:
        return content, 0
    text = _notice_text(hits)
    if not notice:
        return masked, hits
    if kind == "dict":
        try:
            parsed = json.loads(masked)
        except ValueError:
            return masked, hits
        if isinstance(parsed, dict) and NOTICE_FIELD not in parsed:
            parsed = {NOTICE_FIELD: text, **parsed}
            return json.dumps(parsed, ensure_ascii=False), hits
        return masked, hits
    if NOTICE_PREFIX in content:
        return masked, hits
    return f"{text}\n{masked}", hits


def _current_hop_tool_indices(messages: list[Any]) -> set[int]:
    """当前 hop = 最后一条 assistant 之后的工具结果。

    不能只看"末尾连续 tool"：前门每跳会在工具结果之后追加一条 `[RUNTIME_NOTICE]` 用户
    消息，节点道也会追加中插通知，尾部因此不再是工具段；按"最后一次 assistant 之后"
    取，结果与模型看到的这一跳一致。没有任何 assistant 时退回末尾连续 tool。
    """
    last_assistant = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "assistant":
            last_assistant = index
    indices = {
        index
        for index, message in enumerate(messages)
        if index > last_assistant
        and isinstance(message, dict)
        and str(message.get("role") or "").strip().lower() == "tool"
    }
    if indices:
        return indices
    tail: set[int] = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "tool":
            tail.add(index)
            continue
        break
    return tail


def apply_to_tool_messages(
    messages: list[Any],
    *,
    notice_indices: Iterable[int] | None = None,
) -> list[Any]:
    """替换 `role=tool` 消息里的词表命中；末段连续 tool 消息（当前 hop）才加提示。"""
    if not isinstance(messages, list):
        return messages
    policy = load_policy()
    if not policy.get("enabled"):
        return list(messages)
    matcher = compile_matcher(policy.get("words") or [])
    if matcher is None:
        return list(messages)
    revision = policy_revision(policy)
    if notice_indices is None:
        indexed = _current_hop_tool_indices(messages)
    else:
        indexed = {int(item) for item in notice_indices}
    result: list[Any] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            result.append(message)
            continue
        if str(message.get("role") or "").strip().lower() != "tool":
            result.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, str):
            result.append(message)
            continue
        replaced, hits = replace_content(
            content,
            matcher,
            notice=index in indexed,
            revision=revision,
        )
        if hits <= 0:
            result.append(message)
            continue
        result.append({**message, "content": replaced})
    return result
