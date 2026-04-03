# Candidate Skill Comparison

> 上游候选 skill 能力对比与取舍依据
> 分析日期: 2025-06-01

## Evaluated Candidates

### Core Candidates (5)

| Candidate | Source | State | Key Ideas Adopted |
|-----------|--------|-------|-------------------|
| context-management-context-save | ClawHub | Non-suspicious | Session lifecycle, save triggers |
| context-recovery | ClawHub | Non-suspicious | Recovery hierarchy, validation |
| context-budgeting | ClawHub | Non-suspicious | Priority levels, trimming rules |
| context-scope-tags | ClawHub | Non-suspicious | Scope definition, file tracking |
| context-driven-development | ClawHub | Non-suspicious | Context-first workflow |

### Additional Candidates (7)

| Candidate | Source | State | Key Ideas Adopted |
|-----------|--------|-------|-------------------|
| session-watchdog | ClawHub | Non-suspicious | Session monitoring |
| context-clean-up | ClawHub | Non-suspicious | Cleanup triggers |
| context-compression | ClawHub | Non-suspicious | Compression strategies |
| context-sentinel | ClawHub | Non-suspicious | Anomaly detection |
| compaction-survival | ClawHub | Non-suspicious | Post-compaction recovery |
| context-preserver | ClawHub | Non-suspicious | Important context tagging |
| context-hygiene | ClawHub | Non-suspicious | Regular cleanup routines |

## Detailed Assessment

### context-management-context-save

**Strengths**: Clear session lifecycle model, specific save triggers (10-round warning, explicit save command, stage completion, session end). Good focus on preventing context loss.

**Limitations**: Too prescriptive about save mechanisms; assumes a specific save API that G3KU doesn't have. Overlaps with G3KU's existing stage system which already provides natural save points.

**Decision**: Adopt lifecycle model and save trigger concepts; integrate with G3KU stage transitions rather than standalone save system.

### context-recovery

**Strengths**: Excellent recovery hierarchy (L0-L4), explicit validation requirements, clear anti-patterns. Recovery report format is practical.

**Limitations**: Recovery sequence is too detailed for a general skill; some steps assume tools G3KU doesn't have.

**Decision**: Adopt recovery hierarchy, validation principles, anti-patterns, and report format. Simplify recovery sequence to match G3KU tools.

### context-budgeting

**Strengths**: Priority levels (P0-P4) are excellent. Trimming cascade gives clear ordering. Token awareness is crucial for LLM context.

**Limitations**: Token counts are approximate and vary by model. The specific thresholds may not match G3KU's actual limits.

**Decision**: Adopt priority levels and trimming cascade concept. Remove hard token thresholds; use relative percentages and budget state instead.

### context-scope-tags

**Strengths**: Very practical file-scope tracking. Tag types (workspace, context, exclude, read-only, generated) cover common cases well.

**Limitations**: Too prescriptive about tag format; doesn't account for G3KU's runtime_environment.path_policy which already provides structural scope information.

**Decision**: Adopt scope tag concepts and tag types. Integrate with G3KU's existing runtime_environment scope rather than standalone tagging system.

### context-driven-development

**Strengths**: Strong workflow model (establish context first, then work). The context reconciliation phase before starting work is excellent. Continuity principles are valuable.

**Limitations**: Workflow steps are too detailed and prescriptive. Some steps don't apply to all task types.

**Decision**: Adopt context reconciliation workflow and continuity principles. Simplify steps for G3KU's task model.

### session-watchdog

**Strengths**: Good concept of monitoring session health and warning before context loss.

**Limitations**: Too implementation-specific. Assumes continuous monitoring which may not be practical in G3KU's tool-call model.

**Decision**: Reference only for awareness; not directly integrated.

### context-clean-up / context-compression / context-hygiene / context-sentinel / compaction-survival / context-preserver

**Strengths**: Each has a narrow focus (cleanup, compression, anomaly detection, post-compaction recovery, importance tagging).

**Limitations**: These are all variations on the budgeting/trimming theme. Their specific implementations are too tool-specific.

**Decision**: Reference for additional cleanup strategies; not directly integrated as separate modules. Their ideas are absorbed into the budget & trimming section.

## Design Rationale

### Why One Unified Skill

1. **Overlapping concerns**: All candidates deal with different aspects of the same problem — maintaining useful context without hitting limits.
2. **Consistent terminology**: Separate skills would use different terms for the same concepts.
3. **Simpler maintenance**: One skill is easier to update, review, and understand.
4. **G3KU fit**: G3KU already has stage system, memory system, and task system; context strategy should complement these, not replace them.

### What Was Preserved

- Session lifecycle model from context-save
- Recovery hierarchy and validation from context-recovery
- Priority-based trimming from context-budgeting
- Scope tagging from context-scope-tags
- Context-first workflow from context-driven-development

### What Was Simplified

- Hard token thresholds → relative budget percentages
- Prescriptive save API → integration with G3KU stage system
- Detailed recovery sequences → G3KU tool-specific steps
- Standalone monitoring → awareness and triggers

### What Was Excluded

- Implementation-specific API calls
- Tool assumptions that don't match G3KU
- Continuous monitoring that doesn't fit G3KU's interaction model
- Redundant concepts covered by existing G3KU systems (stages, memory, tasks)
