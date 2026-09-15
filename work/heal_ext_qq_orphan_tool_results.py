"""一次性治愈 ext:qq-official:f8a8001865631301 会话固化在持久状态里的孤儿工具结果。

背景：2026-09-09 一个携带 tool_calls 的阶段块回显回合被整块丢弃（is_stage_context_message
缺 tool_calls 守卫，已于 2026-09-15 修复），其两条 role=tool 结果
（call_f7779e2c00c14bd18bcd0e7f load_tool_context / call_9260f1c92ef84776b2a6fa9a exec）
从此固化在连续性 sidecar 与轮边界快照里，每轮请求重放。代码修复不能治愈存量数据。

用法（在 G3KU-Agent 仓库根执行）：
    .venv/Scripts/python.exe work/heal_ext_qq_orphan_tool_results.py            # dry-run 只报数
    .venv/Scripts/python.exe work/heal_ext_qq_orphan_tool_results.py --apply    # 备份后改写

安全：默认 dry-run；--apply 先为每个目标文件复制 <原名>.bak-<时间戳> 再原地改写；
改写后重跑孤儿分析必须为零，否则中止。运行前确保该会话空闲（运行时可能正在读写这些文件）。
"""

from __future__ import annotations

import gzip
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from g3ku.runtime.tool_history import analyze_tool_call_history  # noqa: E402

SESSION = "ext_qq-official_f8a8001865631301"
REPO = Path(__file__).resolve().parents[1]

TARGETS = [
    REPO / ".g3ku" / "web-ceo-continuity" / f"{SESSION}.json",
    *sorted((REPO / ".g3ku" / "web-ceo-turn-boundaries" / SESSION).glob("*.json.gz")),
]

KEY = "frontdoor_request_body_messages"


def _load(path: Path) -> dict:
    raw = gzip.open(path, "rb").read() if path.suffix == ".gz" else path.read_bytes()
    return json.loads(raw.decode("utf-8"))


def _dump(path: Path, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if path.suffix == ".gz":
        with gzip.open(path, "wb") as handle:
            handle.write(raw)
    else:
        path.write_bytes(raw)


def heal(payload: dict, *, apply: bool) -> int:
    messages = payload.get(KEY)
    if not isinstance(messages, list):
        return 0
    analysis = analyze_tool_call_history(messages)
    orphan_ids = set(analysis.orphan_tool_result_ids)
    if not orphan_ids:
        return 0
    print(f"  orphan tool results: {sorted(orphan_ids)}")
    if not apply:
        print(f"  dry-run: would remove {len(orphan_ids)} tool message(s)")
        return len(orphan_ids)
    kept = [
        message
        for message in messages
        if not (
            str((message or {}).get("role") or "").strip().lower() == "tool"
            and str((message or {}).get("tool_call_id") or "").strip() in orphan_ids
        )
    ]
    payload[KEY] = kept
    return len(messages) - len(kept)


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    if not TARGETS or not TARGETS[0].exists():
        print(f"continuity sidecar not found: {TARGETS[0]}")
        return 1
    total = 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in TARGETS:
        if not path.exists():
            print(f"skip (missing): {path}")
            continue
        print(f"target: {path}")
        payload = _load(path)
        removed = heal(payload, apply=apply)
        total += removed
        if removed and apply:
            backup = path.with_name(f"{path.name}.bak-{stamp}")
            shutil.copy2(path, backup)
            _dump(path, payload)
            check = analyze_tool_call_history(_load(path).get(KEY) or [])
            if check.orphan_tool_result_ids:
                print(f"  ERROR: orphans remain after heal, restore from {backup}")
                shutil.copy2(backup, path)
                return 2
            print(f"  removed {removed} orphan message(s); backup: {backup.name}")
    print(f"TOTAL {'removed' if apply else 'to remove'}: {total} orphan message(s)")
    if not apply and total:
        print("dry-run only; re-run with --apply after confirming the session is idle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
