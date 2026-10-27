"""数据面工具 → jail 代理。

只有**数据面**工具换实例；控制面（记忆、任务、cron、配置）根本不进 jail，走宿主的
正常车道。代理只覆写 ``execute``，其余契约（`name` / `parameters` / `to_model_schema` /
`coerce_arguments` …）全部转发底层的原对象——所以 `tools[]` 的字节不变，前缀缓存不受影响。

三条硬规则：

* 路径先过 :meth:`SandboxManager.display_path`：模型只该看见 ``/workspace`` 与 ``/shared``，
  旧上下文里的宿主绝对路径在这里被拒，而不是变成一串"文件不存在"。
* ``exec`` 的 ``background`` 在 jail 里不支持：jail 是每次调用一个一次性进程，
  后台进程会随调用结束消失，收下它等于给模型一个不存在的承诺。
* 所有阻塞调用走 ``asyncio.to_thread``：jail 的每次执行都是一个子进程，不能卡住事件循环。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable

from g3ku.agent.tools.base import Tool
from g3ku.sandbox.errors import SandboxDeniedError, SandboxError, SandboxUnavailableError
from g3ku.sandbox.manager import SandboxHandle, SandboxManager

# 需要进 jail 的数据面工具。propose_patch / process_kill 不在其中：前者依赖宿主的
# 工件库，后者在一次性进程模型下没有对象可杀（见 policy 的 jailed 拒绝集）。
JAILED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "exec",
        "filesystem_write",
        "filesystem_edit",
        "filesystem_delete",
        "filesystem_copy",
        "filesystem_move",
        "filesystem_stat",
        "content_open",
        "content_search",
        "content_describe",
    }
)

_MAX_EXEC_OUTPUT_CHARS = 60_000


def _error_text(message: str) -> str:
    return f"Error: {message}"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


class JailToolProxy(Tool):
    """同一个工具契约、换一个执行面。"""

    def __init__(self, *, base_tool: Tool, manager: SandboxManager, handle: SandboxHandle):
        self._base_tool = base_tool
        self._manager = manager
        self._handle = handle

    # 抽象成员与带默认实现的契约成员都要显式转发：默认实现会**遮蔽** __getattr__，
    # 漏掉任何一条都会让 tools[] 与宿主档产生字节差。
    @property
    def name(self) -> str:
        return self._base_tool.name

    @property
    def description(self) -> str:
        return self._base_tool.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._base_tool.parameters

    @property
    def model_description(self) -> str:
        return self._base_tool.model_description

    @property
    def model_parameters(self) -> dict[str, Any]:
        return self._base_tool.model_parameters

    @property
    def self_enforced_timeout(self) -> bool:
        return bool(getattr(self._base_tool, "self_enforced_timeout", False))

    @property
    def hide_universal_timeout_parameter(self) -> bool:
        return bool(getattr(self._base_tool, "hide_universal_timeout_parameter", False))

    @property
    def exempt_universal_timeout(self) -> bool:
        return bool(getattr(self._base_tool, "exempt_universal_timeout", False))

    @property
    def rerun_safe(self) -> bool:
        return bool(getattr(self._base_tool, "rerun_safe", False))

    def __getattr__(self, item: str) -> Any:
        # 其余成员（前缀、能力声明、未来的新标志）一律转发，代理只拥有 execute。
        base = self.__dict__.get("_base_tool")
        if base is None:
            raise AttributeError(item)
        return getattr(base, item)

    async def execute(self, **kwargs: Any) -> Any:
        kwargs.pop("__g3ku_runtime", None)
        return await asyncio.to_thread(self._dispatch, dict(kwargs))

    # -- 分派 ---------------------------------------------------------------

    def _dispatch(self, args: dict[str, Any]) -> str:
        name = str(getattr(self._base_tool, "name", "") or "")
        try:
            handler = self._handlers().get(name)
            if handler is None:
                return _error_text(f"{name} is not available inside the sandbox")
            return handler(args)
        except SandboxDeniedError as exc:
            return _error_text(f"sandbox denied: {exc}")
        except SandboxUnavailableError as exc:
            return _error_text(f"sandbox unavailable: {exc}")
        except SandboxError as exc:  # noqa: BLE001 - 其余沙箱错误按不可读原因返回
            return _error_text(str(exc))
        except Exception as exc:  # noqa: BLE001 - 工具层不抛异常给图执行器
            return _error_text(f"sandbox tool failed: {type(exc).__name__}: {exc}")

    def _handlers(self):
        return {
            "exec": self._exec,
            "filesystem_write": self._write,
            "filesystem_edit": self._edit,
            "filesystem_delete": self._delete,
            "filesystem_copy": self._copy,
            "filesystem_move": self._move,
            "filesystem_stat": self._stat,
            "content_open": self._open,
            "content_search": self._search,
            "content_describe": self._describe,
        }

    # -- 执行面 -------------------------------------------------------------

    def _exec(self, args: dict[str, Any]) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            return _error_text("command must not be empty")
        if bool(args.get("background")):
            return _error_text(
                "background processes are not supported inside the sandbox: the jail is one "
                "process per call, so anything left running disappears with the call"
            )
        working_dir = args.get("working_dir")
        cwd = self._manager.default_cwd(self._handle)
        if working_dir:
            cwd = self._manager.map_path(self._handle, str(working_dir))
        timeout = float(args.get("timeout_seconds") or 300.0)
        result = self._manager.run(self._handle, ["sh", "-lc", command], cwd=cwd, timeout=timeout)
        body = result.stdout
        if result.stderr:
            body = f"{body}\nSTDERR:\n{result.stderr}".strip()
        truncated = len(body) > _MAX_EXEC_OUTPUT_CHARS
        preview = body[:_MAX_EXEC_OUTPUT_CHARS]
        payload: dict[str, Any] = {
            "status": "success" if result.exit_code == 0 else "error",
            "exit_code": result.exit_code,
            "head_preview": preview,
            "tail_preview": "",
            "stdout_truncated": truncated,
            "stderr_truncated": False,
            "stdout_captured_bytes": len(result.stdout.encode("utf-8")),
            "stderr_captured_bytes": len(result.stderr.encode("utf-8")),
            "output_truncated": truncated,
        }
        if result.timed_out:
            payload["error"] = result.stderr or "command timed out"
        return _json(payload)

    def _fsop(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._manager.fsop(self._handle, request)

    def _host_path(self, value: Any) -> str:
        return self._manager.map_path(self._handle, str(value or ""))

    def _shown_path(self, value: Any) -> str:
        return self._manager.display_path(str(value or ""))

    def _write(self, args: dict[str, Any]) -> str:
        display = self._shown_path(args.get("path"))
        payload = self._fsop(
            {
                "op": "write",
                "path": self._host_path(args.get("path")),
                "content": args.get("content"),
            }
        )
        if not payload.get("ok"):
            return _error_text(str(payload.get("error") or "write failed"))
        return f"Successfully wrote {payload.get('bytes', 0)} bytes to {display}"

    def _edit(self, args: dict[str, Any]) -> str:
        display = self._shown_path(args.get("path"))
        request: dict[str, Any] = {"op": "edit", "path": self._host_path(args.get("path"))}
        target = args.get("target") if isinstance(args.get("target"), dict) else {}
        by = str(target.get("by") or "").strip().lower()
        legacy_mode = str(args.get("mode") or "").strip().lower()
        if by == "exact_text":
            request.update(
                {
                    "mode": "exact_text",
                    "old_text": target.get("text"),
                    "new_text": args.get("new_text"),
                }
            )
        elif by == "anchor_pair":
            request.update(
                {
                    "mode": "anchor_pair",
                    "start_anchor": target.get("start_anchor"),
                    "end_anchor": target.get("end_anchor"),
                    "new_text": args.get("new_text"),
                }
            )
        elif by == "line_range":
            request.update(
                {
                    "mode": "line_range",
                    "start_line": target.get("start_line"),
                    "end_line": target.get("end_line"),
                    "new_text": args.get("new_text"),
                }
            )
        elif legacy_mode == "text_replace":
            request.update(
                {
                    "mode": "exact_text",
                    "old_text": args.get("old_text"),
                    "new_text": args.get("new_text"),
                }
            )
        elif legacy_mode == "line_range":
            request.update(
                {
                    "mode": "line_range",
                    "start_line": args.get("start_line"),
                    "end_line": args.get("end_line"),
                    "new_text": args.get("replacement"),
                }
            )
        else:
            return _error_text("edit requires exactly one mode: target.by or mode")
        payload = self._fsop(request)
        if not payload.get("ok"):
            return _error_text(str(payload.get("error") or "edit failed"))
        return f"Successfully edited {display}"

    def _delete(self, args: dict[str, Any]) -> str:
        raw_paths = args.get("paths") if isinstance(args.get("paths"), list) else []
        if not raw_paths:
            return _error_text("paths must be a non-empty list")
        paths = [self._host_path(item) for item in raw_paths]
        payload = self._fsop(
            {
                "op": "remove",
                "paths": paths,
                "recursive": bool(args.get("recursive")),
                "allow_missing": bool(args.get("allow_missing")),
                "continue_on_error": bool(args.get("continue_on_error")),
            }
        )
        if not payload.get("ok"):
            return _error_text(str(payload.get("error") or "delete failed"))
        removed = list(payload.get("removed") or [])
        errors = list(payload.get("errors") or [])
        text = f"Deleted {len(removed)} path(s)"
        if errors:
            text = f"{text}; {len(errors)} error(s): " + "; ".join(
                str(item.get("error") or "") for item in errors[:3]
            )
        return text

    def _transfer(self, args: dict[str, Any], *, move: bool) -> str:
        raw_operations = args.get("operations") if isinstance(args.get("operations"), list) else []
        if not raw_operations:
            return _error_text("operations must be a non-empty list")
        operations: list[dict[str, str]] = []
        for item in raw_operations:
            payload = item if isinstance(item, dict) else {}
            operations.append(
                {
                    "source": self._host_path(payload.get("source")),
                    "destination": self._host_path(payload.get("destination")),
                }
            )
        result = self._fsop(
            {
                "op": "move" if move else "copy",
                "operations": operations,
                "overwrite": bool(args.get("overwrite")),
                "create_parents": bool(args.get("create_parents")),
                "continue_on_error": bool(args.get("continue_on_error")),
            }
        )
        if not result.get("ok"):
            return _error_text(str(result.get("error") or "transfer failed"))
        verb = "Moved" if move else "Copied"
        completed = list(result.get("operations") or [])
        errors = list(result.get("errors") or [])
        text = f"{verb} {len(completed)} item(s)"
        if errors:
            text = f"{text}; {len(errors)} error(s): " + "; ".join(
                str(item.get("error") or "") for item in errors[:3]
            )
        return text

    def _copy(self, args: dict[str, Any]) -> str:
        return self._transfer(args, move=False)

    def _move(self, args: dict[str, Any]) -> str:
        return self._transfer(args, move=True)

    def _stat(self, args: dict[str, Any]) -> str:
        raw_paths = args.get("paths") if isinstance(args.get("paths"), list) else []
        if not raw_paths:
            return _error_text("paths must be a non-empty list")
        items: list[dict[str, Any]] = []
        for raw in raw_paths:
            entry: dict[str, Any] = {"path": self._shown_path(raw)}
            try:
                host = self._host_path(raw)
                stat = self._fsop({"op": "stat", "paths": [host]})
                item = (stat.get("items") or [{}])[0]
                entry.update({key: item[key] for key in ("exists", "kind", "size", "mtime") if key in item})
                if entry.get("kind") == "dir":
                    listed = self._fsop({"op": "list", "path": host})
                    entry["entries"] = list(listed.get("entries") or [])
            except SandboxError as exc:
                entry.update({"exists": False, "error": str(exc)})
            items.append(entry)
        return _json(
            {
                "ok": True,
                "items": items,
                "summary": f"{len(items)} paths measured inside the sandbox",
            }
        )

    # -- 内容类 -------------------------------------------------------------

    def _open(self, args: dict[str, Any]) -> str:
        if str(args.get("ref") or "").strip():
            return _error_text(
                "artifact refs are not available inside the sandbox; write the file into "
                "/workspace and open it by path"
            )
        raw_path = str(args.get("path") or "").strip()
        if not raw_path:
            return _error_text("path is required")
        request: dict[str, Any] = {"op": "read", "path": self._host_path(raw_path)}
        if args.get("start_line") is not None:
            request["start_line"] = args.get("start_line")
        if args.get("end_line") is not None:
            request["end_line"] = args.get("end_line")
        payload = self._fsop(request)
        if not payload.get("ok"):
            return _error_text(str(payload.get("error") or "open failed"))
        return _json(
            {
                "ok": True,
                "action": "open",
                "path": self._shown_path(raw_path),
                "text": payload.get("text") or "",
                "line_count": payload.get("line_count"),
                "truncated": payload.get("truncated", False),
            }
        )

    def _search(self, args: dict[str, Any]) -> str:
        raw_path = str(args.get("path") or "").strip()
        if not raw_path:
            return _error_text("path is required")
        query = str(args.get("query") or "")
        if not query:
            return _error_text("query is required")
        payload = self._fsop(
            {
                "op": "search",
                "path": self._host_path(raw_path),
                "query": query,
                "limit": args.get("limit") or 10,
            }
        )
        if not payload.get("ok"):
            return _error_text(str(payload.get("error") or "search failed"))
        return _json(
            {
                "ok": True,
                "action": "search",
                "path": self._shown_path(raw_path),
                "matches": list(payload.get("matches") or []),
                "truncated": payload.get("truncated", False),
            }
        )

    def _describe(self, args: dict[str, Any]) -> str:
        raw_path = str(args.get("path") or "").strip()
        if not raw_path:
            return _error_text("path is required")
        host = self._host_path(raw_path)
        stat = self._fsop({"op": "stat", "paths": [host]})
        item = (stat.get("items") or [{}])[0]
        preview = ""
        if item.get("kind") == "file":
            head = self._fsop({"op": "read", "path": host, "end_line": 6})
            preview = str(head.get("text") or "")[:600]
        return _json(
            {
                "ok": True,
                "action": "describe",
                "path": self._shown_path(raw_path),
                "exists": item.get("exists", False),
                "kind": item.get("kind"),
                "size": item.get("size"),
                "preview": preview,
            }
        )


def build_jailed_tools(
    base_tools: Iterable[Tool],
    *,
    manager: SandboxManager,
    handle: SandboxHandle,
) -> dict[str, Tool]:
    """把数据面工具换成 jail 代理；控制面与宿主侧工具原样留用（调用方合并）。"""
    proxies: dict[str, Tool] = {}
    for tool in list(base_tools or []):
        name = str(getattr(tool, "name", "") or "")
        if name in JAILED_TOOL_NAMES:
            proxies[name] = JailToolProxy(base_tool=tool, manager=manager, handle=handle)
    return proxies


__all__ = ["JAILED_TOOL_NAMES", "JailToolProxy", "build_jailed_tools"]
