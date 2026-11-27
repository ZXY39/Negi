from __future__ import annotations

import subprocess
import sys
import time

import pytest

from g3ku.utils.process_tree import kill_process_tree_checked

psutil = pytest.importorskip('psutil')


def test_checked_kill_captures_descendants_before_killing() -> None:
    """整树终止的结果要带回被扫掉的后代快照，供上层如实回报。"""
    code = (
        'import subprocess, sys, time\n'
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        'time.sleep(30)\n'
    )
    popen_kwargs: dict = {}
    if sys.platform != 'win32':
        # POSIX 整树终止只在目标是进程组组长时走 killpg；与 exec 的
        # start_new_session=True 保持同形，孙子进程才会一起被收走。
        popen_kwargs['start_new_session'] = True
    parent = subprocess.Popen([sys.executable, '-c', code], **popen_kwargs)
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if psutil.Process(parent.pid).children(recursive=True):
                    break
            except psutil.Error:
                break
            time.sleep(0.05)

        result = kill_process_tree_checked(parent)

        assert result.initiated is True
        assert result.terminated is True
        assert result.descendant_total >= 1
        pids = [int(item['pid']) for item in result.descendants]
        assert pids
        assert all(float(item['create_time']) > 0 for item in result.descendants)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=20)


def test_checked_kill_of_gone_process_reports_no_descendants() -> None:
    result = kill_process_tree_checked(None)
    assert result.initiated is False
    assert result.terminated is False
    assert result.descendants == []
