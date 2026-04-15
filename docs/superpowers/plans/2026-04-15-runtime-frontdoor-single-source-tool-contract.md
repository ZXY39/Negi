# Runtime And Frontdoor Single-Source Tool Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify execution nodes, acceptance nodes, and CEO/frontdoor around one authoritative per-turn tool contract so agents never see conflicting "current callable tools" while prompt caching remains stable.

**Architecture:** Keep stable bootstrap messages free of high-churn tool state. For execution and acceptance nodes, append one replaceable dynamic tool-contract message before each `before_model` dispatch. For CEO/frontdoor, append the same kind of current-turn tool contract only in the dynamic appendix. Restore and recovery must read the canonical contract from runtime frame or frontdoor state, never from stale bootstrap prompt text.

**Tech Stack:** Python 3.13, `MainRuntimeService`, `NodeRunner`, `ReActToolLoop`, CEO/frontdoor runtime, SQLite runtime frames, pytest.

---

## File Structure

**Create**
- `main/runtime/node_prompt_contract.py`
- `g3ku/runtime/frontdoor/tool_contract.py`
- `tests/resources/test_node_prompt_contract.py`

**Modify**
- `main/runtime/node_runner.py`
- `main/runtime/react_loop.py`
- `main/service/runtime_service.py`
- `main/monitoring/log_service.py`
- `main/prompts/node_execution.md`
- `main/prompts/acceptance_execution.md`
- `g3ku/runtime/frontdoor/message_builder.py`
- `g3ku/runtime/frontdoor/prompt_cache_contract.py`
- `g3ku/runtime/frontdoor/state_models.py`
- `g3ku/runtime/frontdoor/_ceo_runtime_ops.py`
- `g3ku/runtime/session_agent.py`
- `tests/resources/test_task_web_worker_runtime.py`
- `tests/resources/test_ceo_context_assembly_regressions.py`
- `tests/resources/test_ceo_prompt_cache_stability.py`
- `docs/architecture/runtime-overview.md`
- `docs/architecture/tool-and-skill-system.md`

---

## Task Outline

### Task 1: Red Tests For Node Drift

**Files:**
- Create: `tests/resources/test_node_prompt_contract.py`
- Modify: `tests/resources/test_task_web_worker_runtime.py`

- [ ] Add a unit test proving the dynamic node tool-contract message is replaced in-place, not duplicated.
- [ ] Add an execution-node integration test proving `load_tool_context(filesystem_write)` promotion appears in the next-turn dynamic contract as callable, not candidate.
- [ ] Add an acceptance-node integration test proving acceptance nodes use the same dynamic contract refresh path.
- [ ] Run:

```powershell
$env:PYTHONPATH='d:\NewProjects\G3KU'
python -m pytest tests/resources/test_node_prompt_contract.py -q
python -m pytest tests/resources/test_task_web_worker_runtime.py -k "dynamic_tool_contract_after_hydration" -q
```

- [ ] Commit:

```powershell
git add tests/resources/test_node_prompt_contract.py tests/resources/test_task_web_worker_runtime.py
git commit -m "test: lock stale node tool contract regressions"
```

### Task 2: Stable Bootstrap + Dynamic Node Contract

**Files:**
- Create: `main/runtime/node_prompt_contract.py`
- Modify: `main/runtime/node_runner.py`
- Modify: `main/prompts/node_execution.md`
- Modify: `main/prompts/acceptance_execution.md`

- [ ] Introduce `NodeRuntimeToolContract` plus helpers to append or replace a `message_type="node_runtime_tool_contract"` user message.
- [ ] Keep the bootstrap node user JSON stable: task id, node id, prompt, goal, execution policy, runtime environment, completion contract.
- [ ] Remove current-turn `callable_tool_names` and `candidate_tools` from the bootstrap node user JSON.
- [ ] Update both node prompts so the dynamic tool-contract message is the authoritative current-turn tool contract.
- [ ] Run the Task 1 tests again and confirm the helper path is green.
- [ ] Commit:

```powershell
git add main/runtime/node_prompt_contract.py main/runtime/node_runner.py main/prompts/node_execution.md main/prompts/acceptance_execution.md tests/resources/test_node_prompt_contract.py tests/resources/test_task_web_worker_runtime.py
git commit -m "feat: split node bootstrap prompt from dynamic tool contract"
```

### Task 3: Canonical Node Contract On Every `before_model` Turn

**Files:**
- Modify: `main/runtime/react_loop.py`
- Modify: `main/service/runtime_service.py`
- Modify: `main/monitoring/log_service.py`
- Modify: `tests/resources/test_task_web_worker_runtime.py`

- [ ] Extend the node tool-schema selection payload so it returns one authoritative `tool_names` list plus `candidate_tool_names`.
- [ ] Before every `before_model` dispatch in `ReActToolLoop`, rebuild the node dynamic contract from the actual tool-schema selection result and replace the previous dynamic contract message in `message_history`.
- [ ] Persist `callable_tool_names`, `candidate_tool_names`, `selected_skill_ids`, and `candidate_skill_ids` into the runtime frame.
- [ ] Add an invariant test proving the dynamic contract callable list equals the frame callable list and equals the model-visible tool list for the same turn.
- [ ] Run:

```powershell
$env:PYTHONPATH='d:\NewProjects\G3KU'
python -m pytest tests/resources/test_task_web_worker_runtime.py -k "dynamic_tool_contract or callable_source" -q
```

- [ ] Commit:

```powershell
git add main/runtime/react_loop.py main/service/runtime_service.py main/monitoring/log_service.py tests/resources/test_task_web_worker_runtime.py
git commit -m "feat: unify node callable tools under canonical before-model contract"
```

### Task 4: Restore And Recovery Must Read The Canonical Frame Contract

**Files:**
- Modify: `main/service/runtime_service.py`
- Modify: `tests/resources/test_task_web_worker_runtime.py`

- [ ] Update `_restore_node_context_selection_entry()` to read canonical tool and skill lists from frame fields first.
- [ ] Permit fallback to the dynamic contract message only when the new frame fields are absent.
- [ ] Stop reconstructing current callable/candidate tools from the stable bootstrap user JSON.
- [ ] Add a regression test proving a stale bootstrap payload cannot override a newer frame contract.
- [ ] Run:

```powershell
$env:PYTHONPATH='d:\NewProjects\G3KU'
python -m pytest tests/resources/test_task_web_worker_runtime.py -k "restore_node_context_selection_prefers_frame_contract" -q
```

- [ ] Commit:

```powershell
git add main/service/runtime_service.py tests/resources/test_task_web_worker_runtime.py
git commit -m "fix: restore node context selection from canonical frame contract"
```

### Task 5: Frontdoor Single-Source Contract Without Cache Regression

**Files:**
- Create: `g3ku/runtime/frontdoor/tool_contract.py`
- Modify: `g3ku/runtime/frontdoor/message_builder.py`
- Modify: `g3ku/runtime/frontdoor/prompt_cache_contract.py`
- Modify: `g3ku/runtime/frontdoor/state_models.py`
- Modify: `g3ku/runtime/frontdoor/_ceo_runtime_ops.py`
- Modify: `g3ku/runtime/session_agent.py`
- Modify: `tests/resources/test_ceo_context_assembly_regressions.py`
- Modify: `tests/resources/test_ceo_prompt_cache_stability.py`

- [ ] Add a cache-stability test proving the frontdoor prompt cache key does not change when only the dynamic tool contract changes.
- [ ] Build one `FrontdoorToolContract` from current turn state, hydrated tools, visible skills, and candidate tools.
- [ ] Append that contract only in `dynamic_appendix_messages`; do not place current-turn callable/candidate tools in stable frontdoor bootstrap messages.
- [ ] Persist and restore this frontdoor contract across interrupt, pause, and resume.
- [ ] Add a regression test proving current-turn frontdoor callable/candidate tool state comes from exactly one dynamic contract.
- [ ] Run:

```powershell
$env:PYTHONPATH='d:\NewProjects\G3KU'
python -m pytest tests/resources/test_ceo_prompt_cache_stability.py -k "dynamic_tool_contract_changes" -q
python -m pytest tests/resources/test_ceo_context_assembly_regressions.py -k "dynamic_contract_is_authoritative" -q
```

- [ ] Commit:

```powershell
git add g3ku/runtime/frontdoor/tool_contract.py g3ku/runtime/frontdoor/message_builder.py g3ku/runtime/frontdoor/prompt_cache_contract.py g3ku/runtime/frontdoor/state_models.py g3ku/runtime/frontdoor/_ceo_runtime_ops.py g3ku/runtime/session_agent.py tests/resources/test_ceo_context_assembly_regressions.py tests/resources/test_ceo_prompt_cache_stability.py
git commit -m "feat: unify frontdoor tool state under cache-friendly dynamic contract"
```

### Task 6: Observability And Docs

**Files:**
- Modify: `main/monitoring/log_service.py`
- Modify: `docs/architecture/runtime-overview.md`
- Modify: `docs/architecture/tool-and-skill-system.md`

- [ ] Make `task_runtime_messages` snapshots prefer explicit frame `callable_tool_names` and `candidate_tool_names` when present.
- [ ] Document that execution and acceptance nodes now use a stable bootstrap plus a per-turn dynamic tool contract.
- [ ] Document that frontdoor current-turn callable/candidate tools live only in the dynamic appendix and persisted state, not in stable prompt text.
- [ ] Run:

```powershell
$env:PYTHONPATH='d:\NewProjects\G3KU'
python -m pytest tests/resources/test_node_prompt_contract.py -q
python -m pytest tests/resources/test_task_web_worker_runtime.py -k "dynamic_tool_contract or callable_source or restore_node_context_selection_prefers_frame_contract" -q
python -m pytest tests/resources/test_ceo_prompt_cache_stability.py -k "dynamic_tool_contract_changes" -q
```

- [ ] Commit:

```powershell
git add main/monitoring/log_service.py docs/architecture/runtime-overview.md docs/architecture/tool-and-skill-system.md
git commit -m "docs: describe canonical runtime tool contract flow"
```

---

## Acceptance Criteria

- Execution nodes and acceptance nodes both expose exactly one authoritative current-turn tool contract.
- A tool promoted by `load_tool_context(...)` appears in the next-turn callable contract without waiting for a new node bootstrap rebuild.
- The stable bootstrap node user payload does not carry stale callable/candidate tool lists.
- Node restore/recovery no longer trusts stale bootstrap tool lists over frame contract fields.
- Frontdoor current-turn callable/candidate tool state is emitted only in the dynamic appendix and persisted frontdoor state.
- Prompt cache keys stay stable when only dynamic tool contract content changes and tool schemas do not change.
- Architecture docs describe the new single-source contract and the cache-friendly placement rules.
