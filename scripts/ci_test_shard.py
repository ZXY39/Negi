"""CI 用的分片选择器：把测试文件按体量摊成 N 片，跑其中一片。

一条 `pytest tests/` 的整跑在这台仓库上量到 25 分钟以上（`166cc63d6`：43 failed /
4626 passed in 25m10s），而 `ci.yml` 的作业预算是 30 分钟、`concurrency.cancel-in-progress`
又让任何一次新推送顶掉正在跑的那次。两条加起来的结果是：main 越忙，全量读数越取不到
（2026-11 连七八个 run 都在 30 分钟整点被 cancelled，没有 summary）。分片的目的是让
单片跑在"两次推送之间"就结束，把红数真正读出来。

分法按文件字节数贪心装箱，不按文件数：单个文件的用例量差一个数量级
（`tests/resources/test_task_web_worker_runtime.py` 一个文件 220+ 条），按条数摊会让
某一片单独超时。字节数是比文件名更稳的代理，且完全确定——同一条树切出来的片号一致。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = REPO_ROOT / "tests"
PREFIX = "test_*.py"


def collect_files() -> list[Path]:
    return sorted(
        path
        for path in TEST_ROOT.rglob(PREFIX)
        if "__pycache__" not in path.parts and path.is_file()
    )


def plan_shards(files: list[Path], shards: int) -> list[list[Path]]:
    """把文件按体量装进 N 个箱：大的先进，每次给当前最空的箱。"""
    if shards < 1:
        raise ValueError("shards must be >= 1")
    buckets: list[list[Path]] = [[] for _ in range(shards)]
    loads: list[int] = [0] * shards
    for path in sorted(files, key=lambda item: item.stat().st_size, reverse=True):
        target = loads.index(min(loads))
        buckets[target].append(path)
        loads[target] += path.stat().st_size
    return [sorted(bucket) for bucket in buckets]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, required=True, help="这一片，1 起")
    parser.add_argument("--shards", type=int, default=4, help="总片数")
    parser.add_argument("--plan-only", action="store_true", help="只打印分片计划，不跑测试")
    args = parser.parse_args(argv)
    if args.shard < 1 or args.shard > args.shards:
        print(f"shard {args.shard} is outside 1..{args.shards}", file=sys.stderr)
        return 2

    files = collect_files()
    if not files:
        print("no test files found under tests/", file=sys.stderr)
        return 2
    buckets = plan_shards(files, args.shards)
    picked = buckets[args.shard - 1]
    total_bytes = sum(path.stat().st_size for path in files)
    print(
        f"shard {args.shard}/{args.shards}: {len(picked)} of {len(files)} files "
        f"({sum(p.stat().st_size for p in picked)} of {total_bytes} bytes)",
        flush=True,
    )
    for path in picked:
        print(f"  {path.relative_to(REPO_ROOT).as_posix()}", flush=True)
    if args.plan_only:
        return 0

    targets = [str(path) for path in picked]
    print(f"running pytest on {len(targets)} explicit file arguments", flush=True)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *targets], cwd=str(REPO_ROOT)).returncode


if __name__ == "__main__":
    sys.exit(main())
