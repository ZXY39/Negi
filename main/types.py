from typing import Literal, TypeAlias

NodeStatus: TypeAlias = Literal['in_progress', 'success', 'failed']
TaskStatus: TypeAlias = Literal['in_progress', 'success', 'failed']
NodeKind: TypeAlias = Literal['execution', 'acceptance']

STATUS_IN_PROGRESS: NodeStatus = 'in_progress'
STATUS_SUCCESS: NodeStatus = 'success'
STATUS_FAILED: NodeStatus = 'failed'

KIND_EXECUTION: NodeKind = 'execution'
KIND_ACCEPTANCE: NodeKind = 'acceptance'
