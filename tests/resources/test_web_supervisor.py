import importlib.util
import os
import pathlib

REPO_ROOT = pathlib.Path(os.path.dirname(os.path.abspath(__file__))).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("negi_bootstrap", REPO_ROOT / "negi_bootstrap.py")
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


class FakeChild:
    def __init__(self, code):
        self._code = code

    def wait(self):
        return self._code


def supervise(initial, spawned_codes, clock_values=None):
    spawned = []
    sleeps = []
    ticks = iter(clock_values or [0.0] * 64)

    def spawn():
        child = FakeChild(spawned_codes[len(spawned)])
        spawned.append(child)
        return child

    code = bs._supervise_web_child(
        FakeChild(initial),
        ["web"],
        port=18790,
        hidden=True,
        console_log_stream=None,
        spawn=spawn,
        wait_ready=lambda child, port: True,
        sleep=sleeps.append,
        clock=lambda: next(ticks),
    )
    return code, spawned, sleeps


def test_clean_exit_ends_supervision():
    code, spawned, _ = supervise(0, [])
    assert code == 0
    assert spawned == []


def test_crash_respawns_until_clean_exit():
    code, spawned, sleeps = supervise(1, [0])
    assert code == 0
    assert len(spawned) == 1
    assert sleeps == [1.0]


def test_crash_storm_gives_up_after_limit():
    code, spawned, sleeps = supervise(1, [1, 1, 1, 1, 1])
    assert code == 1
    assert len(spawned) == bs.SUPERVISE_MAX_RESPAWNS
    assert sleeps == list(bs.SUPERVISE_BACKOFF_SECONDS)


def test_window_expiry_resets_crash_budget():
    # 每次崩溃间隔超过窗口 → 永不熔断；第 7 次干净退出收尾。
    code, spawned, _ = supervise(1, [1, 1, 1, 1, 1, 1, 0], clock_values=[i * 1000.0 for i in range(64)])
    assert code == 0
    assert len(spawned) == 7
