"""Live acceptance for exec full-output artifact ref (P0) and the compaction ref index (P1).

Runs against the local Negi web instance on 127.0.0.1:18790 with a temporary
OpenAI-compatible fake provider, so the real lane runs deterministically:

frontdoor: exec a >400KB file -> envelope ref -> content_search(ref) finds a
sentinel that lives ONLY in the middle (not in head/tail) -> content_open(ref,
around_line) returns it with the line gutter -> an >8MiB path target is refused
with the exec-extraction hint -> create a node task.

node: repeat exec/search, then submit. The fake binding uses a small context
window so node compaction is likely; every node request body is scanned for the
machine-recomputed externalized-ref index heading.

What this proves: tee -> streaming registration -> envelope ref -> navigation
retrieval works end to end on the real runtime, and the read guard fires.
What it does NOT prove: that a real model chooses not to re-run the command —
the provider here is scripted, so that choice is not exercised.

Usage: .venv/Scripts/python.exe work/live_full_output_acceptance.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from g3ku.utils.net_env import normalize_no_proxy_env

normalize_no_proxy_env()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:18790"
WS_BASE = "ws://127.0.0.1:18790/api/ws/ceo"
MODEL_KEY = "full-output-acceptance"
MODEL_KEY_NODE = "full-output-acceptance-node"
FAKE_PORT = 18903
MARKER = "FULL-OUTPUT-ACCEPTANCE-" + str(int(time.time()))
REQUEST_LOG = pathlib.Path("work/full-output-acceptance-requests.jsonl")
BIG_FILE = pathlib.Path("D:/Project/Negi/work/acceptance-full-output.txt")
HUGE_FILE = pathlib.Path("D:/Project/Negi/work/acceptance-huge-output.txt")
HEAD = "HEAD-SENTINEL"
MIDDLE = "MIDDLE-SENTINEL-needle"
TAIL = "TAIL-SENTINEL"
REF_INDEX_HEADING = "【已外置结果索引】"
_NODE_STATE = {"step": 0, "ref": "", "tool": "", "args": None}
_FRONT_STATE = {"ref": "", "line": 0}
FINAL_TEXT = f"{MARKER} frontdoor acceptance complete"
TASK_PROMPT = (
    f"{MARKER}: 用 exec 运行 `Get-Content -Path {BIG_FILE.as_posix()}`，"
    "然后用 content_search 在结果 ref 里搜 MIDDLE-SENTINEL，再提交最终结果。"
)


def _prepare_files() -> None:
    filler = "x" * 40 + "\n"
    parts = [HEAD + "\n", filler * 6000, MIDDLE + "\n", filler * 6000, TAIL + "\n"]
    BIG_FILE.write_text("".join(parts), encoding="utf-8")
    HUGE_FILE.write_text("y" * (9 * 1024 * 1024), encoding="utf-8")


def _tool_names(body: dict) -> list[str]:
    names = []
    for item in body.get("tools") or []:
        fn = item.get("function") or {}
        name = str(fn.get("name") or item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _tool_messages(body: dict) -> list[dict]:
    return [m for m in (body.get("messages") or []) if str(m.get("role") or "") == "tool"]


def _last_tool_text(body: dict) -> str:
    messages = _tool_messages(body)
    if not messages:
        return ""
    content = messages[-1].get("content")
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or "")


def _body_text(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False)


def _extract_ref(text: str) -> str:
    # 工具回执是信封 JSON（summary 里的引号被转义），先按结构取 ref，再退回正则。
    try:
        payload = json.loads(text[text.index("{"):])
        ref = str(payload.get("ref") or "").strip()
        if ref:
            return ref
    except Exception:
        pass
    match = re.search(r'content_open\(ref=\\?"([^"\\]+)\\?"\)', text)
    return match.group(1) if match else ""


def _extract_line(text: str) -> int:
    try:
        payload = json.loads(text[text.index("{"):])
    except Exception:
        return 0
    hits = payload.get("hits") or []
    return int(hits[0].get("line") or 0) if hits else 0


class _FakeProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunks: list[dict]) -> None:
        payload = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
        payload += "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _chunks(self) -> list[dict]:
        return [{
            "id": "chatcmpl_fake", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "fake-model",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }]

    def _usage(self) -> dict:
        # 必须按请求体规模报 prompt_tokens：恒报 10 会让运行时永远看不到上下文压力，
        # 压缩车道根本不会被触发，P1 也就验不到。
        prompt = int(getattr(self, "_prompt_tokens", 10) or 10)
        return {"prompt_tokens": prompt, "completion_tokens": 2, "total_tokens": prompt + 2}

    def _text(self, text: str) -> None:
        chunks = self._chunks()
        chunks.append({
            "id": "chatcmpl_fake", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "fake-model",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        })
        chunks.append({
            "id": "chatcmpl_fake", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "fake-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        })
        chunks.append({
            "id": "chatcmpl_fake", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "fake-model",
            "choices": [], "usage": self._usage(),
        })
        self._sse(chunks)

    def _tool_call(self, name: str, arguments: dict) -> None:
        chunks = self._chunks()
        chunks.append({
            "id": "chatcmpl_fake", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "fake-model",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0, "id": f"call_{name}_{int(time.time() * 1000)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }]},
                "finish_reason": None,
            }],
        })
        chunks.append({
            "id": "chatcmpl_fake", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "fake-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        })
        chunks.append({
            "id": "chatcmpl_fake", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "fake-model",
            "choices": [], "usage": self._usage(),
        })
        self._sse(chunks)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def _exec_args(self) -> dict:
        return {"command": f"Get-Content -Path {BIG_FILE.as_posix()}", "timeout_seconds": 120}

    def _frontdoor_next(self, hop: int, last: str) -> None:
        if hop == 0:
            self._tool_call("exec", self._exec_args())
            return
        if hop == 1:
            _FRONT_STATE["ref"] = _extract_ref(last)
            self._tool_call("content_search", {"ref": _FRONT_STATE["ref"], "query": MIDDLE})
            return
        if hop == 2:
            self._tool_call("content_search", {"ref": _FRONT_STATE["ref"], "query": MIDDLE, "reuse_args": True})
            return
        if hop == 3:
            _FRONT_STATE["line"] = _extract_line(last)
            line = _FRONT_STATE["line"]
            self._tool_call("content_open", {"ref": _FRONT_STATE["ref"], "start_line": max(1, line - 3), "end_line": line + 3})
            return
        if hop == 4:
            line = _FRONT_STATE["line"]
            self._tool_call("content_open", {
                "ref": _FRONT_STATE["ref"], "start_line": max(1, line - 3), "end_line": line + 3, "reuse_args": True,
            })
            return
        if hop == 5:
            self._tool_call("content_open", {"path": HUGE_FILE.as_posix(), "start_line": 1, "end_line": 2})
            return
        if hop == 6:
            self._tool_call("content_open", {"path": HUGE_FILE.as_posix(), "start_line": 1, "end_line": 2, "reuse_args": True})
            return
        if hop == 7:
            self._tool_call("create_async_task", {
                "task": TASK_PROMPT,
                "core_requirement": "节点道复现 exec 全量 ref 与 content_search 取回中段。",
                "execution_policy": {"mode": "focus"},
            })
            return
        self._text(FINAL_TEXT)

    def _node_next(self, hop: int, last: str) -> None:
        # 压缩会把历史换成压缩块，请求里的 tool 回执数随之重置，所以节点道不按 hop
        # 号编排：自己维护步数，契约投递跳原参重发，真实回执才推进一步。
        # 24 次 content_open（每次约 8000 字符内联）把历史推过 26000 窗口，逼出压缩。
        state = _NODE_STATE
        if "result externalized" in last and not state["ref"]:
            state["ref"] = _extract_ref(last)
        is_payload = ('"excerpt"' in last) or ('"hits"' in last) or ("result externalized" in last)
        if last and not is_payload:
            args = dict(state.get("args") or {})
            args["reuse_args"] = True
            self._tool_call(str(state.get("tool") or "exec"), args)
            return
        if is_payload:
            state["step"] += 1
        step = state["step"]
        if step <= 0:
            tool, args = "exec", self._exec_args()
        elif step <= 24:
            start = (step - 1) * 200 + 1
            tool, args = "content_open", {"ref": state["ref"], "start_line": start, "end_line": start + 199}
        elif step == 25:
            tool, args = "content_search", {"ref": state["ref"], "query": MIDDLE}
        else:
            tool, args = "submit_final_result", {
                "summary": f"{MARKER} 节点道验收完成",
                "answer": "node lane full-output ref checks done",
                "evidence": [],
                "remaining_work": [],
            }
        state.update(tool=tool, args=args)
        self._tool_call(tool, args)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self._prompt_tokens = max(10, len(raw) // 4)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        tools = _tool_names(body)
        tool_messages = _tool_messages(body)
        last = _last_tool_text(body)
        lane = "node" if "submit_final_result" in tools else ("frontdoor" if "create_async_task" in tools else "other")
        whole = _body_text(body)
        messages = body.get("messages") or []
        last_user = next(
            (str(m.get("content") or "") for m in reversed(messages) if str(m.get("role") or "") == "user"),
            "",
        )
        is_compaction = "【上下文压缩指令】" in last_user
        if is_compaction:
            lane = "compaction"
        with REQUEST_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": time.time(),
                "lane": lane,
                "hop": len(tool_messages),
                "tools": tools,
                "last_tool_text": last[:8000],
                "has_ref_index": REF_INDEX_HEADING in whole,
                "ref_index_excerpt": (
                    whole[whole.index(REF_INDEX_HEADING):whole.index(REF_INDEX_HEADING) + 900]
                    if REF_INDEX_HEADING in whole else ""
                ),
            }, ensure_ascii=False) + "\n")
        if is_compaction:
            # 压缩助手要的是摘要正文，不是工具调用；回工具调用会被当成空摘要。
            self._text(f"{MARKER} 压缩摘要：已用 exec 取大输出，并按 ref 搜回中段哨兵。")
            return
        if lane == "node":
            self._node_next(len(tool_messages), last)
            return
        if lane == "frontdoor":
            self._frontdoor_next(len(tool_messages), last)
            return
        self._text("ok")


def _wait_for(predicate, timeout: float, interval: float = 0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _logs() -> list[dict]:
    if not REQUEST_LOG.exists():
        return []
    rows = []
    for line in REQUEST_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _lane_hop(lane: str, hop: int) -> str:
    for row in _logs():
        if row.get("lane") == lane and row.get("hop") == hop:
            return str(row.get("last_tool_text") or "")
    return ""


async def main() -> int:
    _prepare_files()
    REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    REQUEST_LOG.unlink(missing_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), _FakeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = httpx.Client(base_url=BASE, timeout=60.0, trust_env=False)
    session_id = ""
    task_id = ""
    routes_original = None
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))

    try:
        routes_original = client.get("/api/llm/routes").json()
        # 两个绑定分开窗口：前门 200000 不压缩（hop 编号才稳定），节点 26000 逼出压缩。
        for key, window in ((MODEL_KEY, 200000), (MODEL_KEY_NODE, 26000)):
            client.delete(f"/api/models/{key}")
            created = client.post("/api/models", json={
                "key": key,
                "provider_model": "openai:fake-model",
                "api_key": "test",
                "api_base": f"http://127.0.0.1:{FAKE_PORT}/v1",
                "enabled": True,
                "retry_on": [],
                "retry_count": 0,
                "context_window_tokens": window,
                "image_multimodal_enabled": False,
                "request_timeout_seconds": 120,
                "name": f"full output acceptance {key}",
            })
            check(f"register fake binding {key}", created.status_code == 200, created.text[:160])
            if created.status_code != 200:
                return 1
        routes_set = client.put("/api/llm/routes", json={"updates": {"execution": {
            "route_entries": [{"type": "model", "modelKey": MODEL_KEY_NODE}],
            "load_balance_groups": routes_original.get("load_balance_groups") or {},
        }}})
        check("pin global execution chain to fake", routes_set.status_code == 200, routes_set.text[:200])

        session_response = client.post("/api/ceo/sessions", json={"title": "full-output-acceptance"})
        session_id = str(session_response.json().get("active_session_id") or "")
        check("create session", bool(session_id))
        pinned = client.patch(f"/api/ceo/sessions/{session_id}/model-selection", json={"mode": "model", "model_key": MODEL_KEY})
        check("pin session to fake", pinned.status_code == 200)

        async with websockets.connect(f"{WS_BASE}?session_id={session_id}", max_size=None) as ws:
            await asyncio.sleep(0.8)
            await ws.send(json.dumps({
                "type": "client.user_message",
                "session_id": session_id,
                "text": f"{MARKER}: 请按脚本运行 exec 并用 content_search / content_open 取回中段。",
            }))
            flow_done = await asyncio.to_thread(_wait_for, lambda: _lane_hop("frontdoor", 8), 300)
        check("frontdoor reached the last scripted hop", bool(flow_done))

        exec_result = _lane_hop("frontdoor", 1)
        ref = _extract_ref(exec_result)
        check("exec result externalized with a ref", bool(ref), exec_result[:200])
        check("exec result does not claim the middle is lost", "中段未保留" not in exec_result)
        check("exec envelope reports full-output scale", "lines," in exec_result and "chars)" in exec_result)

        search_result = _lane_hop("frontdoor", 3)
        check("content_search(ref) finds the middle sentinel", MIDDLE in search_result, search_result[:200])
        line = _extract_line(search_result)
        check("search hit carries an absolute line number", line > 6000, str(line))

        opened = _lane_hop("frontdoor", 5)
        check("content_open(ref, around_line) returns the middle sentinel", MIDDLE in opened, opened[:200])
        check("excerpt carries the line gutter", f"{line}: " in opened)

        guard = _lane_hop("frontdoor", 7)
        check("oversized path target is refused", "navigation read limit" in guard, guard[:200])
        check("refusal gives an exec extraction way out", "rg -n" in guard and "exec" in guard)

        task_match = re.search(r"task:[0-9a-zA-Z]+", _lane_hop("frontdoor", 8))
        scripted_task_id = task_match.group(0) if task_match else ""
        check("frontdoor created a task", bool(scripted_task_id), scripted_task_id)
        if scripted_task_id:
            terminal = _wait_for(lambda: next(
                (item for item in client.get("/api/tasks", params={"session_id": "all", "scope": 1}).json().get("items", [])
                 if item.get("task_id") == scripted_task_id and item.get("status") in {"success", "failed", "unpassed"}), None), 300)
            task_id = str((terminal or {}).get("task_id") or "")
            check("node task reaches terminal", terminal is not None, str((terminal or {}).get("status")))
            check("node task success", (terminal or {}).get("status") == "success")

        node_search_rows = [
            row for row in _logs()
            if row.get("lane") == "node"
            and MIDDLE in str(row.get("last_tool_text") or "")
            and '"hits"' in str(row.get("last_tool_text") or "")
        ]
        check("node lane content_search(ref) finds the middle sentinel", bool(node_search_rows),
              str(node_search_rows[0].get("last_tool_text") if node_search_rows else "")[:200])

        index_rows = [row for row in _logs() if row.get("lane") == "node" and row.get("has_ref_index")]
        check("node compaction block carries the ref index", bool(index_rows),
              (index_rows[0].get("ref_index_excerpt") if index_rows else "compaction not triggered in this run")[:400])
        if index_rows:
            excerpt = str(index_rows[0].get("ref_index_excerpt") or "")
            check("ref index lists artifact refs", "artifact:" in excerpt)
            check("ref index states the on-demand rule", "按需才开" in excerpt)

        time.sleep(10)
    finally:
        if routes_original is not None:
            restore = client.put("/api/llm/routes", json={"updates": {"execution": {
                "route_entries": (routes_original.get("route_entries") or {}).get("execution") or [],
                "load_balance_groups": routes_original.get("load_balance_groups") or {},
            }}})
            print("restore execution route:", restore.status_code)
        print(f"session_id={session_id or '<none>'} task_id={task_id or '<none>'} (kept for maintainer review)")
        print(f"fake provider log: {REQUEST_LOG}")

    failed = [name for name, ok, _detail in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
