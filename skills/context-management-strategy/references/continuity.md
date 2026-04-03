# Continuity Strategies

> 上下文持续性与跨会话工作策略
> 上游贡献者: context-driven-development, session-watchdog, context-preserver

## TL;DR

1. **连续性优先级**: Task contracts > Scope tags > Checkpoints > Memory
2. **跨会话工作**: 每次 session 启动时先做 context reconciliation，然后按 task contract 恢复
3. **渐进式上下文**: 不追求一次性恢复全部上下文，而是按需加载、按需裁剪

## Continuity Model

### Session Lifecycle

```
SESSION START
    ↓
Context Reconciliation (Phase 1)
    ├── Load task contract
    ├── Load scope tags for current task
    ├── Check for active checkpoints
    └── Build working context
    ↓
Work Execution (Phase 2)
    ├── Follow task contract
    ├── Update checkpoints at milestones
    ├── Monitor context budget
    └── Apply scope tags to new artifacts
    ↓
SESSION END (graceful)
    ├── Final checkpoint
    ├── Context summary to memory
    └── Task state update
    ↓
SESSION END (abrupt)
    └── Relies on auto-save + next session recovery
```

### Task Continuity

For multi-session tasks, maintain:

1. **Task Contract**: What are we building? Why? What are the constraints?
2. **Progress Ledger**: What has been done? What remains?
3. **Active Context**: Files, artifacts, decisions directly relevant to current work
4. **Scope Boundaries**: What is in scope vs out of scope for this task

### Context Reconciliation Protocol

When starting work on a task (new or resumed session):

1. **Load task contract** — understand the goal and constraints
2. **Scan for recent checkpoints** — find the latest known good state
3. **Load scope tags** — know what files/areas matter for this task
4. **Build working set** — identify the minimal context needed to start
5. **Validate against current source** — does context match HEAD?
6. **Declare readiness** — report context state before proceeding

## Checkpoint Strategy

### When to Checkpoint

- After completing a logical unit of work (function, module, test)
- Before switching to a different task or sub-task
- Before any operation that might corrupt state
- At regular intervals during long work sessions (every ~10 tool rounds)

### Checkpoint Content

```
{
  "timestamp": "ISO8601",
  "task_id": "task:xxx",
  "checkpoint_id": "cp:N",
  "state": {
    "summary": "What was just completed",
    "files_modified": ["path/to/file"],
    "artifacts_created": ["artifact ref"],
    "decisions_made": ["decision summary"],
    "next_action": "What should happen next"
  },
  "context_state": {
    "token_budget_used": "~N",
    "scope_tags_active": ["tag1", "tag2"]
  }
}
```

### Continuity Anti-Patterns

- **Do not** assume continuity without explicit reconciliation
- **Do not** carry over context from unrelated tasks
- **Do not** trust old context if source code has changed significantly
- **Do not** create orphan checkpoints without task association

## Cross-Session Decision Guide

| Scenario | Action |
|----------|--------|
| Resume same task | Load task contract + latest checkpoint + reconcile |
| Resume task, different sub-task | Load task contract + full task context + new scope tags |
| New task in known project | Load project scope + create new task contract |
| New task in new project | Start fresh, establish project scope from scratch |
| Uncertain state | Run recovery protocol with medium-depth search |

## Integration Points

This continuity strategy integrates with:

- **Memory System**: Task contracts and session summaries stored in L2 memory
- **Budget System**: Checkpoints include budget state; continuity respects budget limits
- **Scope Tags**: Continuity depends on scope tags to know what context is relevant
- **Stage System**: Checkpoints naturally align with stage transitions
