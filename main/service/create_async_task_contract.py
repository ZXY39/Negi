from __future__ import annotations

from typing import Any

from main.models import build_execution_policy_schema


CREATE_ASYNC_TASK_DESCRIPTION = (
    '把用户需求转交为后台异步任务；主 agent 不可直接使用派生子节点。'
    '对于工作量大的任务，应在 task 说明中显式要求执行节点优先评估拆解并派生子节点。'
    '调用时必须提供一句高度概括核心需求的 core_requirement，且其内容不能等于 task 原文。'
    '还必须显式提供 execution_policy.mode，用于约束后续任务树整体采用聚焦还是全面执行策略。'
    '必要时可同时声明最终结果是否需要验收。'
)

CREATE_ASYNC_TASK_TASK_DESCRIPTION = (
    '用户的原始需求。若任务工作量大，应在说明中写明拆分维度，并显式建议执行节点优先评估拆解/派生子节点。'
)

CREATE_ASYNC_TASK_CORE_REQUIREMENT_DESCRIPTION = (
    'CEO 对用户需求的核心需求概括。只能是一句高度概括核心需求的句子，'
    '不能等于 task 内容，不能复制 task 的大段原文。该句子会沿任务树传播到所有下游子节点。'
)

CREATE_ASYNC_TASK_EXECUTION_POLICY_DESCRIPTION = (
    '执行策略。`focus` 表示只做最高价值、最必要、与当前目标直接相关的动作；'
    '`coverage` 表示仍然先做最高价值动作，但在需要时允许扩展范围、补做边缘分支或系统性全量操作。'
)

CREATE_ASYNC_TASK_REQUIRES_FINAL_ACCEPTANCE_DESCRIPTION = '是否需要在 root execution 完成后再做最终验收。'

CREATE_ASYNC_TASK_FINAL_ACCEPTANCE_PROMPT_DESCRIPTION = '最终验收提示词。仅当 requires_final_acceptance=true 时必填。'

CREATE_ASYNC_TASK_CONTINUATION_OF_TASK_ID_DESCRIPTION = '若这是对某个已失败任务的续跑，请传入原失败任务 id，格式为 task:xxxx。'

CREATE_ASYNC_TASK_REUSE_EXISTING_DESCRIPTION = '是否优先复用同一会话下、针对同一个 continuation_of_task_id 的进行中续跑任务。默认 true。'


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
            'execution_policy': build_execution_policy_schema(
                description=CREATE_ASYNC_TASK_EXECUTION_POLICY_DESCRIPTION,
            ),
            'requires_final_acceptance': {
                'type': 'boolean',
                'description': CREATE_ASYNC_TASK_REQUIRES_FINAL_ACCEPTANCE_DESCRIPTION,
            },
            'final_acceptance_prompt': {
                'type': 'string',
                'description': CREATE_ASYNC_TASK_FINAL_ACCEPTANCE_PROMPT_DESCRIPTION,
            },
            'continuation_of_task_id': {
                'type': 'string',
                'description': CREATE_ASYNC_TASK_CONTINUATION_OF_TASK_ID_DESCRIPTION,
            },
            'reuse_existing': {
                'type': 'boolean',
                'description': CREATE_ASYNC_TASK_REUSE_EXISTING_DESCRIPTION,
            },
        },
        'required': ['task', 'core_requirement', 'execution_policy'],
    }
