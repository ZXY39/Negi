#!/usr/bin/env python3
"""Minimal polling connector for a Negi external event subscription.

Replace poll_once() with the platform/source-specific check. Keep the cursor
state and delivery loop: those carry the idempotency and retry contract.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def save_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def post_event(
    *,
    base_url: str,
    credential: dict[str, Any],
    idempotency_key: str,
    event_type: str,
    text: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    url = base_url.rstrip("/") + str(credential["endpoint_path"])
    body = json.dumps(
        {
            "type": event_type,
            "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "text": text,
            "data": data,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + str(credential["token"]),
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        if exc.code in {400, 401, 403, 404, 413, 423}:
            raise RuntimeError(f"event rejected: HTTP {exc.code}: {detail}") from exc
        raise


def poll_once(cursor: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (new_events, next_cursor).

    Replace this stub. Each event must carry:
      - key: stable idempotency key (platform message id, file fingerprint, ...)
      - type: short event type
      - text: model-facing summary
      - data: JSON object with paths/ids/values the future agent needs
    """
    _ = cursor
    return [], cursor


def deliver_with_backoff(**kwargs: Any) -> dict[str, Any]:
    delay = 1.0
    while True:
        try:
            return post_event(**kwargs)
        except RuntimeError:
            raise
        except Exception as exc:  # network, timeout, 429, 5xx
            print(f"delivery failed: {exc}; retrying in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2.0, 60.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("NEGI_BASE_URL", "http://127.0.0.1:18790"))
    parser.add_argument("--credential", required=True)
    parser.add_argument("--state", default=str(Path(__file__).with_name("connector-state.json")))
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    args = parser.parse_args()

    credential_path = Path(args.credential)
    state_path = Path(args.state)
    cursor: dict[str, Any] = {}
    if state_path.exists():
        cursor = load_json(state_path)

    while True:
        events, next_cursor = poll_once(cursor)
        for event in events:
            key = str(event.get("key") or "").strip()
            if not key:
                raise RuntimeError("poll_once returned an event without a stable key")
            result = deliver_with_backoff(
                base_url=args.base_url,
                credential=load_json(credential_path),
                idempotency_key=key,
                event_type=str(event.get("type") or "external.event"),
                text=str(event.get("text") or ""),
                data=dict(event.get("data") or {}),
            )
            print(
                f"accepted event={result.get('event_id', '')} duplicate={bool(result.get('duplicate'))}",
                file=sys.stderr,
            )
        if next_cursor != cursor:
            cursor = next_cursor
            save_json_atomic(state_path, cursor)
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
