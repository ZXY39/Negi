class TaskPausedError(Exception):
    pass


class NodePausedError(Exception):
    def __init__(self, task_id: str, node_id: str):
        self.task_id = str(task_id or "").strip()
        self.node_id = str(node_id or "").strip()
        super().__init__(self.task_id, self.node_id)


class DistributionHoldError(Exception):
    """子树分发屏障冻结：节点在安全边界停摆等待 epoch 释放。

    与 NodePausedError 语义不同：不是操作员暂停，不落节点暂停标志；
    dispatcher 对包括根在内的所有节点都保持 future pending，由分发
    驱动器在释放时对每个 held entry 调 resume_node 恢复。
    """

    def __init__(self, task_id: str, node_id: str, epoch_id: str = ""):
        self.task_id = str(task_id or "").strip()
        self.node_id = str(node_id or "").strip()
        self.epoch_id = str(epoch_id or "").strip()
        super().__init__(self.task_id, self.node_id, self.epoch_id)


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

