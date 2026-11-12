"""Live acceptance for the per-task model chain override (task-model-config).

Runs against the local Negi web instance on 127.0.0.1:18790 with a temporary
OpenAI-compatible fake provider:

1. register a fake binding and a temporary load-balance group whose only member
   is that binding; NO global role chain references the group;
2. a fake front door creates one real execution task (no acceptance node);
3. pause the task, save a task-level execution override pointing at the group,
   resume; the worker must admit the node through the group (the registration
   surface covers the whole config, not only global-chain groups) and the fake
   member serves the turn and submits the final result;
4. assert the model-call ledger carries the group key and the fake binding key,
   and that task-panel writes never touch config.json;
5. restore everything: override cleared, task/session deleted, routes and group
   dictionary put back, fake binding deleted, config bytes identical to start.

Usage: .venv/Scripts/python.exe work/live_task_model_config_acceptance.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from g3ku.utils.net_env import normalize_no_proxy_env

# 与运行入口同一口径：NO_PROXY 里的 [::1] 会让 httpx 构造抛 Invalid port。
normalize_no_proxy_env()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:18790"
WS_BASE = "ws://127.0.0.1:18790/api/ws/ceo"
MODEL_KEY = "tmc-acceptance-model"
GROUP_KEY = "tmc-acceptance-grp"
FAKE_PORT = 18899
MARKER = "TMC-OVERRIDE-ACCEPTANCE"
REQUEST_LOG = pathlib.Path("work/tmc-acceptance-requests.jsonl")
DATA_ROOT_FILE = pathlib.Path(".g3ku/data-root.json")
CONFIG_FILE = pathlib.Path(".g3ku/config.json")
TASK_PROMPT = (
    f"{MARKER}: 这是任务级模型链覆盖验收任务。收到本提示后直接调用 "
    "submit_final_result 提交最终结果，summary 写覆盖验收完成。"
)


def _data_root() -> pathlib.Path:
    payload = json.loads(DATA_ROOT_FILE.read_text(encoding="utf-8"))
    return pathlib.Path(payload["data_dir"]).resolve()


def _db_rows(query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(_data_root() / ".g3ku" / "main-runtime" / "runtime.sqlite3"))
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _content(body: dict) -> str:
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = body.get("input") if isinstance(body.get("input"), list) else []
    parts: list[str] = []
    for item in messages:
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            parts.append(item["content"])
    return "\n".join(parts)


def _tool_names(body: dict) -> list[str]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return []
    names = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn["name"]))
    return names


def _has_tool_result(body: dict) -> bool:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    return any(isinstance(item, dict) and item.get("role") == "tool" for item in messages)


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
        return {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}

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
                    "index": 0, "id": f"call_{name}_{int(time.time())}", "type": "function",
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

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        tools = _tool_names(body)
        with REQUEST_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), "tools": tools}, ensure_ascii=False) + "\n")
        if "create_async_task" in tools:
            if _has_tool_result(body):
                self._text("任务已创建。")
            else:
                self._tool_call("create_async_task", {
                    "task": TASK_PROMPT,
                    "core_requirement": "验证任务级模型链覆盖经负载均衡组准入生效。",
                    "execution_policy": {"mode": "focus"},
                })
            return
        if "submit_final_result" in tools:
            self._tool_call("submit_final_result", {
                "summary": f"{MARKER} 覆盖验收完成",
                "answer": "override served through load-balance group",
            })
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


async def _recv_final(ws, text: str, timeout: float):
    deadline = time.monotonic() + timeout
    frames = []
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
        frame = json.loads(raw)
        frames.append(frame)
        kind = str(frame.get("type") or "")
        if kind in {"ceo.turn.final", "ceo.final", "message_end", "ceo.message_end"}:
            return frames, frame
        if text in json.dumps(frame, ensure_ascii=False):
            return frames, frame
    return frames, None


async def main() -> int:
    REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    REQUEST_LOG.unlink(missing_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), _FakeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = httpx.Client(base_url=BASE, timeout=60.0)
    config_original = CONFIG_FILE.read_bytes()
    session_id = ""
    task_id = ""
    routes_original = None
    binding_created = False
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok)))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))

    try:
        routes = client.get("/api/llm/routes")
        routes.raise_for_status()
        routes_original = routes.json()
        original_entries = routes_original.get("route_entries") or {}
        original_groups = routes_original.get("load_balance_groups") or {}

        client.delete(f"/api/models/{MODEL_KEY}")
        created = client.post("/api/models", json={
            "key": MODEL_KEY,
            "provider_model": "openai:fake-model",
            "api_key": "test",
            "api_base": f"http://127.0.0.1:{FAKE_PORT}/v1",
            "enabled": True,
            "retry_on": [],
            "retry_count": 0,
            "context_window_tokens": 200000,
            "image_multimodal_enabled": False,
            "request_timeout_seconds": 90,
            "name": "task model config acceptance provider",
        })
        check("register fake binding", created.status_code == 200, created.text[:200])
        if created.status_code != 200:
            return 1
        binding_created = True

        groups_with_test = dict(original_groups)
        groups_with_test[GROUP_KEY] = {"enabled": True, "max_retry_rounds": 1, "model_keys": [MODEL_KEY]}
        put = client.put("/api/llm/routes", json={"updates": {"execution": {
            "route_entries": original_entries.get("execution") or [],
            "load_balance_groups": groups_with_test,
        }}})
        check("register unreferenced group via model-page lane", put.status_code == 200, put.text[:200])
        if put.status_code != 200:
            return 1
        config_after_setup = CONFIG_FILE.read_bytes()

        session_response = client.post("/api/ceo/sessions", json={"title": "tmc-override-acceptance"})
        session_id = str(session_response.json().get("active_session_id") or "")
        check("create session", bool(session_id))
        pinned = client.patch(f"/api/ceo/sessions/{session_id}/model-selection", json={"mode": "model", "model_key": MODEL_KEY})
        check("pin session to fake", pinned.status_code == 200)

        async with websockets.connect(f"{WS_BASE}?session_id={session_id}", max_size=None) as ws:
            await asyncio.sleep(0.8)
            await ws.send(json.dumps({"type": "client.user_message", "session_id": session_id, "text": f"请创建验收任务 {MARKER}。"}))
            frames, final = await _recv_final(ws, "任务已创建", 90)
        check("frontdoor created task", final is not None)

        task = _wait_for(lambda: next((item for item in client.get("/api/tasks", params={"session_id": "all", "scope": 1}).json().get("items", []) if MARKER in str(item.get("title") or "") or MARKER in str(item.get("user_request") or "")), None), 30)
        task_id = str((task or {}).get("task_id") or "")
        check("task visible", bool(task_id), task_id)
        if not task_id:
            return 1

        paused = client.post(f"/api/tasks/{task_id}/pause")
        check("pause task", paused.status_code == 200)
        _wait_for(lambda: not client.get(f"/api/tasks/{task_id}/pause-state").json().get("item", {}).get("draining", False), 30)

        override = client.patch(f"/api/tasks/{task_id}/model-config", json={
            "execution_route_entries": [{"type": "load_balance", "group_key": GROUP_KEY}],
        })
        check("save task override", override.status_code == 200, override.text[:200])
        got = client.get(f"/api/tasks/{task_id}/model-config").json()
        check("override stored", (got.get("execution") or {}).get("follows_global") is False)
        check("task writes do not touch config", CONFIG_FILE.read_bytes() == config_after_setup)

        resumed = client.post(f"/api/tasks/{task_id}/resume")
        check("resume task", resumed.status_code == 200)
        terminal = _wait_for(lambda: next((item for item in client.get("/api/tasks", params={"session_id": "all", "scope": 1}).json().get("items", []) if item.get("task_id") == task_id and item.get("status") in {"success", "failed", "unpassed"}), None), 120)
        check("task reaches terminal", terminal is not None, str((terminal or {}).get("status")))
        check("task success", (terminal or {}).get("status") == "success")

        calls = _wait_for(lambda: [json.loads(row[0]) for row in _db_rows("select payload_json from task_model_calls where task_id=? order by seq", (task_id,))], 20) or []

        def _served_by_fake(call: dict) -> bool:
            return any(str(m.get("model_key") or "") == MODEL_KEY for m in call.get("delta_usage_by_model") or [])

        group_calls = [c for c in calls if str(c.get("route_served_group") or "") == GROUP_KEY]
        fake_calls = [c for c in calls if _served_by_fake(c)]
        check("ledger shows group admission", bool(group_calls), f"{len(group_calls)} call(s)")
        check("ledger shows fake member served", bool(fake_calls), f"{len(fake_calls)} call(s)")
        check("last call served by override", bool(calls) and _served_by_fake(calls[-1]))
        if not calls and "--keep" in sys.argv:
            detail = client.get(f"/api/tasks/{task_id}").json()
            root_id = str((detail.get("task") or {}).get("root_node_id") or "")
            ledger = client.get(f"/api/tasks/{task_id}/token-ledger", params={"limit": 20}).json()
            print("DIAG root:", root_id)
            print("DIAG ledger keys:", list(ledger.keys()))
            print("DIAG ledger items:", json.dumps(ledger.get("items") or ledger.get("rows") or [], ensure_ascii=False)[:600])
            print("DIAG node rows:", _db_rows("select node_id, seq from task_model_calls where task_id=?", (task_id,)))
            print("DIAG any recent:", _db_rows("select task_id, node_id, seq from task_model_calls order by rowid desc limit 3"))
            print("DIAG task token_usage:", json.dumps((detail.get("task") or {}).get("token_usage") or {}, ensure_ascii=False)[:300])

        cleared = client.patch(f"/api/tasks/{task_id}/model-config", json={"execution_route_entries": []})
        check("clear override", cleared.status_code == 200)
        got = client.get(f"/api/tasks/{task_id}/model-config").json()
        check("follows global restored", got.get("execution", {}).get("follows_global") is True)
        check("config still untouched by panel writes", CONFIG_FILE.read_bytes() == config_after_setup)
    finally:
        if task_id and "--keep" not in sys.argv:
            client.delete(f"/api/tasks/{task_id}")
        if session_id:
            client.delete(f"/api/ceo/sessions/{session_id}")
        if routes_original is not None:
            restore = client.put("/api/llm/routes", json={"updates": {"execution": {
                "route_entries": (routes_original.get("route_entries") or {}).get("execution") or [],
                "load_balance_groups": routes_original.get("load_balance_groups") or {},
            }}})
            print("restore routes:", restore.status_code)
        if binding_created:
            print("delete fake binding:", client.delete(f"/api/models/{MODEL_KEY}").status_code)
        server.shutdown()
        client.close()

    check("config bytes restored to start", CONFIG_FILE.read_bytes() == config_original)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("failed:", "; ".join(failed))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))