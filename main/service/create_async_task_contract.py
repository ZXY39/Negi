from __future__ import annotations

from typing import Any


CREATE_ASYNC_TASK_DESCRIPTION = (
    '把用户需求转交为后台异步任务；主 agent 不可直接使用派生子节点。'
    '对于工作量大的任务，应在 task 说明中显式要求执行节点优先评估拆解并派生子节点。'
    '调用时必须提供一句高度概括核心需求的 core_requirement，且其内容不能等于 task 原文。'
    '必要时可同时声明最终结果是否需要验收。'
)

CREATE_ASYNC_TASK_TASK_DESCRIPTION = (
    '用户的原始需求。若任务工作量大，应在说明中写明拆分维度，并显式建议执行节点优先评估拆解/派生子节点。'
)

CREATE_ASYNC_TASK_CORE_REQUIREMENT_DESCRIPTION = (
    'CEO 对用户需求的核心需求概括。只能是一句高度概括核心需求的句子；'
    '不能等于 task 内容，不能复制 task 的大段原文。该句子会沿任务树传播到所有下游子节点。'
)

CREATE_ASYNC_TASK_REQUIRES_FINAL_ACCEPTANCE_DESCRIPTION = '是否需要在 root execution 完成后再做最终验收。'

CREATE_ASYNC_TASK_FINAL_ACCEPTANCE_PROMPT_DESCRIPTION = (
    '最终验收提示词。仅当 requires_final_acceptance=true 时必填。'
)


def build_create_async_task_parameters() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'task': {
                'type': 'string',
                'description': CREATE_ASYNC_TASK_TASK_DESCRIPTION,
            },
            'core_requirement': {
                'type': 'string',
                'description': CREATE_ASYNC_TASK_CORE_REQUIREMENT_DESCRIPTION,
            },
            'requires_final_acceptance': {
                'type': 'boolean',
                'description': CREATE_ASYNC_TASK_REQUIRES_FINAL_ACCEPTANCE_DESCRIPTION,
            },
            'final_acceptance_prompt': {
                'type': 'string',
                'description': CREATE_ASYNC_TASK_FINAL_ACCEPTANCE_PROMPT_DESCRIPTION,
            },
        },
        'required': ['task', 'core_requirement'],
    }
