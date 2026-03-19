from __future__ import annotations

from collections import defaultdict


MUTATING_ACTION_IDS = frozenset({'write', 'edit', 'delete', 'propose_patch'})


def list_effective_tool_names(*, subject, supported_tool_names: list[str], resource_registry, policy_engine, mutation_allowed: bool) -> list[str]:
    executor_actions: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for family in resource_registry.list_tool_families():
        for action in family.actions:
            if not bool(getattr(action, 'agent_visible', True)):
                continue
            for executor_name in list(action.executor_names or []):
                executor_actions[str(executor_name)].append((family.tool_id, action.action_id))

    visible: list[str] = []
    for executor_name in supported_tool_names:
        action_pairs = executor_actions.get(executor_name) or []
        if not action_pairs:
            continue
        for tool_id, action_id in action_pairs:
            if not mutation_allowed and action_id in MUTATING_ACTION_IDS:
                continue
            decision = policy_engine.evaluate_tool_action(subject=subject, tool_id=tool_id, action_id=action_id)
            if decision.allowed:
                visible.append(executor_name)
                break
    return sorted(set(visible))


def list_effective_skill_ids(*, subject, available_skill_ids: list[str], policy_engine) -> list[str]:
    visible: list[str] = []
    for skill_id in available_skill_ids:
        decision = policy_engine.evaluate_skill_access(subject=subject, skill_id=skill_id)
        if decision.allowed:
            visible.append(skill_id)
    return sorted(set(visible))
