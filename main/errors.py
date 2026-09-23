import os


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


_RUNTIME_SELF_FAULT_TYPES = frozenset({
    'NameError',
    'UnboundLocalError',
    'AttributeError',
    'ImportError',
    'TypeError',
})


def is_runtime_self_fault(exc: BaseException | None) -> bool:
    """区分"运行时自身缺陷"与"工具用法错误"。

    只认最后一帧落在本仓库运行时包（`main/`、`g3ku/`）内的那批异常：炸在 `tools/`
    里是工具实现的输入校验问题，炸在标准库或三方库里多半是数据问题，两者都不该
    被当成运行时坏了。2026-09-23 事故里 worker 进程加载了改到一半的 `log_service.py`，
    每次 `submit_next_stage` 抛同一个 `NameError`，空转 24 轮无人发现。
    """
    if exc is None:
        return False
    if type(exc).__name__ not in _RUNTIME_SELF_FAULT_TYPES:
        return False
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    owned_prefixes = (
        os.path.join(root, 'main') + os.sep,
        os.path.join(root, 'g3ku') + os.sep,
    )
    last_file = ''
    traceback_frame = exc.__traceback__
    while traceback_frame is not None:
        last_file = str(traceback_frame.tb_frame.f_code.co_filename or '')
        traceback_frame = traceback_frame.tb_next
    if not last_file:
        return False
    normalized = os.path.abspath(last_file) + os.sep
    return any(normalized.startswith(prefix) for prefix in owned_prefixes)

