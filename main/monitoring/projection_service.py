from __future__ import annotations


class TaskProjectionService:
    """V2 runtime keeps task read models incrementally updated on write.

    The old projection service rebuilt task-level projections from scratch on
    each update. That path is intentionally removed in V2. The retained class
    is now a lightweight compatibility surface for callers that still invoke the
    old hook points during startup or recovery.
    """

    def __init__(self, *, store, tree_builder) -> None:
        self._store = store
        self._tree_builder = tree_builder

    def ensure_task_projection(self, task_id: str) -> None:
        return

    def sync_task(
        self,
        task_id: str,
        *,
        task=None,
        nodes=None,
        runtime_state=None,
    ) -> None:
        return

    def sync_runtime_state(
        self,
        task_id: str,
        *,
        task=None,
        runtime_state=None,
    ) -> None:
        return
