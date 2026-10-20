"""前门钉住 `tools[]` 整表的落盘载体（会话级 singleton）。

web 进程没有节点道那套 task-artifact store（`create_or_replace_singleton_text_artifact` 只在
worker 侧），所以这里自己开一条按 `session_key` 单文件就地覆盖的车道。四条刻意的形状：

- 存的是**投影后的线上原字节**（渲染结果本身）。读回来原样递出去，不重投影、不重排、不补默认值：
  重投影是"读路径与写回不同源"那一类缺陷的入口。
- 只存文件。连续性侧车里只带 **path + revision**，不塞字节本体——侧车最大实测 15 MB，而基线
  载荷每跳都可能重写，多带一份 ~16 KB 的整表等于把这份成本乘在每一跳上。先例是
  `frontdoor_actual_request_path` 与 `pinned_contract_text` 的分法。
- singleton 就地覆盖 ⇒ **路径不随内容变**（节点道同一个坑：`artifact_id` 被复用）。所以缓存键
  必须是 `(path, revision)`，revision 取内容的 sha256；只按 path 缓存会在重印之后继续供旧字节。
- 进程内那份按 `session_key` 分桶的表是给**拿不到 session 对象**的读点用的：可渲染性闸门在装配
  层被调用，那里只有 `session_key`。形状与 `tool_contract` 的钉住头部表一致。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.deployment.data_root import data_root
from g3ku.resources.retired_schemas import index_schemas_by_name
from g3ku.utils.helpers import ensure_dir, safe_filename

PINNED_TOOL_BUNDLE_ROOT = Path(".g3ku") / "web-ceo-tool-bundle"
# 与钉住头部同一条 LRU 纪律：进程表只是"这回合内别重复读盘"，真值在文件里。
_MAX_CACHED_SESSIONS = 64
_lock = threading.Lock()
_entries: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def now_iso() -> str:
    return datetime.now().isoformat()


def _normalized_name_list(values: Any) -> list[str]:
    names: list[str] = []
    for raw in list(values or []):
        name = str(raw or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def bundle_path_for_session(session_key: str, *, create: bool = True) -> Path:
    safe_session = safe_filename(str(session_key or "web_shared").replace(":", "_")) or "web_shared"
    root = data_root() / PINNED_TOOL_BUNDLE_ROOT
    directory = ensure_dir(root) if create else root
    return directory / f"{safe_session}.json"


def compute_revision(payload_text: str) -> str:
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    directory = ensure_dir(path.parent)
    # 临时名后缀要短：工件名 + 32 位 uuid 会把 temp 路径顶过 Windows 260 字符上限
    # （未开长路径支持时 open() 直接 FileNotFoundError，看着像目录不存在）。
    temp_path = directory / f"{path.name}.{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(text)
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_pinned_tool_bundle(
    *,
    session_key: str,
    provider_tool_names: list[str] | None,
    rendered_tool_schemas: list[dict[str, Any]] | None,
    build_reason: str,
) -> dict[str, Any]:
    """把整表原字节落盘，返回 {path, revision, names}；任何失败返回空表（调用方记 fault）。

    顺序按传入的渲染结果走，不排序：钉住的是成员**与顺序**，读回来必须逐字节复用同一份。
    """
    schemas = [dict(item) for item in list(rendered_tool_schemas or []) if isinstance(item, dict)]
    if not schemas:
        return {}
    names = _normalized_name_list(provider_tool_names)
    payload = {
        "session_key": str(session_key or "").strip(),
        "names": names,
        "tool_schemas": schemas,
        "built_at": now_iso(),
        "build_reason": str(build_reason or "").strip(),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    revision = compute_revision(text)
    path = bundle_path_for_session(str(session_key or "").strip())
    try:
        _atomic_write(path, text)
    except Exception:
        return {}
    remember_pinned_tool_bundle(
        session_key=str(session_key or "").strip(),
        path=str(path),
        revision=revision,
        index=index_schemas_by_name(schemas),
        names=names,
    )
    return {"path": str(path), "revision": revision, "names": names}


def read_pinned_tool_bundle_index(
    *,
    session_key: str,
    path: str,
    revision: str = "",
) -> dict[str, dict[str, Any]]:
    """按 (path, revision) 读回整表并按名索引；读不到返回空表。

    这里刻意不把"0 字节 / 解析失败 / `names` 为空"折叠成"名单为空"：满盘写坏过的载体是 0 字节，
    把它读成空索引等于告诉调用方"这跳没有兜底源"，而真实语义是"这份工件读不出来"。两者的
    处置不同——后者要走强制重印，所以统一返回空表，由调用侧按"缺失"计一次可数控数。
    """
    ref = str(path or "").strip()
    if not ref:
        return {}
    with _lock:
        entry = _entries.get(str(session_key or "").strip())
        if entry and str(entry.get("path") or "") == ref:
            if not revision or str(entry.get("revision") or "") == revision:
                return dict(entry.get("index") or {})
    try:
        text = Path(ref).read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    names = _normalized_name_list(payload.get("names"))
    schemas = [dict(item) for item in list(payload.get("tool_schemas") or []) if isinstance(item, dict)]
    if not names or not schemas:
        return {}
    index = index_schemas_by_name(schemas)
    if not index:
        return {}
    payload_revision = str(payload.get("revision") or "").strip() or compute_revision(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    remember_pinned_tool_bundle(
        session_key=str(session_key or "").strip(),
        path=ref,
        revision=payload_revision,
        index=index,
        names=names,
    )
    return index


def remember_pinned_tool_bundle(
    *,
    session_key: str,
    path: str,
    revision: str,
    index: dict[str, dict[str, Any]],
    names: list[str] | None = None,
) -> None:
    key = str(session_key or "").strip()
    if not key or not index:
        return
    with _lock:
        _entries[key] = {
            "path": str(path or "").strip(),
            "revision": str(revision or "").strip(),
            "index": dict(index),
            "names": _normalized_name_list(names or list(index.keys())),
        }
        _entries.move_to_end(key)
        while len(_entries) > _MAX_CACHED_SESSIONS:
            _entries.popitem(last=False)


def bundle_names_for_session(session_key: str) -> list[str]:
    key = str(session_key or "").strip()
    with _lock:
        entry = _entries.get(key)
        return list(entry.get("names") or []) if entry else []


def clear_pinned_tool_bundles() -> None:
    """清空进程表（测试隔离用）。

    这份表按 `session_key` 分桶且活在整个进程里，而用例的数据根每支换一个目录：上一支用例
    写下的整表文件此时已不在，条目却还指着它——读回来的是别人家的字节。
    """
    with _lock:
        _entries.clear()


def index_for_session(session_key: str) -> dict[str, dict[str, Any]]:
    """按 session_key 取整表索引（会话属性为空时的第二份载体）。

    必须有这一份：同一回合里不同装配路拿到的 session 对象不是同一个实例，只认会话属性的话，
    换一个对象就读不到整表，于是又去读活实例——单源就在这个装配点破了。
    """
    key = str(session_key or "").strip()
    if not key:
        return {}
    with _lock:
        entry = _entries.get(key)
        if not entry:
            return {}
        entry_ref = dict(entry.get("index") or {})
        path = str(entry.get("path") or "")
        revision = str(entry.get("revision") or "")
    if entry_ref:
        return entry_ref
    return read_pinned_tool_bundle_index(session_key=key, path=path, revision=revision)


def has_bundled_schema(*, session_key: str, session: Any = None, name: str) -> bool:
    """这个名字能不能从整表供出参数表（闸门放行用的判据）。

    先看会话属性（同回合内最快的一份），再看按 session_key 分桶的进程表——装配层的调用点
    拿不到 session 对象，只有 key。
    """
    normalized = str(name or "").strip()
    if not normalized:
        return False
    if session is not None:
        bundled = getattr(session, "_frontdoor_pinned_tool_schema_bundle", None)
        if isinstance(bundled, dict) and isinstance(bundled.get(normalized), dict):
            return True
    key = str(session_key or "").strip()
    with _lock:
        entry = _entries.get(key)
        if entry and isinstance(entry.get("index"), dict):
            return isinstance(entry["index"].get(normalized), dict)
    return False


def forget_pinned_tool_bundle(session_key: str) -> None:
    """会话删除时的回收：进程表条目 + 那份单文件。"""
    key = str(session_key or "").strip()
    if not key:
        return
    with _lock:
        entry = _entries.pop(key, None)
    path = str((entry or {}).get("path") or "").strip()
    if not path:
        # 没有进程条目时按同一命名规则推算路径；`create=False` 保证"删一个不存在的会话"
        # 不会顺手把根目录建出来。
        path = str(bundle_path_for_session(key, create=False))
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def restore_pinned_tool_bundle_state(
    session: Any,
    *,
    session_key: str,
    path: Any,
    revision: Any,
) -> bool:
    """重启/续跑时把整表放回两份载体。返回是否真恢复了内容。

    **只在进程内载体为空时覆盖**（沿用钉住头部的纪律）：本回合刚跨过重印边界时，会话属性里
    那份是刚重渲的，被一份持久化的旧字节盖回去等于把重印抹掉。载荷没带这些键时同样不动。
    """
    ref = str(path or "").strip()
    if not ref:
        return False
    key = str(session_key or "").strip()
    current = getattr(session, "_frontdoor_pinned_tool_schema_bundle", None) if session is not None else None
    if isinstance(current, dict) and current:
        return False
    index = read_pinned_tool_bundle_index(session_key=key, path=ref, revision=str(revision or "").strip())
    if not index:
        return False
    if session is not None:
        try:
            setattr(session, "_frontdoor_pinned_tool_schema_bundle", dict(index))
            setattr(session, "_frontdoor_pinned_tool_bundle_path", ref)
            setattr(session, "_frontdoor_pinned_tool_bundle_revision", str(revision or "").strip())
        except Exception:
            pass
    remember_pinned_tool_bundle(
        session_key=key,
        path=ref,
        revision=str(revision or "").strip(),
        index=index,
    )
    return True
