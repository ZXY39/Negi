"""External bridge session registry.

Authoritative mapping between bridge-supplied ``external_key`` identifiers and
g3ku runtime session keys (``ext:{bridge}:{hash}``) for sessions driven
through the External Agent API. Transcripts themselves are owned by the shared
``SessionManager`` (``sessions/<safe_key>.jsonl``); this registry only owns
identity mapping plus small display metadata, persisted atomically at
``.g3ku/external-sessions/registry.json``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.bus.events import OutboundMessage
from g3ku.config.live_runtime import get_runtime_config
from g3ku.runtime.session_keys import build_external_session_key, normalize_bridge_id
from g3ku.utils.helpers import ensure_dir

REGISTRY_DIRNAME = Path(".g3ku") / "external-sessions"
REGISTRY_FILENAME = "registry.json"
# Outbound bus channel value for messages addressed to external bridge
# sessions; the web shell drain resolves the target via this registry.
EXTERNAL_OUTBOUND_CHANNEL = "ext"


@dataclass(slots=True)
class ExternalSessionEntry:
    bridge_id: str
    external_key: str
    session_key: str
    created_at: str
    title: str = ""
    hidden: bool = False
    # 入站消息自报的聊天形状（``group`` / ``dm`` / ``guild`` / ``guild_dm``）。
    # 存量条目不迁移：空值由 ``external_key`` 前缀回退解析。
    chat_type: str = ""
    # 该会话历史上见过的最受限信任级（见 ``g3ku.runtime.session_trust``）。
    # 空值 = 还没见过入站消息，读取侧按第三方兜底。
    trust: str = ""
    # 最近一次主动推送（heartbeat / cron / 任务通知 / @ 工具）的发布时刻。
    # 只由 outbound drain 写；普通回合回复不更新。cron 的 60 秒节流读它。
    last_proactive_at: str = ""
    # 最近一次入站消息时刻：mention 工具用它判断被动回复窗口是否还开着。
    last_inbound_at: str = ""
    # 平台能力读数：主动消息被拒后记 denied/quota；主动形状送达成功后清空。
    active_message_state: str = ""
    active_message_denied_at: str = ""


def _registry_workspace() -> Path:
    try:
        config = get_runtime_config(force=False)[0]
        workspace = getattr(config, "workspace_path", None)
        if workspace:
            return Path(workspace).resolve()
    except Exception:
        pass
    return Path.cwd().resolve()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    temp_path.replace(path)


class ExternalSessionRegistry:
    def __init__(self, workspace: Path | None = None):
        self.workspace = (workspace or _registry_workspace()).resolve()
        self.path = ensure_dir(self.workspace / REGISTRY_DIRNAME) / REGISTRY_FILENAME
        self._lock = threading.RLock()
        self._entries: dict[str, ExternalSessionEntry] = {}
        self._index: dict[tuple[str, str], str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, dict):
            return
        for session_key, item in sessions.items():
            if not isinstance(item, dict):
                continue
            entry = ExternalSessionEntry(
                bridge_id=normalize_bridge_id(item.get("bridge_id")),
                external_key=str(item.get("external_key") or ""),
                session_key=str(session_key),
                created_at=str(item.get("created_at") or ""),
                title=str(item.get("title") or ""),
                hidden=bool(item.get("hidden")),
                chat_type=str(item.get("chat_type") or ""),
                trust=str(item.get("trust") or ""),
                last_proactive_at=str(item.get("last_proactive_at") or ""),
                last_inbound_at=str(item.get("last_inbound_at") or ""),
                active_message_state=str(item.get("active_message_state") or ""),
                active_message_denied_at=str(item.get("active_message_denied_at") or ""),
            )
            self._entries[entry.session_key] = entry
            self._index[(entry.bridge_id, entry.external_key)] = entry.session_key

    def _save(self) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(),
            "sessions": {
                session_key: {
                    "bridge_id": entry.bridge_id,
                    "external_key": entry.external_key,
                    "created_at": entry.created_at,
                    "title": entry.title,
                    "hidden": bool(entry.hidden),
                    "chat_type": entry.chat_type,
                    "trust": entry.trust,
                    "last_proactive_at": entry.last_proactive_at,
                    "last_inbound_at": entry.last_inbound_at,
                    "active_message_state": entry.active_message_state,
                    "active_message_denied_at": entry.active_message_denied_at,
                }
                for session_key, entry in self._entries.items()
            },
        }
        _atomic_write_json(self.path, payload)

    def resolve_or_create(
        self,
        *,
        bridge_id: str,
        external_key: str,
        title: str | None = None,
        chat_type: str | None = None,
    ) -> tuple[ExternalSessionEntry, bool]:
        """Idempotent get-or-create. Returns ``(entry, created)``."""
        from g3ku.runtime.session_trust import is_multi_party_chat, normalize_chat_type

        bridge = normalize_bridge_id(bridge_id)
        key = str(external_key or "").strip()
        if not key:
            raise ValueError("external_key is required")
        title_text = str(title or "").strip()
        shape = normalize_chat_type(chat_type)
        with self._lock:
            existing_key = self._index.get((bridge, key))
            if existing_key is not None:
                entry = self._entries[existing_key]
                changed = False
                if title_text and entry.title != title_text:
                    entry.title = title_text
                    changed = True
                if shape and entry.chat_type != shape:
                    # 形状冲突按更严的一档记：先报 dm 再报 group 的桥不能让会话留在 dm。
                    if not entry.chat_type or is_multi_party_chat(chat_type=shape):
                        entry.chat_type = shape
                        changed = True
                if entry.hidden:
                    # 入站即"这个渠道还在跟我说话"：被操作员从网页隐身的会话要当场回来。
                    entry.hidden = False
                    changed = True
                if changed:
                    self._save()
                return entry, False

            digest_length = 16
            session_key = build_external_session_key(
                bridge_id=bridge, external_key=key, digest_length=digest_length
            )
            while session_key in self._entries:
                digest_length += 8
                # 上限 24 而不是更长的hex：会话键直接进文件路径，而 Windows 裸路径
                # 在 260 处失败（实测本 venv python）。键每长一位，
                # .g3ku/web-ceo-requests/<键>/… 这类深目录就少一位余量。
                if digest_length > 24:
                    raise RuntimeError("external session key space exhausted")
                session_key = build_external_session_key(
                    bridge_id=bridge, external_key=key, digest_length=digest_length
                )

            entry = ExternalSessionEntry(
                bridge_id=bridge,
                external_key=key,
                session_key=session_key,
                created_at=datetime.now().isoformat(),
                title=title_text,
                chat_type=shape,
            )
            self._entries[session_key] = entry
            self._index[(bridge, key)] = session_key
            self._save()
            return entry, True

    def forget(self, session_key: str) -> ExternalSessionEntry | None:
        """Retire the identity mapping so the same external_key starts fresh.

        调用方必须先证明没有东西还指着这个会话键（未 ack 的出站账本行、指向它的
        定时任务）：`find_by_any_key` 对失联目标只 WARNING 后取最新，静默摘除会把
        推送送走。闸门在 `ceo_sessions._identity_forget_blocker`。
        """
        raw = str(session_key or "").strip()
        with self._lock:
            entry = self._entries.pop(raw, None)
            if entry is None:
                return None
            if self._index.get((entry.bridge_id, entry.external_key)) == raw:
                self._index.pop((entry.bridge_id, entry.external_key), None)
            self._save()
            return entry

    def set_hidden(self, session_key: str, hidden: bool) -> ExternalSessionEntry | None:
        """把会话从网页渠道列表隐身/恢复，身份映射照旧保留。

        隐藏是给「渠道 token 还在」的那条删除车道用的：转录按既定清历史契约删掉，
        但 `external_key → session_key` 不动，所以桥下次发消息仍落回同一个会话，
        `resolve_or_create` 会顺手把标记清掉。注销身份是另一条道（`forget`）。
        """
        raw = str(session_key or "").strip()
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                return None
            if entry.hidden is bool(hidden):
                return entry
            entry.hidden = bool(hidden)
            self._save()
            return entry

    def get_by_session_key(self, session_key: str | None) -> ExternalSessionEntry | None:
        raw = str(session_key or "").strip()
        if not raw:
            return None
        with self._lock:
            return self._entries.get(raw)

    def note_inbound(
        self,
        session_key: str,
        *,
        chat_type: str = "",
        trust: str = "",
    ) -> ExternalSessionEntry | None:
        """记下一条入站消息的形状与信任级（粘性，只降不升）。

        只在真的变化时落盘：稳态下每条入站消息就是一次 dict 查找。信任级由
        `g3ku.runtime.session_trust` 判定，这里只负责持久化"见过的最受限那一档"——
        一条声称是操作员的后继消息不能把先前的第三方判定抬回去。
        """
        from g3ku.runtime.session_trust import more_restrictive, normalize_chat_type

        raw = str(session_key or "").strip()
        shape = normalize_chat_type(chat_type)
        incoming = str(trust or "").strip()
        if not raw:
            return None
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                return None
            changed = False
            if shape and entry.chat_type != shape:
                entry.chat_type = shape
                changed = True
            if incoming:
                merged = more_restrictive(entry.trust, incoming)
                if entry.trust != merged:
                    entry.trust = merged
                    changed = True
            # 最近入站时刻：mention 的被动窗口判据；每条入站都刷新，重启后仍可读。
            entry.last_inbound_at = datetime.now().isoformat()
            changed = True
            if changed:
                self._save()
            return entry

    def trust_owner(self, session_key: str) -> ExternalSessionEntry | None:
        """口令门控的注册动作：把一条 DM 会话的粘性信任显式抬到 owner。

        入站消息仍只降不升（走 `note_inbound`）；只有管理面的注册入口在验过项目
        解锁口令后调用这里。群聊一律拒绝：多人会话按第三方整会话处理。
        """
        from g3ku.runtime.session_trust import TRUST_OWNER, is_multi_party_chat

        raw = str(session_key or "").strip()
        if not raw:
            return None
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                return None
            if is_multi_party_chat(
                chat_type=getattr(entry, "chat_type", ""),
                external_key=getattr(entry, "external_key", ""),
            ):
                return None
            if entry.trust != TRUST_OWNER:
                entry.trust = TRUST_OWNER
                self._save()
            return entry

    def note_proactive_outbound(
        self,
        session_key: str,
        *,
        at: str | None = None,
    ) -> ExternalSessionEntry | None:
        """Record one proactive push for the 60s throttle.

        Only the outbound drain calls this, and only for `outbound.created`
        notices (heartbeat / cron / task terminal / mention). Ordinary turn
        replies are not proactive and must not reset the window.
        """
        raw = str(session_key or "").strip()
        if not raw:
            return None
        stamp = str(at or datetime.now().isoformat()).strip()
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                return None
            entry.last_proactive_at = stamp
            self._save()
            return entry

    def last_proactive_at(self, session_key: str) -> str:
        raw = str(session_key or "").strip()
        if not raw:
            return ""
        with self._lock:
            entry = self._entries.get(raw)
            return str(getattr(entry, "last_proactive_at", "") or "") if entry is not None else ""

    def note_active_message_state(self, session_key: str, state: str) -> ExternalSessionEntry | None:
        """Record the platform's active-message capability reading (`denied` / `quota` / "")."""
        raw = str(session_key or "").strip()
        normalized = str(state or "").strip().lower()
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                return None
            changed = entry.active_message_state != normalized
            entry.active_message_state = normalized
            if normalized in {"denied", "quota"}:
                entry.active_message_denied_at = datetime.now().isoformat()
            elif not normalized:
                entry.active_message_denied_at = ""
            if changed:
                self._save()
            return entry

    def active_message_state(self, session_key: str) -> str:
        raw = str(session_key or "").strip()
        if not raw:
            return ""
        with self._lock:
            entry = self._entries.get(raw)
            return str(getattr(entry, "active_message_state", "") or "") if entry is not None else ""

    def last_inbound_at(self, session_key: str) -> str:
        raw = str(session_key or "").strip()
        if not raw:
            return ""
        with self._lock:
            entry = self._entries.get(raw)
            return str(getattr(entry, "last_inbound_at", "") or "") if entry is not None else ""

    def session_count(self) -> int:
        """当前加载进来的会话条目数。

        空注册表与「注册表文件没读到」（0 字节、满盘截断）在磁盘上不可区分，
        而账本的不可达判定要把这两种情况分开：误判会把还在等人取的回复清掉。
        """
        with self._lock:
            return len(self._entries)

    def get_session_key(self, *, bridge_id: str, external_key: str) -> str | None:
        bridge = normalize_bridge_id(bridge_id)
        key = str(external_key or "").strip()
        with self._lock:
            return self._index.get((bridge, key))

    def find_by_any_key(self, value: str | None) -> ExternalSessionEntry | None:
        """Resolve an outbound target that may be either a session_key or a
        registered external_key (any bridge).

        external_key 不是全局唯一：同一自然人在两个 QQ 号下是两个会话，而 bridge_id
        改名（如单号 → `qq-official-<appId>`）会给同一个 external_key 留下新旧两条
        目。按插入序静默取第一条会把推送发给可能已无桥消费的旧会话，所以多命中时取
        `created_at` 最新的一条并留下 WARNING——WARNING 就是"该把这条改指新键"的信号。
        """
        raw = str(value or "").strip()
        if not raw:
            return None
        with self._lock:
            entry = self._entries.get(raw)
            if entry is not None:
                return entry
            candidates = [item for item in self._entries.values() if item.external_key == raw]
        if not candidates:
            return None
        if len(candidates) > 1:
            candidates.sort(key=lambda item: (str(item.created_at or ""), item.session_key))
            chosen = candidates[-1]
            logger.warning(
                "external_key {} resolves to {} sessions; routing to newest {} — 把直连该目标的"
                "推送/定时任务改指新会话键，否则旧会话会静默收不到",
                raw,
                len(candidates),
                chosen.session_key,
            )
            return chosen
        return candidates[0]

    def list_bridge_sessions(self, bridge_id: str) -> list[ExternalSessionEntry]:
        bridge = normalize_bridge_id(bridge_id)
        with self._lock:
            return [entry for entry in self._entries.values() if entry.bridge_id == bridge]

    def list_entries(self) -> list[ExternalSessionEntry]:
        with self._lock:
            return list(self._entries.values())

    def update_title(self, session_key: str, title: str) -> bool:
        raw = str(session_key or "").strip()
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                return False
            entry.title = str(title or "").strip()
            self._save()
            return True


_REGISTRY: ExternalSessionRegistry | None = None
_REGISTRY_LOCK = threading.RLock()


def get_external_session_registry(workspace: Path | None = None) -> ExternalSessionRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = ExternalSessionRegistry(workspace)
        return _REGISTRY


def reset_external_session_registry() -> None:
    """Test hook: drop the process-wide singleton."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


async def publish_channel_notice(
    bus: Any,
    session_key: str,
    text: str,
    *,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Put one runtime-originated notice on the external channel outbound lane.

    The shared web-shell drain is the only consumer of ``channel="ext"``: it
    sanitizes the text, registers it in the durable outbox and publishes
    ``outbound.created`` on that session's event hub, so anything sent through
    here inherits a turn reply's delivery contract (ledger, dedup id, ack,
    reconnect replay) instead of becoming an in-memory event.

    Returns True only when the message was handed to the bus. No channel other
    than ``ext:`` sessions has a delivery path, so non-external sessions and a
    missing bus are a silent no-op by contract, not a failure.
    """
    if bus is None:
        return False
    key = str(session_key or "").strip()
    body = str(text or "").strip()
    if not key or not body:
        return False
    outbound_metadata = {
        "source": str(source or "").strip(),
        "session_key": key,
    }
    if isinstance(metadata, dict):
        outbound_metadata.update(
            {str(name): value for name, value in metadata.items() if str(name or "").strip()}
        )
    await bus.publish_outbound(
        OutboundMessage(
            channel=EXTERNAL_OUTBOUND_CHANNEL,
            chat_id=key,
            content=body,
            metadata=outbound_metadata,
        )
    )
    return True
