from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class OrgGraphError(Exception):
    code: str
    message: str
    http_status: int = 400

    def __str__(self) -> str:
        return self.message


class OrgGraphNotFoundError(OrgGraphError):
    def __init__(self, code: str, message: str):
        super().__init__(code=code, message=message, http_status=404)


class OrgGraphConflictError(OrgGraphError):
    def __init__(self, code: str, message: str):
        super().__init__(code=code, message=message, http_status=409)


class OrgGraphDepthLimitError(OrgGraphConflictError):
    def __init__(self, message: str = "Depth limit reached"):
        super().__init__(code="depth_limit_reached", message=message)


class PermissionBlockedError(OrgGraphConflictError):
    def __init__(self, message: str = "Permission request pending"):
        super().__init__(code="permission_blocked", message=message)


class PermissionDeniedError(OrgGraphConflictError):
    def __init__(self, message: str = "Permission denied"):
        super().__init__(code="permission_denied", message=message)


class OrdinaryTaskFailureError(RuntimeError):
    pass


class EngineeringFailureError(RuntimeError):
    pass


class ModelChainUnavailableError(RuntimeError):
    pass
