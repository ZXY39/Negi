class TaskPausedError(Exception):
    pass


class NodePausedError(Exception):
    def __init__(self, task_id: str, node_id: str):
        self.task_id = str(task_id or "").strip()
        self.node_id = str(node_id or "").strip()
        super().__init__(self.task_id, self.node_id)


def describe_exception(exc: BaseException | None) -> str:
    if exc is None:
        return 'UnknownError'
    name = str(type(exc).__name__ or 'Exception').strip() or 'Exception'
    message = str(exc or '').strip()
    if message:
        return f'{name}: {message}' if message != name else name
    rendered = repr(exc).strip()
    if rendered and rendered != f'{name}()':
        return f'{name}: {rendered}'
    return name

