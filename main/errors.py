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


def describe_exception_chain(exc: BaseException | None, *, max_depth: int = 5) -> str:
    """把整条因果链摊开：`describe_exception` 只报最后那一跳。

    在 `except` 体里抛出的新异常会把原异常降成 `__context__`，于是「存储抖动顶替了
    看门狗要取消的那个原始异常」这类事实只剩最后一跳的名字。2026-10-15 事故里 12 条
    entry 的正文都是裸 `Error: CancelledError`，因为 `CancelledError` 的 `str()` 为空。

    只给需要一手诊断的落点用（错误行正文、结算文本、取消诊断）；日志标题与诊断 detail
    继续用 `describe_exception`——那些行有 20+ 处，全改成跟链会让正文暴涨。
    """
    if exc is None:
        return 'UnknownError'
    limit = max(1, int(max_depth or 1))
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    stopped_by_depth = False
    while current is not None:
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        parts.append(describe_exception(current))
        cause = current.__cause__
        following = cause if cause is not None else (
            None if current.__suppress_context__ else current.__context__
        )
        if following is None:
            break
        if len(parts) >= limit:
            stopped_by_depth = True
            break
        current = following
    if stopped_by_depth:
        parts.append(f'…（因果链超过 {limit} 跳，已截）')
    return ' <- '.join(parts)


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
    每次控制工具调用都抛同一个 `NameError`，空转 24 轮无人发现。
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

