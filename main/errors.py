class TaskPausedError(Exception):
    pass


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

