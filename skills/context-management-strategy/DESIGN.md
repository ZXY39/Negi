# Context Management Strategy — Design Document

> 设计说明：为何这样整合、保留了哪些能力、舍弃了哪些能力
> Skill ID: `context-management-strategy`
> Date: 2025-06-01

## 1. Design Goal

Create a unified, G3KU-adapted context management strategy that consolidates best practices from multiple upstream context management skills, providing comprehensive guidance for:

- **Context saving**: When and how to preserve working context
- **Context recovery**: How to rebuild context after interruptions
- **Budget control**: What to keep vs trim when context approaches limits
- **Scope management**: How to define and track relevant file sets
- **Continuity**: How to maintain context across sessions and stages

## 2. Source Material

### Evaluated Candidates (ClawHub)

| Candidate | Status | Key Contribution |
|-----------|--------|------------------|
| context-management-context-save | Non-suspicious | Session lifecycle, save triggers |
| context-recovery | Non-suspicious | Recovery hierarchy (L0–L4), validation |
| context-budgeting | Non-suspicious | Priority levels (P0–P4), trimming cascade |
| context-scope-tags | Non-suspicious | Scope definition, file tracking |
| context-driven-development | Non-suspicious | Context-first workflow, continuity principles |
| session-watchdog | Non-suspicious | Session health monitoring |
| context-clean-up | Non-suspicious | Cleanup triggers and routines |
| context-compression | Non-suspicious | Compression strategies |
| context-sentinel | Non-suspicious | Anomaly detection in context |
| compaction-survival | Non-suspicious | Post-compaction recovery |
| context-preserver | Non-suspicious | Important context tagging |
| context-hygiene | Non-suspicious | Regular cleanup procedures |

Full comparison in `references/candidate-comparison.md`.

## 3. Preservation Decisions

### Preserved (Directly Incorporated)

| Capability | Source | Where in unified skill |
|------------|--------|----------------------|
| Session lifecycle model | context-save | SKILL.md § Session Lifecycles |
| Save trigger conditions | context-save | SKILL.md § When to Save Context |
| Recovery hierarchy (L0–L4) | context-recovery | references/recovery.md |
| Recovery report format | context-recovery | references/recovery.md |
| Recovery validation principles | context-recovery | references/recovery.md |
| Priority levels (P0–P4) | context-budgeting | SKILL.md § Budget Levels |
| Trimming cascade | context-budgeting | references/budget-and-trimming.md |
| Scope tag types | context-scope-tags | SKILL.md § Scope Management |
| Context reconciliation workflow | context-driven-development | references/continuity.md |
| Continuity anti-patterns | context-driven-development | references/continuity.md |
| Cross-session continuity model | context-driven-development | references/continuity.md |

### Adapted (Integrated with G3KU)

| Capability | Original | G3KU Adaptation |
|------------|----------|-----------------|
| Checkpoint system | Standalone save API | Integrated with `submit_next_stage` stage summaries |
| Token thresholds | Hard numbers (100K, 150K) | Relative budget percentages and state awareness |
| Recovery sequence | Tool-specific steps | G3KU tool-mapped recovery (`memory_search`, `filesystem`, `content`) |
| Scope tags | Standalone format | Integrated with `runtime_environment.path_policy` |
| Context-first workflow | Prescriptive steps | Simplified for G3KU's task/stage model |

### Simplified (Reduced Detail)

| Area | Original | Simplified |
|------|----------|------------|
| Continuous monitoring | Background watchdog process | Awareness and trigger-based approach |
| Cleanup procedures | Separate skill with routines | Absorbed into budget & trimming section |
| Compression strategies | Dedicated algorithm | Referenced as technique, not prescribed |
| Anomaly detection | Sentinel monitoring | Basic awareness in anti-patterns section |
| Post-compaction recovery | Specialized protocol | Covered under recovery hierarchy (L3/L4) |

### Excluded (Not Incorporated)

| Concept | Reason |
|---------|--------|
| Standalone save/checkpoint API | G3KU's stage system already provides save points |
| Continuous background monitoring | Doesn't fit G3KU's tool-call interaction model |
| Tool-specific implementations | Would become outdated; focus on principles instead |
| Hard token thresholds | Vary by model; relative percentages are more portable |
| Redundant concepts | Already covered by G3KU's memory/stage/task systems |

## 4. Architecture Decisions

### Single Skill, Multiple References

**Why**: Context management concerns are deeply interrelated. Budget trimming depends on scope tags; recovery depends on checkpoints; continuity depends on all of the above. Separate skills would create artificial boundaries.

**Structure**:
- `SKILL.md` — Core workflow, quick reference, decision trees
- `references/` — Detailed protocols, comparison docs, integration guides

### Integration Over Replacement

**Principle**: This skill does not replace G3KU's existing systems (memory, stages, tasks). It complements them by providing context-specific strategy.

- Memory system → stores and retrieves contextual knowledge
- Stage system → provides natural checkpoint boundaries
- Task system → provides scope and goal context
- **This skill** → provides strategy for managing context within and across these systems

### Principles Over Prescriptions

**Principle**: Specific tools and APIs change. Principles endure. The skill focuses on:
- When to act (triggers and thresholds)
- What to consider (priorities and scopes)
- How to validate (verification steps)
- What to avoid (anti-patterns)

Rather than prescribing exact API calls or tool invocations.

## 5. Workflow Design

The unified skill defines a context lifecycle:

```
CONTEXT ESTABLISH → CONTEXT MONITOR → CONTEXT MAINTAIN → CONTEXT RECOVER
     ↓                  ↓                  ↓                  ↓
 Define scope      Track budget       Save at triggers   Rebuild from hierarchy
 Set priorities    Watch for warnings Trim when needed    Validate recovery
 Load checkpoint   Stay within scope  Archive completed   Continue work
```

This maps to G3KU's operational phases:

| Phase | G3KU Tool/System | Context Strategy Action |
|-------|-----------------|------------------------|
| Task start | `submit_next_stage` | Establish context, load checkpoints |
| During work | Tool rounds | Monitor budget, respect scope |
| Stage transition | `submit_next_stage` | Checkpoint, summarize |
| Context pressure | N/A | Trim per priority cascade |
| Session end | N/A | Final checkpoint, memory summary |
| Session start | `memory_search` | Recovery protocol |

## 6. Maintenance Guide

### When to Update

- G3KU adds new tools that can participate in context management
- Token limits or context windows change significantly
- New patterns emerge that challenge current strategies
- Upstream candidates add valuable new concepts

### How to Update

1. Review current `SKILL.md` and references for relevance
2. Check ClawHub for new context-related candidates
3. Assess new patterns against existing anti-patterns
4. Update `references/candidate-comparison.md` with new evaluations
5. Update `SKILL.md` and affected reference files

### Key Files

| File | Purpose | Update Frequency |
|------|---------|-----------------|
| SKILL.md | Core strategy | When workflow changes |
| references/budget-and-trimming.md | Budget details | When thresholds change |
| references/checkpoint-and-save.md | Checkpoint protocol | When checkpoint format changes |
| references/recovery.md | Recovery protocol | When recovery tools change |
| references/continuity.md | Continuity strategy | When cross-session patterns change |
| references/integration.md | System integration | When G3KU systems change |
| references/candidate-comparison.md | Source material tracking | When new candidates emerge |

## 7. Relationship to Other Systems

| System | Role | How context-strategy relates |
|--------|------|---------------------------|
| `memory` | Long-term storage | Context strategy defines what to store and how to retrieve |
| `clawhub-skill-manager` | Skill installation | Used to evaluate upstream candidates |
| `skill-creator` | Skill creation | Used to create this skill |
| G3KU stage system | Task progression | Context checkpoints align with stage boundaries |
| G3KU task system | Work organization | Context scope is defined per task |
