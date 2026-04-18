# Async Task Duplicate Precheck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow one CEO turn to create multiple different async tasks while blocking duplicate task creation against the current session's unfinished task pool through rule-first precheck plus LLM fuzzy review.

**Architecture:** `create_async_task` stays the only CEO/frontdoor task-spawn tool, but task creation becomes guarded by a service-owned precheck in `MainRuntimeService`. The precheck first performs conservative exact duplicate checks on normalized target text and exact keyword fingerprints, then optionally invokes an inspection-model review that can return `approve_new`, `reject_duplicate`, or `reject_use_append_notice`. Frontdoor dispatch bookkeeping must stop inferring success from any visible `task:` substring and instead treat only explicit create-success results as verified dispatches, so rejected duplicate/create-update cases do not become fake `task_dispatch` turns.

**Tech Stack:** Python, pytest, MainRuntimeService, CEO/frontdoor runtime, LangGraph frontdoor runner, prompt-based inspection model chain, architecture docs in `docs/architecture/`.

---

## Implementation Units

- `main/service/runtime_service.py`
  - Add service-owned async-task duplicate precheck helpers.
  - Integrate precheck into `CreateAsyncTaskTool.execute(...)`.
  - Reuse the existing inspection-model infrastructure pattern used by task governance review.
- `main/prompts/async_task_duplicate_precheck.md`
  - Add the inspection prompt that decides between `approve_new`, `reject_duplicate`, and `reject_use_append_notice`.
- `g3ku/runtime/frontdoor/_ceo_runtime_ops.py`
  - Add a parser for `create_async_task` tool results that distinguishes real create success from rejection text.
  - Make `route_kind=task_dispatch` depend on verified created task ids instead of raw tool usage.
- `g3ku/runtime/frontdoor/_ceo_create_agent_impl.py`
  - Keep compatibility-path postprocessing aligned with the explicit graph path.
  - Accumulate multiple verified task ids in one turn.
- `g3ku/runtime/session_agent.py`
  - Preserve multiple verified task ids from the frontdoor turn into assistant metadata.
- `tests/resources/test_async_task_duplicate_precheck.py`
  - New focused service-level tests for rule review and LLM review behavior.
- `tests/resources/test_tool_resource_admin_api.py`
  - Extend `create_async_task` tool tests for rejection text and append-notice guidance.
- `tests/resources/test_ceo_create_agent_runner.py`
  - Extend frontdoor postprocess tests so rejected duplicate messages with old `task_id` do not count as successful dispatch.
- `docs/architecture/runtime-overview.md`
  - Document the new service-layer duplicate precheck and the new “append notice instead of new task” boundary.
- `docs/architecture/tool-and-skill-system.md`
  - Document the updated `create_async_task` contract and rejection behavior.

### Task 1: Add Service-Owned Rule Precheck For Exact Duplicates

**Files:**
- Modify: `main/service/runtime_service.py`
- Create: `tests/resources/test_async_task_duplicate_precheck.py`

- [ ] **Step 1: Write failing tests for exact duplicate, paused-task coverage, and non-duplicate allowance**

```python
import pytest

from pathlib import Path
from types import SimpleNamespace

from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, *args, **kwargs):
        raise AssertionError("LLM review should not run in rule-only tests")


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


@pytest.mark.asyncio
async def test_precheck_rejects_exact_core_requirement_duplicate(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        first = await service.create_task(
            "整理本周用户投诉并给出处理建议",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理本周用户投诉并给出处理建议",
                "execution_policy": {"mode": "focus"},
            },
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="整理本周用户投诉并给出处理建议",
            core_requirement="整理本周用户投诉并给出处理建议",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "reject_duplicate"
        assert decision["matched_task_id"] == first.task_id
        assert decision["decision_source"] == "rule"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_includes_paused_tasks_in_duplicate_pool(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        first = await service.create_task(
            "整理北美客户续费流失原因",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理北美客户续费流失原因",
                "execution_policy": {"mode": "focus"},
            },
        )
        await service.pause_task(first.task_id)

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="整理北美客户续费流失原因",
            core_requirement="整理北美客户续费流失原因",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "reject_duplicate"
        assert decision["matched_task_id"] == first.task_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_allows_distinct_new_task(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        await service.create_task(
            "整理北美客户续费流失原因",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理北美客户续费流失原因",
                "execution_policy": {"mode": "focus"},
            },
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="设计续费流失挽回实验方案",
            core_requirement="设计续费流失挽回实验方案",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "approve_new"
        assert decision["matched_task_id"] == ""
        assert decision["decision_source"] == "rule"
    finally:
        await service.close()
```

- [ ] **Step 2: Run the new service tests to verify they fail before implementation**

Run:

```bash
python -m pytest tests/resources/test_async_task_duplicate_precheck.py -k "exact_core_requirement_duplicate or paused_tasks_in_duplicate_pool or distinct_new_task" -q
```

Expected: FAIL with `AttributeError: 'MainRuntimeService' object has no attribute 'precheck_async_task_creation'`.

- [ ] **Step 3: Implement normalization, keyword fingerprinting, unfinished-task pool selection, and exact-match rule review in `MainRuntimeService`**

```python
import re


def _normalize_async_task_target_text(self, value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    text = text.replace("，", ",").replace("。", ".").replace("：", ":")
    return text


def _async_task_keyword_fingerprint(self, value: Any) -> tuple[str, ...]:
    normalized = self._normalize_async_task_target_text(value)
    if not normalized:
        return ()
    tokens = re.findall(r"[a-z0-9_:/.-]+|[\u4e00-\u9fff]+", normalized)
    unique_tokens: list[str] = []
    for token in tokens:
        cleaned = token.strip()
        if len(cleaned) <= 1 or cleaned in unique_tokens:
            continue
        unique_tokens.append(cleaned)
    return tuple(unique_tokens)


def _async_task_precheck_pool(self, session_id: str) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    for task in self.list_tasks_for_session(session_id):
        status = str(getattr(task, "status", "") or "").strip().lower()
        if status != "in_progress":
            continue
        metadata = dict(getattr(task, "metadata", {}) or {})
        target_text = str(metadata.get("core_requirement") or getattr(task, "user_request", "") or "").strip()
        pool.append(
            {
                "task_id": str(getattr(task, "task_id", "") or "").strip(),
                "status": status,
                "is_paused": bool(getattr(task, "is_paused", False)),
                "task_text": str(getattr(task, "user_request", "") or "").strip(),
                "core_requirement": str(metadata.get("core_requirement") or "").strip(),
                "execution_policy": dict(metadata.get("execution_policy") or {}),
                "target_text": self._normalize_async_task_target_text(target_text),
                "keyword_fingerprint": self._async_task_keyword_fingerprint(target_text),
            }
        )
    return pool


def _rule_precheck_async_task_creation(
    self,
    *,
    session_id: str,
    task_text: str,
    core_requirement: str,
    execution_policy: dict[str, Any],
    requires_final_acceptance: bool,
    final_acceptance_prompt: str,
) -> dict[str, Any]:
    _ = execution_policy, requires_final_acceptance, final_acceptance_prompt
    candidate_target = self._normalize_async_task_target_text(core_requirement or task_text)
    candidate_keywords = self._async_task_keyword_fingerprint(core_requirement or task_text)
    for item in self._async_task_precheck_pool(session_id):
        if candidate_target and candidate_target == item["target_text"]:
            return {
                "decision": "reject_duplicate",
                "matched_task_id": item["task_id"],
                "reason": "core_requirement exact match",
                "decision_source": "rule",
            }
        if candidate_keywords and candidate_keywords == item["keyword_fingerprint"]:
            return {
                "decision": "reject_duplicate",
                "matched_task_id": item["task_id"],
                "reason": "keyword fingerprint exact match",
                "decision_source": "rule",
            }
    return {
        "decision": "approve_new",
        "matched_task_id": "",
        "reason": "rule precheck found no exact duplicate",
        "decision_source": "rule",
    }
```

- [ ] **Step 4: Add the public async precheck entrypoint that returns `approve_new` when the rule layer finds no exact duplicate**

```python
async def precheck_async_task_creation(
    self,
    *,
    session_id: str,
    task_text: str,
    core_requirement: str,
    execution_policy: dict[str, Any] | None,
    requires_final_acceptance: bool,
    final_acceptance_prompt: str,
) -> dict[str, Any]:
    rule_decision = self._rule_precheck_async_task_creation(
        session_id=session_id,
        task_text=task_text,
        core_requirement=core_requirement,
        execution_policy=dict(execution_policy or {}),
        requires_final_acceptance=bool(requires_final_acceptance),
        final_acceptance_prompt=str(final_acceptance_prompt or "").strip(),
    )
    if str(rule_decision.get("decision") or "") != "approve_new":
        return rule_decision
    return rule_decision
```

- [ ] **Step 5: Re-run the service tests and confirm rule-only cases pass**

Run:

```bash
python -m pytest tests/resources/test_async_task_duplicate_precheck.py -k "exact_core_requirement_duplicate or paused_tasks_in_duplicate_pool or distinct_new_task" -q
```

Expected: PASS.

- [ ] **Step 6: Commit the exact-rule precheck baseline**

```bash
git add main/service/runtime_service.py tests/resources/test_async_task_duplicate_precheck.py
git commit -m "feat: add exact duplicate precheck for async task creation"
```

### Task 2: Add LLM Fuzzy Review And Append-Notice Decision

**Files:**
- Modify: `main/service/runtime_service.py`
- Create: `main/prompts/async_task_duplicate_precheck.md`
- Modify: `tests/resources/test_async_task_duplicate_precheck.py`

- [ ] **Step 1: Extend the test file with LLM review approval, duplicate rejection, append-notice rejection, and model-failure fallback cases**

```python
class _ReviewBackend:
    def __init__(self, response):
        self._response = response

    async def chat(self, *args, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_precheck_uses_llm_to_reject_fuzzy_duplicate(tmp_path: Path):
    response = SimpleNamespace(
        tool_calls=[
            {
                "name": "review_async_task_duplicate_precheck",
                "arguments": {
                    "decision": "reject_duplicate",
                    "matched_task_id": "task:demo-existing",
                    "reason": "same goal and deliverable as existing task",
                },
            }
        ],
        content="",
    )
    service = MainRuntimeService(
        chat_backend=_ReviewBackend(response),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        first = await service.create_task(
            "整理重点客户流失信号",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理重点客户流失信号",
                "execution_policy": {"mode": "focus"},
            },
        )
        service.store.update_task(first.task_id, metadata={**first.metadata, "shadow_task_id": "task:demo-existing"})

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="汇总重点客户流失预警并整理结论",
            core_requirement="汇总重点客户流失预警并整理结论",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "reject_duplicate"
        assert decision["matched_task_id"] == "task:demo-existing"
        assert decision["decision_source"] == "llm"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_returns_append_notice_decision_when_old_task_needs_new_constraints(tmp_path: Path):
    response = SimpleNamespace(
        tool_calls=[
            {
                "name": "review_async_task_duplicate_precheck",
                "arguments": {
                    "decision": "reject_use_append_notice",
                    "matched_task_id": "task:demo-existing",
                    "reason": "existing task should receive the new acceptance constraint",
                },
            }
        ],
        content="",
    )
    service = MainRuntimeService(
        chat_backend=_ReviewBackend(response),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        await service.create_task(
            "整理重点客户流失信号",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理重点客户流失信号",
                "execution_policy": {"mode": "focus"},
            },
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="整理重点客户流失信号并新增董事会验收格式",
            core_requirement="整理重点客户流失信号并新增董事会验收格式",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=True,
            final_acceptance_prompt="必须按董事会模板输出",
        )

        assert decision["decision"] == "reject_use_append_notice"
        assert decision["matched_task_id"] == "task:demo-existing"
        assert decision["decision_source"] == "llm"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_precheck_falls_back_to_approve_when_llm_review_is_unavailable(tmp_path: Path):
    class _BrokenBackend:
        async def chat(self, *args, **kwargs):
            raise RuntimeError("inspection backend unavailable")

    service = MainRuntimeService(
        chat_backend=_BrokenBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        await service.create_task(
            "整理重点客户流失信号",
            session_id="web:ceo-demo",
            metadata={
                "core_requirement": "整理重点客户流失信号",
                "execution_policy": {"mode": "focus"},
            },
        )

        decision = await service.precheck_async_task_creation(
            session_id="web:ceo-demo",
            task_text="汇总重点客户流失预警并整理结论",
            core_requirement="汇总重点客户流失预警并整理结论",
            execution_policy={"mode": "focus"},
            requires_final_acceptance=False,
            final_acceptance_prompt="",
        )

        assert decision["decision"] == "approve_new"
        assert decision["decision_source"] == "fallback"
    finally:
        await service.close()
```

- [ ] **Step 2: Run the LLM-review tests and verify they fail before the new review path exists**

Run:

```bash
python -m pytest tests/resources/test_async_task_duplicate_precheck.py -k "fuzzy_duplicate or append_notice_decision or llm_review_is_unavailable" -q
```

Expected: FAIL because the service never calls an LLM review and always returns `approve_new`.

- [ ] **Step 3: Add the inspection prompt file for async-task duplicate review**

```md
You are reviewing whether a newly requested async task should be created.

You must return exactly one tool call to `review_async_task_duplicate_precheck`.

Decision rules:
- `approve_new`: the candidate task is meaningfully different from all unfinished session tasks.
- `reject_duplicate`: the candidate task is effectively the same job as an unfinished session task.
- `reject_use_append_notice`: the candidate task is not a truly new job; it is the old task plus new constraints, acceptance details, or updated requirements, so the system should update the existing task instead of creating a new one.

Never choose `reject_duplicate` or `reject_use_append_notice` without naming the matched task id.
Prefer `approve_new` when evidence is weak.
```

- [ ] **Step 4: Implement the LLM review executor and parser in `MainRuntimeService` using the task-governance-review pattern**

```python
def _unfinished_async_task_review_payload(self, session_id: str) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in self._async_task_precheck_pool(session_id):
        payload.append(
            {
                "task_id": item["task_id"],
                "task_text": item["task_text"],
                "core_requirement": item["core_requirement"],
                "execution_policy": item["execution_policy"],
                "status": item["status"],
                "is_paused": item["is_paused"],
            }
        )
    return payload


def _parse_async_task_duplicate_precheck_response(self, response: Any) -> dict[str, Any] | None:
    tool_calls = list(getattr(response, "tool_calls", []) or [])
    for call in tool_calls:
        name = str((call or {}).get("name") or "").strip() if isinstance(call, dict) else str(getattr(call, "name", "") or "").strip()
        arguments = (call or {}).get("arguments") if isinstance(call, dict) else getattr(call, "arguments", None)
        if name != "review_async_task_duplicate_precheck":
            continue
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            return None
        decision = str(arguments.get("decision") or "").strip()
        if decision not in {"approve_new", "reject_duplicate", "reject_use_append_notice"}:
            return None
        matched_task_id = str(arguments.get("matched_task_id") or "").strip()
        if decision != "approve_new" and not matched_task_id.startswith("task:"):
            return None
        return {
            "decision": decision,
            "matched_task_id": matched_task_id,
            "reason": str(arguments.get("reason") or "").strip(),
            "decision_source": "llm",
        }
    return None


async def _execute_async_task_duplicate_precheck_review(self, *, session_id: str, task_text: str, core_requirement: str, execution_policy: dict[str, Any], requires_final_acceptance: bool, final_acceptance_prompt: str) -> dict[str, Any] | None:
    model_refs = list(self.node_runner._acceptance_model_refs or self.node_runner._execution_model_refs)
    backend = self._chat_backend
    if not model_refs or backend is None or not callable(getattr(backend, "chat", None)):
        return None
    response = await backend.chat(
        messages=[
            {"role": "system", "content": load_prompt("async_task_duplicate_precheck.md").strip()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "candidate_task": {
                            "task_text": task_text,
                            "core_requirement": core_requirement,
                            "execution_policy": dict(execution_policy or {}),
                            "requires_final_acceptance": bool(requires_final_acceptance),
                            "final_acceptance_prompt": final_acceptance_prompt,
                        },
                        "unfinished_session_tasks": self._unfinished_async_task_review_payload(session_id),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "review_async_task_duplicate_precheck",
                    "description": "Decide whether a new async task should be created or blocked.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "decision": {
                                "type": "string",
                                "enum": ["approve_new", "reject_duplicate", "reject_use_append_notice"],
                            },
                            "matched_task_id": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["decision", "reason"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        model_refs=model_refs,
    )
    return self._parse_async_task_duplicate_precheck_response(response)
```

- [ ] **Step 5: Wire the public precheck entrypoint to call the LLM review only when the exact-rule layer approves and the unfinished-task pool is not empty**

```python
async def precheck_async_task_creation(
    self,
    *,
    session_id: str,
    task_text: str,
    core_requirement: str,
    execution_policy: dict[str, Any] | None,
    requires_final_acceptance: bool,
    final_acceptance_prompt: str,
) -> dict[str, Any]:
    rule_decision = self._rule_precheck_async_task_creation(
        session_id=session_id,
        task_text=task_text,
        core_requirement=core_requirement,
        execution_policy=dict(execution_policy or {}),
        requires_final_acceptance=bool(requires_final_acceptance),
        final_acceptance_prompt=str(final_acceptance_prompt or "").strip(),
    )
    if str(rule_decision.get("decision") or "") != "approve_new":
        return rule_decision
    if not self._async_task_precheck_pool(session_id):
        return rule_decision
    try:
        llm_decision = await self._execute_async_task_duplicate_precheck_review(
            session_id=session_id,
            task_text=task_text,
            core_requirement=core_requirement,
            execution_policy=dict(execution_policy or {}),
            requires_final_acceptance=bool(requires_final_acceptance),
            final_acceptance_prompt=str(final_acceptance_prompt or "").strip(),
        )
    except Exception:
        llm_decision = None
    if llm_decision is None:
        return {
            "decision": "approve_new",
            "matched_task_id": "",
            "reason": "llm review unavailable; allow new task",
            "decision_source": "fallback",
        }
    return llm_decision
```

- [ ] **Step 6: Re-run the service review tests and confirm all new decisions pass**

Run:

```bash
python -m pytest tests/resources/test_async_task_duplicate_precheck.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the LLM-review layer and prompt**

```bash
git add main/service/runtime_service.py main/prompts/async_task_duplicate_precheck.md tests/resources/test_async_task_duplicate_precheck.py
git commit -m "feat: add llm async task duplicate review"
```

### Task 3: Integrate Precheck Into `create_async_task` And Return Clear Rejection Text

**Files:**
- Modify: `main/service/runtime_service.py`
- Modify: `tests/resources/test_tool_resource_admin_api.py`

- [ ] **Step 1: Extend `create_async_task` tool tests for duplicate rejection text and append-notice guidance**

```python
@pytest.mark.asyncio
async def test_create_async_task_tool_returns_duplicate_rejection_text(monkeypatch):
    captured: dict[str, object] = {}

    class _StubService:
        async def precheck_async_task_creation(self, **kwargs):
            captured["precheck"] = dict(kwargs)
            return {
                "decision": "reject_duplicate",
                "matched_task_id": "task:existing-1",
                "reason": "core_requirement exact match",
                "decision_source": "rule",
            }

        async def create_task(self, *args, **kwargs):
            raise AssertionError("create_task should not run for duplicate rejection")

    tool = CreateAsyncTaskTool(_StubService())
    result = await tool.execute(
        "整理重点客户流失信号",
        core_requirement="整理重点客户流失信号",
        execution_policy={"mode": "focus"},
        __g3ku_runtime={"session_key": "web:ceo-demo"},
    )

    assert "任务未创建" in result
    assert "task:existing-1" in result
    assert "高度重复" in result
    assert captured["precheck"]["session_id"] == "web:ceo-demo"


@pytest.mark.asyncio
async def test_create_async_task_tool_returns_append_notice_guidance(monkeypatch):
    class _StubService:
        async def precheck_async_task_creation(self, **kwargs):
            return {
                "decision": "reject_use_append_notice",
                "matched_task_id": "task:existing-2",
                "reason": "existing task only needs the new acceptance constraint",
                "decision_source": "llm",
            }

        async def create_task(self, *args, **kwargs):
            raise AssertionError("create_task should not run for append-notice rejection")

    tool = CreateAsyncTaskTool(_StubService())
    result = await tool.execute(
        "整理重点客户流失信号并新增董事会验收格式",
        core_requirement="整理重点客户流失信号并新增董事会验收格式",
        execution_policy={"mode": "focus"},
        requires_final_acceptance=True,
        final_acceptance_prompt="必须按董事会模板输出",
        __g3ku_runtime={"session_key": "web:ceo-demo"},
    )

    assert "任务未创建" in result
    assert "task:existing-2" in result
    assert "追加通知" in result
```

- [ ] **Step 2: Run the tool tests to verify they fail before the tool calls precheck**

Run:

```bash
python -m pytest tests/resources/test_tool_resource_admin_api.py -k "duplicate_rejection_text or append_notice_guidance" -q
```

Expected: FAIL because the tool currently always calls `create_task(...)`.

- [ ] **Step 3: Update `CreateAsyncTaskTool.execute(...)` to invoke the service precheck before `create_task(...)` and render three distinct result shapes**

```python
async def execute(
    self,
    task: str,
    core_requirement: str = "",
    __g3ku_runtime: dict[str, Any] | None = None,
    **kwargs: Any,
) -> str:
    runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
    session_id = str(runtime.get("session_key") or "web:shared").strip() or "web:shared"
    normalized_core_requirement = str(core_requirement or kwargs.get("core_requirement") or "").strip() or str(task or "").strip()
    normalized_execution_policy = normalize_execution_policy_metadata(kwargs.get("execution_policy"))
    final_acceptance_prompt = str(kwargs.get("final_acceptance_prompt") or "").strip()
    raw_requires_final_acceptance = kwargs.get("requires_final_acceptance")
    requires_final_acceptance = bool(raw_requires_final_acceptance) or (
        raw_requires_final_acceptance in (None, "") and bool(final_acceptance_prompt)
    )

    precheck = await self._service.precheck_async_task_creation(
        session_id=session_id,
        task_text=str(task or "").strip(),
        core_requirement=normalized_core_requirement,
        execution_policy=normalized_execution_policy.model_dump(mode="json"),
        requires_final_acceptance=requires_final_acceptance,
        final_acceptance_prompt=final_acceptance_prompt,
    )
    decision = str(precheck.get("decision") or "").strip()
    matched_task_id = str(precheck.get("matched_task_id") or "").strip()
    reason = str(precheck.get("reason") or "").strip()

    if decision == "reject_duplicate":
        return f"任务未创建：与进行中任务 {matched_task_id} 高度重复。原因：{reason}"
    if decision == "reject_use_append_notice":
        return f"任务未创建：现有任务 {matched_task_id} 需要追加通知而不是新建。原因：{reason}"

    record = await self._service.create_task(
        str(task or ""),
        session_id=session_id,
        max_depth=explicit_max_depth,
        metadata={
            "core_requirement": normalized_core_requirement,
            "execution_policy": normalized_execution_policy.model_dump(mode="json"),
            "final_acceptance": {
                "required": requires_final_acceptance,
                "prompt": final_acceptance_prompt,
                "node_id": "",
                "status": "pending",
            },
        },
    )
    return f"创建任务成功{record.task_id}"
```

- [ ] **Step 4: Re-run the tool tests and confirm success / duplicate / append-notice paths are all covered**

Run:

```bash
python -m pytest tests/resources/test_tool_resource_admin_api.py -k "create_async_task_tool_" -q
```

Expected: PASS.

- [ ] **Step 5: Commit the tool integration**

```bash
git add main/service/runtime_service.py tests/resources/test_tool_resource_admin_api.py
git commit -m "feat: gate create_async_task behind duplicate precheck"
```

### Task 4: Make Frontdoor Dispatch Verification Depend On Explicit Create Success

**Files:**
- Modify: `g3ku/runtime/frontdoor/_ceo_runtime_ops.py`
- Modify: `g3ku/runtime/frontdoor/_ceo_create_agent_impl.py`
- Modify: `tests/resources/test_ceo_create_agent_runner.py`

- [ ] **Step 1: Add failing frontdoor tests for duplicate rejection-with-task-id and multiple successful task creations in one turn**

```python
@pytest.mark.asyncio
async def test_create_agent_postprocess_duplicate_rejection_with_old_task_id_does_not_count_as_dispatch():
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "create_async_task",
                    "content": "任务未创建：与进行中任务 task:demo-123 高度重复。原因：core_requirement exact match",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["create_async_task"],
        }
    )

    assert result["verified_task_ids"] == []
    assert result["route_kind"] == "direct_reply"
    assert "repair_overlay_text" not in result


@pytest.mark.asyncio
async def test_create_agent_postprocess_accumulates_multiple_verified_task_ids():
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo-1"}},
                {"id": "call-2", "name": "create_async_task", "arguments": {"task": "demo-2"}},
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call-1", "type": "function", "function": {"name": "create_async_task", "arguments": "{}"}},
                        {"id": "call-2", "type": "function", "function": {"name": "create_async_task", "arguments": "{}"}},
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "name": "create_async_task", "content": "创建任务成功task:demo-123"},
                {"role": "tool", "tool_call_id": "call-2", "name": "create_async_task", "content": "创建任务成功task:demo-456"},
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["create_async_task"],
        }
    )

    assert result["verified_task_ids"] == ["task:demo-123", "task:demo-456"]
    assert result["route_kind"] == "task_dispatch"
```

- [ ] **Step 2: Run the frontdoor tests to verify current dispatch bookkeeping misclassifies rejected create calls**

Run:

```bash
python -m pytest tests/resources/test_ceo_create_agent_runner.py -k "duplicate_rejection_with_old_task_id or accumulates_multiple_verified_task_ids" -q
```

Expected: FAIL because the current postprocess path treats any visible `task:` as eligible for dispatch verification and only keeps the first verified task id.

- [ ] **Step 3: Add a helper that parses `create_async_task` tool results by explicit success text instead of generic `task:` scanning**

```python
def _parse_create_async_task_result(self, result_text: str) -> dict[str, Any]:
    text = str(result_text or "").strip()
    if text.startswith("创建任务成功"):
        return {
            "created": True,
            "created_task_ids": self._normalize_task_ids(_TASK_ID_PATTERN.findall(text)),
            "rejection_kind": "",
        }
    if text.startswith("任务未创建："):
        rejection_kind = "append_notice" if "追加通知" in text else "duplicate"
        return {
            "created": False,
            "created_task_ids": [],
            "rejection_kind": rejection_kind,
        }
    return {
        "created": False,
        "created_task_ids": [],
        "rejection_kind": "",
    }
```

- [ ] **Step 4: Update both the explicit-graph execution path and the compatibility postprocess path to accumulate verified ids and derive `task_dispatch` only from explicit create success**

```python
created_task_ids: list[str] = []
for tool_result in tool_results:
    if str(tool_result.get("tool_name") or "").strip() != "create_async_task":
        continue
    parsed = self._parse_create_async_task_result(str(tool_result.get("result_text") or ""))
    if not parsed["created"]:
        continue
    for task_id in list(parsed["created_task_ids"] or []):
        if not self._task_id_exists(task_id) or task_id in created_task_ids:
            continue
        created_task_ids.append(task_id)

non_dispatch_used_tools = [
    name for name in used_tools
    if str(name or "").strip() != "create_async_task"
]
route_kind = (
    "task_dispatch"
    if created_task_ids
    else self._route_kind_for_turn(used_tools=non_dispatch_used_tools, default=str(state.get("route_kind") or "direct_reply"))
)

result["verified_task_ids"] = list(created_task_ids)
result["route_kind"] = route_kind
if created_task_ids and route_kind == "task_dispatch":
    result["repair_overlay_text"] = (
        f"Dispatch result is already available. Reply naturally based on the verified task ids {', '.join(created_task_ids)}."
    )
```

- [ ] **Step 5: Re-run the targeted frontdoor tests and then re-run the full `create_async_task` runner slice**

Run:

```bash
python -m pytest tests/resources/test_ceo_create_agent_runner.py -k "create_async_task" -q
```

Expected: PASS.

- [ ] **Step 6: Commit the frontdoor dispatch-alignment changes**

```bash
git add g3ku/runtime/frontdoor/_ceo_runtime_ops.py g3ku/runtime/frontdoor/_ceo_create_agent_impl.py tests/resources/test_ceo_create_agent_runner.py
git commit -m "fix: only treat explicit async task success as dispatch"
```

### Task 5: Update Architecture Docs And Run The Regression Sweep

**Files:**
- Modify: `docs/architecture/runtime-overview.md`
- Modify: `docs/architecture/tool-and-skill-system.md`

- [ ] **Step 1: Update runtime architecture docs to describe the new duplicate-precheck and append-notice boundary**

```md
### CEO `create_async_task` duplicate precheck

- `create_async_task` no longer creates tasks unconditionally.
- Before creation, `MainRuntimeService` compares the candidate task against the current session's unfinished task pool.
- The rule layer only blocks exact duplicates by normalized target text or exact keyword fingerprint.
- If the rule layer does not block and unfinished tasks exist, an inspection-model review may return:
  - `approve_new`
  - `reject_duplicate`
  - `reject_use_append_notice`
- `reject_use_append_notice` means the new request should update an existing unfinished task instead of spawning another one.
```

- [ ] **Step 2: Update tool-system docs to clarify the new `create_async_task` result contract**

```md
### `create_async_task` result semantics

- Success still returns `创建任务成功task:...`.
- Duplicate rejections now return a non-success business message beginning with `任务未创建：`.
- Frontdoor dispatch bookkeeping must only treat the explicit success form as a verified task dispatch.
- A rejection message may still mention an existing `task_id`, but that must not be interpreted as a newly created task.
```

- [ ] **Step 3: Run the focused regression suite that covers service logic, tool behavior, and frontdoor bookkeeping**

Run:

```bash
python -m pytest tests/resources/test_async_task_duplicate_precheck.py -q
python -m pytest tests/resources/test_tool_resource_admin_api.py -k "create_async_task_tool_" -q
python -m pytest tests/resources/test_ceo_create_agent_runner.py -k "create_async_task" -q
```

Expected: PASS for all three commands.

- [ ] **Step 4: Run a final placeholder scan on the plan-critical files and make sure docs mention the new rejection forms**

Run:

```bash
rg -n "TODO|TBD|implement later|fill in details" main/service/runtime_service.py main/prompts/async_task_duplicate_precheck.md g3ku/runtime/frontdoor/_ceo_runtime_ops.py g3ku/runtime/frontdoor/_ceo_create_agent_impl.py docs/architecture/runtime-overview.md docs/architecture/tool-and-skill-system.md
```

Expected: no matches.

- [ ] **Step 5: Commit docs and final regression updates**

```bash
git add docs/architecture/runtime-overview.md docs/architecture/tool-and-skill-system.md
git commit -m "docs: document async task duplicate precheck behavior"
```

## Implementation Notes

- Do not change the input schema for `create_async_task` in this iteration.
- Do not implement the future append-notice tool in this iteration.
- Do not add semantic similarity to the rule layer; exact normalized target and exact keyword fingerprint are the only deterministic blockers in v1.
- The inspection-model review should see all unfinished tasks in the session, not just the most recent one.
- If the inspection-model review is unavailable or unparsable, fall back to `approve_new` so the service remains available.
