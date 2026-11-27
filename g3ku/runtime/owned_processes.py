"""`exec background` 起出来的后台进程的归属登记表。

模型用 `Start-Process` / `nohup` 起的服务，一旦脱离 exec 的输出管道就脱离运行
时视野：谁起的、还在不在、日志在哪、该由谁回收，全都没有答案。结果是端口被占
着、下一轮没人知道它存在，而模型自己又被宿主进程保护挡着停不掉它。

这里补上"归属"这一格，登记是**持久**的（每个进程一个 json，落在数据根下），
不是进程内内存：运行时崩溃/被强杀之后，条目仍在盘上，下次启动按 `owner_pid`
是否存活判定接管——活进程由新运行时认领（`readopt_orphans`），死进程转
`.notice` 交消费口主动激活模型。

三条硬规矩：

- **scope 隔离**：CEO 会话与执行任务各有各的 scope，`process_kill` 只能看自己
  那一个；跨 scope 的 pid 等于没登记。
- **身份校验**：条目记 `create_time`，kill 前比对。PID 会被系统复用，只认 pid
  的话，一个旧条目可能指到刚起来的宿主进程上。
- **只向下杀**：终止走 `kill_process_tree_pid`（只收后代），祖先进程不在可达
  集合里。

日志与条目同寿命：`owned-processes/<scope 摘要>/<pid>-<create_time>.log`。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.utils.process_tree import kill_process_tree_pid_checked

_ENTRY_SUFFIX = ".json"
_LOG_SUFFIX = ".log"
_LOG_TAIL_CHARS = 2000


def _owned_root() -> Path:
    from g3ku.deployment.data_root import data_root

    return Path(data_root()) / "owned-processes"


def scope_key(*, session_key: str, task_id: str = "") -> str:
    """登记表的作用域键：执行节点按任务分，前门按会话分。"""
    task = str(task_id or "").strip()
    if task:
        return f"task:{task}"
    session = str(session_key or "").strip() or "web:shared"
    return f"session:{session}"


def _scope_dir(scope: str, *, create: bool = False) -> Path:
    digest = hashlib.sha1(str(scope or "").encode("utf-8")).hexdigest()[:16]
    path = _owned_root() / digest
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _entry_path(scope: str, pid: int, create_time: float) -> Path:
    stamp = f"{float(create_time):.3f}".replace(".", "_")
    return _scope_dir(scope, create=True) / f"{int(pid)}-{stamp}{_ENTRY_SUFFIX}"


def new_log_path(scope: str) -> Path:
    """为即将启动的后台进程分配一个日志落点（与登记表同目录，同寿命）。"""
    directory = _scope_dir(scope, create=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return directory / f"{stamp}-{uuid.uuid4().hex[:8]}{_LOG_SUFFIX}"


def _psutil() -> Any | None:
    try:
        import psutil  # noqa: PLC0415 - optional at import time

        return psutil
    except Exception:
        return None


def process_create_time(pid: int) -> float | None:
    """进程创建时间（秒）。取不到时返回 None——调用方据此判"无法校验身份"。"""
    try:
        normalized = int(pid)
    except (TypeError, ValueError):
        return None
    if normalized <= 0:
        return None
    psutil = _psutil()
    if psutil is not None:
        try:
            return float(psutil.Process(normalized).create_time())
        except Exception:
            return None
    return None


def is_alive(pid: int) -> bool:
    try:
        normalized = int(pid)
    except (TypeError, ValueError):
        return False
    if normalized <= 0:
        return False
    psutil = _psutil()
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(normalized))
        except Exception:
            return False
    try:
        os.kill(normalized, 0)
        return True
    except Exception:
        return False


def register(
    *,
    scope: str,
    pid: int,
    cmd: str,
    log_path: str,
    session_key: str,
    task_id: str = "",
    node_id: str = "",
    actor_role: str = "",
    owner_pid: int | None = None,
    label: str = "",
) -> dict[str, Any]:
    """登记一个后台进程。`create_time` 取不到时按 0 存，kill 侧会拒绝无身份条目。"""
    create_time = process_create_time(pid) or 0.0
    normalized_owner_pid = int(owner_pid if owner_pid is not None else os.getpid())
    entry = {
        "pid": int(pid),
        "create_time": create_time,
        "cmd": str(cmd or ""),
        "log_path": str(log_path or ""),
        "scope": str(scope or ""),
        "session_key": str(session_key or ""),
        "task_id": str(task_id or ""),
        "node_id": str(node_id or ""),
        "actor_role": str(actor_role or ""),
        "owner_pid": normalized_owner_pid,
        "owner_create_time": process_create_time(normalized_owner_pid) or 0.0,
        "label": str(label or ""),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = _entry_path(scope, int(pid), create_time)
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def _load_entry(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _identity_matches(entry: dict[str, Any]) -> bool:
    """条目指向的进程是否还是当初那一个（防 PID 复用）。"""
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if not is_alive(pid):
        return False
    recorded = float(entry.get("create_time") or 0.0)
    if recorded <= 0:
        return False
    actual = process_create_time(pid)
    if actual is None:
        return False
    return abs(actual - recorded) < 1.0


def identity_matches(entry: dict[str, Any]) -> bool:
    """条目指向的进程是否还是当初那一个（防 PID 复用）；公开给观察/认领车道。"""
    return _identity_matches(entry)


def list_owned(
    scope: str,
    *,
    prune: bool = True,
    node_id: str | None = None,
    include_unattributed: bool = False,
) -> list[dict[str, Any]]:
    """列出该 scope 下仍然有效的条目；死条目与身份不符的条目顺手删掉。

    `node_id` 给定时按条目归属过滤；`include_unattributed=True` 把迁移前的
    空 `node_id` 条目一并带出（kill 台账要能收尾这些 legacy 条目）。头部清单
    不传这个开关，只显示可归属到当前节点的进程。
    """
    directory = _scope_dir(scope, create=False)
    if not directory.exists():
        return []
    normalized_node = str(node_id or "").strip()
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.glob(f"*{_ENTRY_SUFFIX}")):
        entry = _load_entry(path)
        if entry is None:
            path.unlink(missing_ok=True)
            continue
        if prune and not _identity_matches(entry):
            path.unlink(missing_ok=True)
            continue
        if normalized_node:
            recorded_node = str(entry.get("node_id") or "").strip()
            if recorded_node != normalized_node and not (include_unattributed and not recorded_node):
                continue
        entries.append(entry)
    return entries


def find(scope: str, pid: int) -> dict[str, Any] | None:
    directory = _scope_dir(scope, create=False)
    if not directory.exists():
        return None
    for path in sorted(directory.glob(f"{int(pid)}-*{_ENTRY_SUFFIX}")):
        entry = _load_entry(path)
        if entry is None:
            path.unlink(missing_ok=True)
            continue
        return entry
    return None


def list_session_owned() -> list[dict[str, Any]]:
    """所有会话域的登记条目（`task_id` 为空），供会话道全域退出观察器清扫。

    读面**不 prune**：死条目是"还没交代"的凭证，删掉就没人能报它了。
    无法解析的条目照旧清掉。节点条目（有 `task_id`）不在这里，它们由
    认领时挂上的无句柄观察器负责。
    """
    root = _owned_root()
    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        for path in sorted(directory.glob(f"*{_ENTRY_SUFFIX}")):
            entry = _load_entry(path)
            if entry is None:
                path.unlink(missing_ok=True)
                continue
            if str(entry.get("task_id") or "").strip():
                continue
            if not str(entry.get("session_key") or "").strip():
                continue
            try:
                pid = int(entry.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            entries.append(entry)
    return entries


def drop(scope: str, pid: int) -> None:
    directory = _scope_dir(scope, create=False)
    if not directory.exists():
        return
    for path in sorted(directory.glob(f"{int(pid)}-*{_ENTRY_SUFFIX}")):
        path.unlink(missing_ok=True)


def _entry_paths(scope: str, pid: int) -> list[Path]:
    directory = _scope_dir(scope, create=False)
    if not directory.exists():
        return []
    return sorted(directory.glob(f"{int(pid)}-*{_ENTRY_SUFFIX}"))


def mark_notice(
    scope: str,
    pid: int,
    *,
    exit_reason: str,
    exit_code: int | None = None,
    exited_at: str | None = None,
    extra: dict[str, Any] | None = None,
    keep_entry: bool = False,
) -> bool:
    """把活条目改写成待投递的 `.notice` 侧车；普通读取面看不见它。

    进程已经退出/将被回收而运行时还没把结果交给模型时，条目不能静默删掉：
    它既是"谁还没交代"的凭证，也是重启后消费口的唯一输入。写入走临时文件 +
    `os.replace`，先落新文件再摘旧 `.json`，读者最坏看到两份，不会看到半份。
    `keep_entry=True` 用于非终态告知（如 survived_restart）：进程还活着，
    条目保留，notice 只是待投递的凭证。
    """
    paths = _entry_paths(scope, pid)
    if not paths:
        return False
    path = paths[0]
    entry = _load_entry(path) or {}
    entry.update(
        {
            "exit_reason": str(exit_reason or ""),
            "exit_code": exit_code,
            "exited_at": str(exited_at or time.strftime("%Y-%m-%dT%H:%M:%S")),
            "scope": str(scope or entry.get("scope") or ""),
        }
    )
    if isinstance(extra, dict):
        entry.update(extra)
    notice_path = path.with_suffix(".notice")
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, notice_path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    if not keep_entry:
        path.unlink(missing_ok=True)
    return True


def write_swept_notice(
    *,
    scope: str,
    session_key: str,
    task_id: str = "",
    node_id: str = "",
    actor_role: str = "",
    command: str,
    descendants: list[dict[str, Any]],
    exit_reason: str = "swept_by_exec_cancel",
) -> str:
    """记一条"整树被前台调用取消带走"的聚合凭证。

    取消路径不产生工具结果，逐个 pid 写 notice 会在启动时刷屏；这里一次取消
    只落一条记录，`swept_pids` 带全部被扫进程。消费口（服务 startup）按节点/
    会话分流投递后删除。没有任何后代时不写文件，返回空串。
    """
    pids: list[dict[str, Any]] = []
    for item in descendants or []:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        pids.append({"pid": pid, "name": str(item.get("name") or "")})
    if not pids:
        return ""
    sweep_id = uuid.uuid4().hex[:12]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry = {
        "swept": True,
        "sweep_id": sweep_id,
        "pid": 0,
        "create_time": 0.0,
        "cmd": str(command or ""),
        "log_path": "",
        "scope": str(scope or ""),
        "session_key": str(session_key or ""),
        "task_id": str(task_id or ""),
        "node_id": str(node_id or ""),
        "actor_role": str(actor_role or ""),
        "owner_pid": os.getpid(),
        "started_at": now,
        "exited_at": now,
        "exit_reason": str(exit_reason or "swept_by_exec_cancel"),
        "swept_pids": pids,
    }
    directory = _scope_dir(scope, create=True)
    notice_path = directory / f"swept-{sweep_id}.notice"
    tmp_path = notice_path.with_name(notice_path.name + ".tmp")
    try:
        tmp_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, notice_path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return ""
    return str(notice_path)


def swept_notice_text(entry: dict[str, Any]) -> str:
    """节点面的"取消带走整树"告知：英文、稳定（不含相对时长），重试同文才幂等。"""
    pids = [item for item in (entry.get("swept_pids") or []) if isinstance(item, dict)]
    count = len(pids)
    lines = [
        f"Foreground exec processes were swept when the call was cancelled: {count} process(es)."
    ]
    cmd = str(entry.get("cmd") or "").strip()
    if cmd:
        lines.append(f"Command: {cmd}")
    shown: list[str] = []
    for item in pids[:12]:
        try:
            pid = int(item.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or "").strip()
        shown.append(f"pid={pid}" + (f" ({name})" if name else ""))
    if shown:
        more = f" (+{count - 12} more)" if count > 12 else ""
        lines.append(f"Swept: {', '.join(shown)}{more}.")
    lines.append(
        "They are gone. If any of them must keep running, restart it with exec background=true."
    )
    return "\n".join(lines)


def list_notices() -> list[dict[str, Any]]:
    """待投递的退出凭证；返回值带内部 `_notice_path` 供消费后删除。"""
    root = _owned_root()
    if not root.exists():
        return []
    notices: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/*.notice")):
        entry = _load_entry(path)
        if entry is None:
            path.unlink(missing_ok=True)
            continue
        entry["_notice_path"] = str(path)
        notices.append(entry)
    return notices


def drop_notice(notice_path: str) -> None:
    try:
        Path(str(notice_path or "")).unlink(missing_ok=True)
    except OSError:
        pass


def read_log_tail(entry: dict[str, Any], *, limit: int = _LOG_TAIL_CHARS) -> str:
    raw_path = str(entry.get("log_path") or "").strip()
    if not raw_path:
        return ""
    try:
        text = Path(raw_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def kill_owned(scope: str, pid: int, *, node_id: str | None = None) -> dict[str, Any]:
    """终止一个已登记的进程；未登记、已死、身份不符都返回 ok=False 并说明原因。

    `node_id` 给定时只接受该节点登记的条目；空 `node_id` 的 legacy 条目放行一次。
    终止结果按"进程已消失"裁决：taskkill/kill 核对失败时**保留登记**并返回
    ``kill_failed``（带诊断与仍存活的后代列表），绝不把"命令执行了"冒充成
    "已经停止"；只有确认退出或确认条目不再对应原进程才退休登记。
    """
    entry = find(scope, pid)
    if entry is None:
        return {
            "ok": False,
            "pid": int(pid),
            "reason": "not_owned",
            "message": (
                "This pid is not an exec background process owned by the current "
                "session/task. Start long-running work with exec background=true, "
                "then stop it here."
            ),
        }
    normalized_node = str(node_id or "").strip()
    recorded_node = str(entry.get("node_id") or "").strip()
    if normalized_node and recorded_node and recorded_node != normalized_node:
        return {
            "ok": False,
            "pid": int(pid),
            "reason": "not_owned",
            "message": "This pid belongs to another node of the task.",
            "entry": entry,
        }
    if not is_alive(int(pid)):
        drop(scope, pid)
        return {"ok": False, "pid": int(pid), "reason": "already_exited", "entry": entry}
    if not _identity_matches(entry):
        drop(scope, pid)
        return {
            "ok": False,
            "pid": int(pid),
            "reason": "identity_mismatch",
            "message": "The recorded pid now belongs to a different process; refusing to terminate it.",
            "entry": entry,
        }
    checked = kill_process_tree_pid_checked(int(pid))
    if checked.terminated:
        drop(scope, pid)
        return {
            "ok": True,
            "pid": int(pid),
            "reason": "terminated",
            "entry": entry,
            "log_tail": read_log_tail(entry),
            "detail": checked.detail,
            "leftover_descendants": [],
        }
    if checked.reason == "already_gone":
        # 身份刚校验过、taskkill 报 not found：目标在两次检查之间退出了。
        drop(scope, pid)
        return {
            "ok": False,
            "pid": int(pid),
            "reason": "already_exited",
            "entry": entry,
            "detail": checked.detail,
        }
    # 终止未完成：登记保留在盘上，等真实退出由自然退出观察道或下次停止收尾。
    return {
        "ok": False,
        "pid": int(pid),
        "reason": "kill_failed",
        "entry": entry,
        "log_tail": read_log_tail(entry),
        "detail": checked.detail or checked.reason,
        "leftover_descendants": list(checked.descendant_leftovers),
    }


def reap_scope(
    scope: str,
    *,
    node_ids: list[str] | set[str] | None = None,
    include_unattributed: bool = False,
) -> dict[str, Any]:
    """回收该 scope 下的存活条目（会话清空、任务终态/删除、范围停止时用）。

    `node_ids` 给定时只回收归属这些节点的条目；`include_unattributed=True`
    才带出空 `node_id` 的 legacy 条目（单节点清理不传它，全任务动作才收）。
    返回逐项裁决：``killed`` 是确认终止的 PID，``already_gone`` 是已退出并
    退休的 PID，``failed`` 保留未完成对象与原因，条目不丢。
    """
    normalized_nodes = {str(item or "").strip() for item in (node_ids or []) if str(item or "").strip()}
    killed: list[int] = []
    already_gone: list[int] = []
    failed: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    entries = list_owned(scope, include_unattributed=include_unattributed)
    if normalized_nodes:
        entries = [
            entry
            for entry in entries
            if str(entry.get("node_id") or "").strip() in normalized_nodes
        ]
    for entry in entries:
        pid = int(entry.get("pid") or 0)
        if pid <= 0:
            continue
        result = kill_owned(scope, pid)
        reason = str(result.get("reason") or "")
        if result.get("ok"):
            killed.append(pid)
            items.append({"pid": pid, "status": "terminated", "node_id": entry.get("node_id") or ""})
        elif reason in {"already_exited", "identity_mismatch"}:
            already_gone.append(pid)
            items.append({"pid": pid, "status": reason, "node_id": entry.get("node_id") or ""})
        else:
            failed.append(
                {
                    "pid": pid,
                    "reason": reason or "kill_failed",
                    "detail": str(result.get("detail") or ""),
                    "leftover_descendants": list(result.get("leftover_descendants") or []),
                    "node_id": entry.get("node_id") or "",
                }
            )
            items.append({"pid": pid, "status": "kill_failed", "node_id": entry.get("node_id") or ""})
    return {"killed": killed, "already_gone": already_gone, "failed": failed, "items": items}


def _owner_alive(entry: dict[str, Any]) -> bool:
    """登记的 owner 是否还是当初那个进程（防止重启后 PID 复用）。

    `owner_create_time` 为 0 的 legacy 条目只能退回 `is_alive`；新条目两者都比。
    """
    try:
        owner_pid = int(entry.get("owner_pid") or 0)
    except (TypeError, ValueError):
        return False
    if owner_pid <= 0 or not is_alive(owner_pid):
        return False
    recorded = float(entry.get("owner_create_time") or 0.0)
    if recorded <= 0:
        return True
    actual = process_create_time(owner_pid)
    if actual is None:
        return True
    return abs(actual - recorded) < 1.0


def readopt_orphans(new_owner_pid: int) -> list[dict[str, Any]]:
    """认领 owner 运行时已不在、但进程仍活着的条目（崩溃/重启后的幸存者）。

    后台进程脱离运行时生命周期轴：重启不杀它们，新运行时改写 owner 接管，
    退出观察与 `process_kill` 归属照常。进程已死（含 PID 复用）的条目照旧
    `mark_notice`，交启动消费口主动激活模型——死进程绝不自动重拉。
    返回认领成功的条目列表；调用方负责挂观察器与投存活告知。
    """
    root = _owned_root()
    if not root.exists():
        return []
    normalized_owner = int(new_owner_pid)
    owner_create_time = process_create_time(normalized_owner) or 0.0
    readopted: list[dict[str, Any]] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        for path in sorted(directory.glob(f"*{_ENTRY_SUFFIX}")):
            entry = _load_entry(path)
            if entry is None:
                path.unlink(missing_ok=True)
                continue
            scope = str(entry.get("scope") or "").strip()
            pid = int(entry.get("pid") or 0)
            if not scope or pid <= 0:
                path.unlink(missing_ok=True)
                continue
            if _owner_alive(entry):
                continue
            if _identity_matches(entry):
                entry["owner_pid"] = normalized_owner
                entry["owner_create_time"] = owner_create_time
                tmp_path = path.with_name(path.name + ".tmp")
                try:
                    tmp_path.write_text(
                        json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    os.replace(tmp_path, path)
                except OSError:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    logger.warning(
                        "failed to rewrite owner for readopted process pid={} scope={}",
                        pid,
                        scope,
                    )
                    continue
                readopted.append(entry)
                continue
            exit_reason = "process_gone" if not is_alive(pid) else "identity_mismatch"
            mark_notice(scope, pid, exit_reason=exit_reason)
    if readopted:
        logger.info(
            "owned-process readopt took over {} surviving background process(es)",
            len(readopted),
        )
    return readopted


def prune_notices(*, max_age_days: float = 7.0, max_count: int = 500) -> int:
    """清理长期投不出去的 `.notice`：按年龄和条数双上限，返回删除数。"""
    root = _owned_root()
    if not root.exists():
        return 0
    path_list = sorted(root.glob("*/*.notice"))
    now = time.time()
    removed = 0
    kept: list[Path] = []
    for path in path_list:
        try:
            age_seconds = now - path.stat().st_mtime
        except OSError:
            continue
        if max_age_days > 0 and age_seconds > max_age_days * 86400:
            path.unlink(missing_ok=True)
            removed += 1
            continue
        kept.append(path)
    if max_count > 0 and len(kept) > max_count:
        for path in kept[: len(kept) - max_count]:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
