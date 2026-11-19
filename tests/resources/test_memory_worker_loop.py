import asyncio
import os
import sys
import threading

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from g3ku.agent.memory_agent_runtime import run_memory_worker_loop


def test_one_persistent_loop_across_batches_and_clean_stop():
    stop = threading.Event()
    loops: list[asyncio.AbstractEventLoop] = []
    batches: list[int] = []

    async def batch():
        loops.append(asyncio.get_running_loop())
        batches.append(1)
        if len(batches) >= 3:
            stop.set()

    async def sweep():
        loops.append(asyncio.get_running_loop())

    t = threading.Thread(target=run_memory_worker_loop, args=(stop, batch, sweep), daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "worker thread did not stop"
    assert len(batches) == 3
    assert len(loops) >= 3
    assert all(loop is loops[0] for loop in loops), "loop identity must be stable across batches"
    assert loops[0].is_closed(), "loop must be closed on thread exit"


def test_batch_exception_keeps_worker_alive_and_skips_sweep():
    stop = threading.Event()
    calls: list[str] = []

    async def batch():
        calls.append("batch")
        if len(calls) == 1:
            raise RuntimeError("batch boom")
        stop.set()

    async def sweep():
        calls.append("sweep")

    t = threading.Thread(target=run_memory_worker_loop, args=(stop, batch, sweep), daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive()
    # 第一轮 batch 抛错 → 当轮 sweep 被跳过；第二轮 batch 成功后 sweep 照跑。
    assert calls == ["batch", "batch", "sweep"], "sweep is skipped only on the exception round"
