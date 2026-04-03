# Integration with Memory and Stage Systems

> 上下文策略与 G3KU 记忆系统、阶段系统的协作协议
> 上游贡献者: memory skill, G3KU stage system

## TL;DR

1. **Memory vs Checkpoint**: Memory = 长期知识沉淀；Checkpoint = 短期工作状态快照。二者互补，不可互相替代。
2. **Context Budget 与 Stage Budget**: 上下文预算控制单个 session 的 context window；Stage 预算控制工具调用轮次。二者协同，但关注不同维度。
3. **Scope Tags 与 Memory Search**: Scope tags 缩小 memory_search 的搜索范围，提高检索准确性和效率。

## Memory System Integration

### What Goes Where

| Content Type | Storage | Retention | Why |
|--------------|---------|-----------|-----|
| Task contracts | Memory L2 | Long-term | Need to resume tasks across days/weeks |
| Session summaries | Memory L2 | Medium-term | Useful for continuity, not forever |
| Checkpoint data | Checkpoint files | Short-term | Tied to active work sessions |
| Project decisions | Memory L2 | Long-term | Architectural decisions persist |
| Working context | Session state | Session lifetime | Only needed while actively working |
| Tool results | Memory L1/L2 | Varies | Depends on reusability |

### Memory Search with Context Strategy

When using `memory_search` in context-aware workflows:

1. **Scope-guided search**: Use scope tags to construct focused queries
   - Bad: `memory_search("what was I doing")`
   - Good: `memory_search("task:10dd00d79d19 context-management-strategy progress")`

2. **Context type filtering**: Always specify `context_type` when possible
   - `context_type: "resource"` for skill/tool context
   - `context_type: "memory"` for session/task records

3. **Include L2 preview**: Use `include_l2: true` for task-level context

### Memory Invalidation

Context stored in memory can become stale. Invalidation triggers:

- **Source code change**: If tracked files have changed, memory references are suspect
- **Task completion**: Completed task artifacts should be archived, not actively loaded
- **Project pivot**: Major direction change invalidates most contextual memory

## Stage System Integration

### Context Checkpoints at Stage Boundaries

Each stage transition is a natural checkpoint opportunity:

```
submit_next_stage {
    completed_stage_summary: "Stage N summary"   ← This IS a checkpoint
    key_refs: [...]                                ← These are context anchors
    stage_goal: "Stage N+1 goal"
}
```

### Context Budget vs Stage Budget

| Dimension | Context Budget | Stage Budget |
|-----------|---------------|--------------|
| Controls | Context window size (tokens) | Tool call rounds |
| Trigger | Approaching token limits | Reaching round limit |
| Response | Trim, checkpoint, summarize | Create new stage or delegate |
| Granularity | Continuous | Discrete (per round) |
| Recovery | From checkpoints + memory | From stage summaries |

### Cross-Stage Context Flow

```
Stage N completes
    ↓
1. Write completed_stage_summary (checkpoint trigger)
2. Collect key_refs (context anchors for next stage)
3. If needed: write explicit checkpoint file
    ↓
Stage N+1 starts
    ↓
4. Load prior stage summary as context
5. Load key_refs if needed for current work
6. Begin with minimal context, expand as needed
```

## Scope Tags Integration

With Task System:
- Task scope tags define the working file set
- Memory search uses scope tags for query construction
- Checkpoints reference scope-tagged files

With File Operations:
- Scope tags are file paths relative to workspace
- Changes to tagged files trigger checkpoint consideration
- Untagged file operations should be deliberate and documented

## Anti-Patterns for Integration

- **Do not** dump all memory into context — memory_search should be targeted
- **Do not** treat stage summaries as full context — they are compressed views
- **Do not** skip checkpoint before stage transitions in costly stages
- **Do not** confuse context budget (tokens) with stage budget (rounds)
- **Do not** store redundant data in both memory and checkpoints

## Quick Reference: Which System to Use

| Need | Primary System | Secondary System |
|------|---------------|------------------|
| Resume work after break | Checkpoint + Memory | Stage summaries |
| Continue after round limit | Stage summary | Checkpoint |
| Continue after context overflow | Checkpoint | Memory |
| Understand past task design | Memory L2 | None |
| Track current session state | Session memory | Checkpoint |
| Cross-session task continuity | Memory + Checkpoint | Stage summaries |
