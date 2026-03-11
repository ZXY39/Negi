from __future__ import annotations

import uuid


def _short() -> str:
    return uuid.uuid4().hex[:10]


def new_project_id() -> str:
    return f"proj:{_short()}"


def new_unit_id(kind: str = "execution") -> str:
    return f"unit:{kind}:{_short()}"


def new_stage_id() -> str:
    return f"stage:{_short()}"


def new_event_id() -> str:
    return f"evt:{_short()}"


def new_artifact_id() -> str:
    return f"artifact:{_short()}"


def new_notice_id() -> str:
    return f"notice:{_short()}"

def new_policy_id() -> str:
    return f"policy:{_short()}"
