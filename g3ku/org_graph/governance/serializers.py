from __future__ import annotations


def paginated_payload(items: list[dict], *, offset: int, limit: int) -> dict:
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(int(limit), 500))
    total = len(items)
    return {
        'items': items[safe_offset:safe_offset + safe_limit],
        'total': total,
        'offset': safe_offset,
        'limit': safe_limit,
    }
