"""Session-link lane constants shared by the delivery side and the heartbeat scanner.

车道本体：关系/别名/信箱存储住 SQLiteTaskStore，投递住 MainRuntimeService，
唤醒住 g3ku/heartbeat 的 SessionMessageScanner。这里只放两侧共用的判据常数。
"""

from __future__ import annotations

# 一条 reply_to 续链最多唤醒这么多跳；到顶的来信落箱不唤醒（随下一次别的唤醒并入），
# 续链要收件人显式发新消息（不带 reply_to 开新链）。结构上封死无限互醒。
SESSION_MESSAGE_HOP_CAP = 8

__all__ = ['SESSION_MESSAGE_HOP_CAP']
