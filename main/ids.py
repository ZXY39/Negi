from __future__ import annotations

from uuid import uuid4


def _new_id(prefix: str) -> str:
    return f'{prefix}:{uuid4().hex[:12]}'


def new_task_id() -> str:
    return _new_id('task')


def new_node_id() -> str:
    return _new_id('node')


def new_artifact_id() -> str:
    return _new_id('artifact')


def new_policy_id() -> str:
    return _new_id('policy')
