from __future__ import annotations


def list_effective_tool_names(*, subject, supported_tool_names: list[str], resource_registry, policy_engine) -> list[str]:
    visible: list[str] = []
    for executor_name in supported_tool_names:
        tool = resource_registry.get_tool(str(executor_name or '').strip())
        if tool is None or not bool(getattr(tool, 'agent_visible', True)):
            continue
        decision = policy_engine.evaluate_tool(subject=subject, tool_id=tool.tool_id)
        if decision.allowed:
            visible.append(tool.tool_id)
    return sorted(set(visible))


def list_effective_skill_ids(*, subject, available_skill_ids: list[str], policy_engine) -> list[str]:
    visible: list[str] = []
    for skill_id in available_skill_ids:
        decision = policy_engine.evaluate_skill_access(subject=subject, skill_id=skill_id)
        if decision.allowed:
            visible.append(skill_id)
    return sorted(set(visible))
