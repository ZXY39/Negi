from typing import Any

from main.models import build_execution_policy_schema


CONTINUE_TASK_DESCRIPTION = (
    'Continue an existing async task in exactly one controlled mode. '
    'The runtime first ensures the target task reaches a terminal state, '
    'then either recreates it as a continuation task or retries it in place.'
)


def build_continue_task_parameters() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'mode': {
                'type': 'string',
                'enum': ['recreate', 'retry_in_place'],
            },
            'target_task_id': {
                'type': 'string',
            },
            'continuation_instruction': {
                'type': 'string',
            },
            'execution_policy': build_execution_policy_schema(
                description='Optional execution policy override. If omitted, inherit the target task execution policy.',
            ),
            'requires_final_acceptance': {
                'type': 'boolean',
            },
            'final_acceptance_prompt': {
                'type': 'string',
            },
            'reuse_existing': {
                'type': 'boolean',
            },
            'reason': {
                'type': 'string',
            },
        },
        'required': ['mode', 'target_task_id', 'continuation_instruction'],
    }
