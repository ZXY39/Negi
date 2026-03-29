from __future__ import annotations

from main.monitoring.query_service import TaskQueryService


class TaskQueryServiceV2(TaskQueryService):
    """Named V2 read-model facade.

    The implementation intentionally reuses the current TaskQueryService logic,
    but runtime wiring should depend on this class name so the V2 boundary is
    explicit.
    """

    pass
