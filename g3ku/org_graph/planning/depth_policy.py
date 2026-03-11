from __future__ import annotations



def clamp_max_depth(requested: int | None, *, default_depth: int, hard_max_depth: int) -> int:
    if requested is None:
        return max(0, min(default_depth, hard_max_depth))
    return max(0, min(int(requested), hard_max_depth))



def can_delegate(level: int, effective_max_depth: int) -> bool:
    return int(level) < int(effective_max_depth)
