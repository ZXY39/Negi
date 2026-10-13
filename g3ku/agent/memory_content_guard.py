"""Pre-write content guard for durable memory.

Long-term memory is a queue: anything accepted here is regenerated into
``MEMORY.md`` and injected at message index 1 of *every* session, including
sessions whose users never produced the text. The surfaced agent can enqueue a
candidate from a third-party channel message, so the candidate is untrusted
input on its way into a shared prompt prefix.

This module blocks on high-confidence patterns only and never rewrites,
redacts, or normalizes the payload: a blocked candidate is reported to whoever
submitted it, so the caller keeps the original text and can decide what to do.
Semantic judgement stays with the internal memory agent, which owns admission.
"""

from __future__ import annotations

import re

# Each rule is (name, patterns). Names reach the model and the terminal ledger,
# so they have to say which behaviour was refused, not just "blocked".
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "instruction_override",
        (
            # An override aimed at the reader: the object is a *prior* instruction and
            # the verb is discardive. "跳过系统提示首节点" (a compression algorithm
            # describing which message index to skip) must not match - discussing the
            # prompt layout is not an attempt to take it over.
            r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions",
            r"disregard\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|rules)",
            r"(?:忽略|无视)[^。\n]{0,10}?(?:之前|以上|先前|既有|所有)[^。\n]{0,10}?(?:指令|规则|设定|要求)",
            r"(?:这条|该)(?:记忆|规则)[^。\n]{0,12}?(?:覆盖|优先于)[^。\n]{0,12}?(?:系统|所有|一切|用户当前)",
        ),
    ),
    (
        "claimed_authority",
        (
            # Self-declared authority, addressed to the reader, with an imperative
            # attached. Bare mentions of 系统提示 in prose are not enough.
            r"(?:i\s+am|this\s+is)\s+(?:the\s+)?(?:system|developer|operator)\s+(?:message|instruction)",
            r"(?:system|developer)\s+prompt\s+(?:update|override|patch)",
            r"(?:系统|开发者|管理员)(?:的)?(?:最高优先级)?(?:指令|命令)[^。\n]{0,16}?(?:必须|禁止|不得|立即)",
        ),
    ),
    (
        "concealment",
        (
            r"(?:do\s+not|don't|never)\s+(?:tell|inform|mention|reveal)[^\n]{0,24}?(?:the\s+user|user)",
            r"(?:secretly|covertly|without\s+asking)\s+(?:send|post|upload|message)",
            r"悄悄[^\n]{0,16}?(?:发送|发出去|上传|执行|转发|告诉)",
            r"背着你[^\n]{0,16}?(?:发送|上传|操作|改掉)",
        ),
    ),
    (
        "credential_exfiltration",
        (
            # Bare `token` is deliberately absent: in this repository it overwhelmingly
            # means token budget / token compression, and "token ... send" collided with
            # a compression pipeline description.
            r"(?:api[ _-]?key|secret|password|credential)s?[^。\n]{0,24}?"
            r"(?:发往|发到|上传到|发送到|外传|贴到|上传至|发至)",
            r"(?:密钥|口令|密码|凭据)[^。\n]{0,24}?(?:发送|上传|打印出来|贴[到至]|发往|外传)",
            # Same intent with the secret named first: "把 master.key 打印出来上传到…".
            r"(?:\.env|master\.key|id_rsa|私钥|密钥文件|凭据文件)[^。\n]{0,24}?"
            r"(?:打印|上传|贴[到至]|发往|发到|外传|发送|git push)",
            r"(?:read|cat|dump|print|open)\s+[^\n]{0,24}(?:\.env\b|master\.key|id_rsa|credentials?\.(?:json|txt|py))",
            r"(?:读取|读出|打印|导出)[^。\n]{0,20}?(?:\.env|master\.key|私钥|密钥文件)",
        ),
    ),
    (
        "third_party_action",
        (
            r"(?:mass|bulk)\s+(?:send|message|dm|email)s?",
            r"(?:群发|批量发送|自动向[^\n]{0,16}?(?:所有人|好友|群|成员)[^\n]{0,16}?(?:发送|私信|加好友))",
            r"(?:delete|drop|truncate)\s+(?:the\s+)?(?:database|whole\s+table|all\s+sessions)",
            r"(?:删库|清空数据库|删掉所有会话|清空全部记忆库)",
        ),
    ),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, patterns in _RULES
    for pattern in patterns
)


def scan_memory_candidate(text: str) -> tuple[bool, str]:
    """Return ``(blocked, rule)`` for one candidate memory payload.

    ``rule`` is empty when the candidate is allowed. A pass means "no known
    override/exfiltration shape", not "this memory is safe or true" - admission
    is still the memory agent's decision.
    """
    value = str(text or "")
    if not value.strip():
        return False, ""
    for name, pattern in _COMPILED:
        if pattern.search(value):
            return True, name
    return False, ""
