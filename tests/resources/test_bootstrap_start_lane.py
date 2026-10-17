import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import negi_bootstrap

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeChild:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self):
        self.wait_calls += 1
        return self.returncode or 0


def _patch_env(monkeypatch, config=None):
    monkeypatch.setattr(negi_bootstrap, 'os', SimpleNamespace(
        name=os.name, environ={}, chdir=lambda _path: None
    ))
    monkeypatch.setattr(negi_bootstrap, '_ensure_venv', lambda: None)
    monkeypatch.setattr(negi_bootstrap, '_ensure_project_installed', lambda: None)
    monkeypatch.setattr(negi_bootstrap, '_rotate_runtime_console_log', lambda: None)
    monkeypatch.setattr(negi_bootstrap, '_load_bootstrap_config', lambda: dict(config or {}))


def test_uv_probe_falls_back_to_the_installer_locations(monkeypatch, tmp_path) -> None:
    # astral 安装器把 uv 写进 ~/.local/bin 再追加用户 PATH，而 PATH 变更对已经开着的终端
    # 不可见；只认 which 会让新装设备的首启停在「No usable package installer」。
    # 兜底候选必须与 install.ps1 / install.sh 的 Get-UvCommand 一致。
    fake_home = tmp_path / "home"
    expected = fake_home / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    expected.parent.mkdir(parents=True)
    expected.write_text("")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    assert negi_bootstrap._uv_executable() == str(expected)

    # 两处都没有才是真的没有：仍回 None，让终止横幅照原样说清缺什么。
    missing = tmp_path / "empty"
    monkeypatch.setenv("HOME", str(missing))
    monkeypatch.setenv("USERPROFILE", str(missing))
    assert negi_bootstrap._uv_executable() is None


def test_port_override_reads_all_three_shapes() -> None:
    assert negi_bootstrap._port_override(['web', '--port', '9000']) == 9000
    assert negi_bootstrap._port_override(['web', '-p', '9001']) == 9001
    assert negi_bootstrap._port_override(['web', '--port=9002']) == 9002
    assert negi_bootstrap._port_override(['web']) is None
    assert negi_bootstrap._port_override(['web', '--port', 'abc']) is None


def test_web_bind_port_prefers_the_port_being_forwarded(monkeypatch) -> None:
    # 探针盯的必须是子进程真正要绑的端口：只看 config 会让非默认端口在
    # 服务正常的情况下报「启动失败」。
    monkeypatch.setattr(negi_bootstrap, '_load_bootstrap_config', lambda: {'web': {'port': 18790}})
    assert negi_bootstrap._web_bind_port(9000) == 9000
    assert negi_bootstrap._web_bind_port() == 18790
    monkeypatch.setattr(negi_bootstrap, '_load_bootstrap_config', lambda: {})
    assert negi_bootstrap._web_bind_port() == negi_bootstrap.DEFAULT_WEB_PORT


def test_hidden_flag_is_consumed_not_forwarded() -> None:
    args, hidden = negi_bootstrap._take_hidden_flag(['--hidden', '--port', '9000'])
    assert hidden is True
    assert args == ['--port', '9000']


def test_start_opens_browser_without_spawning_when_ready(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(negi_bootstrap, '_load_bootstrap_config', lambda: {'web': {'port': 18790}})
    monkeypatch.setattr(negi_bootstrap, '_probe_web_ready', lambda port: True)
    monkeypatch.setattr(negi_bootstrap, '_open_browser', lambda url: opened.append(url) or True)

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError('must not start a second server while one already answers')

    monkeypatch.setattr(negi_bootstrap, '_spawn_web_runtime', _no_spawn)

    assert negi_bootstrap._run_start([]) == 0
    assert opened == ['http://127.0.0.1:18790/']


def test_start_binds_the_url_to_loopback_even_when_config_binds_all(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(
        negi_bootstrap, '_load_bootstrap_config', lambda: {'web': {'host': '0.0.0.0', 'port': 18790}}
    )
    monkeypatch.setattr(negi_bootstrap, '_probe_web_ready', lambda port: True)
    monkeypatch.setattr(negi_bootstrap, '_open_browser', lambda url: opened.append(url) or True)

    assert negi_bootstrap._run_start([]) == 0
    assert opened == ['http://127.0.0.1:18790/']


def test_cold_start_detaches_so_the_window_can_close(monkeypatch) -> None:
    # 他要求的语义：敲 `negi` 起的服务不依附这个终端，关掉窗口项目照跑。
    spawned = {}
    opened = []
    monkeypatch.setattr(negi_bootstrap, '_load_bootstrap_config', lambda: {})
    monkeypatch.setattr(negi_bootstrap, '_probe_web_ready', lambda port: False)
    monkeypatch.setattr(negi_bootstrap, '_open_runtime_console_log_stream', lambda: None)
    monkeypatch.setattr(negi_bootstrap, '_open_browser', lambda url: opened.append(url) or True)
    monkeypatch.setattr(negi_bootstrap, '_wait_for_web_ready', lambda child, port: True)

    def _capture(args, *, hidden, console_log_stream):
        spawned['args'] = list(args)
        spawned['hidden'] = hidden
        return _FakeChild()

    monkeypatch.setattr(negi_bootstrap, '_spawn_web_runtime', _capture)

    assert negi_bootstrap._run_start(['--port', '9555']) == 0
    assert spawned['args'] == ['web', '--port', '9555']
    assert spawned['hidden'] is True
    assert opened == ['http://127.0.0.1:9555/']


def test_start_still_consumes_the_hidden_flag_of_older_shortcuts(monkeypatch) -> None:
    spawned = {}
    monkeypatch.setattr(negi_bootstrap, '_load_bootstrap_config', lambda: {})
    monkeypatch.setattr(negi_bootstrap, '_probe_web_ready', lambda port: False)
    monkeypatch.setattr(negi_bootstrap, '_open_runtime_console_log_stream', lambda: None)
    monkeypatch.setattr(negi_bootstrap, '_open_browser', lambda _url: True)
    monkeypatch.setattr(negi_bootstrap, '_wait_for_web_ready', lambda child, port: True)
    monkeypatch.setattr(
        negi_bootstrap,
        '_spawn_web_runtime',
        lambda args, **kwargs: (spawned.update(args=list(args)), _FakeChild())[1],
    )

    assert negi_bootstrap._run_start(['--hidden', '--port', '9556']) == 0
    assert '--hidden' not in spawned['args']
    assert spawned['args'] == ['web', '--port', '9556']


def test_hidden_start_returns_without_waiting_for_the_server(monkeypatch) -> None:
    child = _FakeChild()
    monkeypatch.setattr(negi_bootstrap, '_open_runtime_console_log_stream', lambda: None)
    monkeypatch.setattr(negi_bootstrap, '_spawn_web_runtime', lambda *_a, **_k: child)
    monkeypatch.setattr(negi_bootstrap, '_wait_for_web_ready', lambda _child, _port: True)
    monkeypatch.setattr(negi_bootstrap, '_open_browser', lambda _url: True)

    assert negi_bootstrap._launch_web(['web'], port=9000, open_browser=True, hidden=True) == 0
    assert child.wait_calls == 0


def test_foreground_start_waits_for_the_server_to_exit(monkeypatch) -> None:
    child = _FakeChild(returncode=3)
    monkeypatch.setattr(negi_bootstrap, '_open_runtime_console_log_stream', lambda: None)
    monkeypatch.setattr(negi_bootstrap, '_spawn_web_runtime', lambda *_a, **_k: child)
    monkeypatch.setattr(negi_bootstrap, '_wait_for_web_ready', lambda _child, _port: True)

    assert negi_bootstrap._launch_web(['web'], port=9000, open_browser=False, hidden=False) == 3
    assert child.wait_calls == 1


def test_failed_start_reports_the_failure_and_opens_nothing(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(negi_bootstrap, '_open_runtime_console_log_stream', lambda: None)
    monkeypatch.setattr(negi_bootstrap, '_spawn_web_runtime', lambda *_a, **_k: _FakeChild(returncode=1))
    monkeypatch.setattr(negi_bootstrap, '_wait_for_web_ready', lambda _child, _port: False)
    monkeypatch.setattr(negi_bootstrap, '_open_browser', lambda url: opened.append(url) or True)

    assert negi_bootstrap._launch_web(['web'], port=9000, open_browser=True, hidden=False) == 1
    assert opened == []


def test_spawned_child_keeps_the_cmdline_shape_the_reaper_matches(monkeypatch) -> None:
    captured = {}

    class _Popen:
        def __init__(self, argv, **kwargs):
            captured['argv'] = list(argv)
            captured['kwargs'] = kwargs

    monkeypatch.setattr(negi_bootstrap.subprocess, 'Popen', _Popen)

    negi_bootstrap._spawn_web_runtime(['web', '--port', '9000'], hidden=True, console_log_stream=None)

    assert captured['argv'][1:4] == ['-m', 'g3ku', 'web']
    if os.name == 'nt':
        assert captured['kwargs']['creationflags'] == (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        assert captured['kwargs']['start_new_session'] is True


def test_main_defaults_to_the_start_lane(monkeypatch) -> None:
    routed = []
    _patch_env(monkeypatch)
    monkeypatch.setattr(negi_bootstrap, '_run_start', lambda extra: routed.append(extra) or 0)

    assert negi_bootstrap.main([]) == 0
    assert routed == [[]]


def test_main_routes_web_with_its_own_port_and_no_browser(monkeypatch) -> None:
    seen = {}
    _patch_env(monkeypatch)

    def _capture(web_args, *, port, open_browser, hidden):
        seen.update(args=list(web_args), port=port, open_browser=open_browser, hidden=hidden)
        return 0

    monkeypatch.setattr(negi_bootstrap, '_launch_web', _capture)

    assert negi_bootstrap.main(['web', '--port', '9123']) == 0
    assert seen == {
        'args': ['web', '--port', '9123'],
        'port': 9123,
        'open_browser': False,
        'hidden': False,
    }


def test_main_passes_other_commands_straight_through(monkeypatch) -> None:
    calls = []
    _patch_env(monkeypatch)

    def _run(command, **kwargs):
        calls.append((list(command), kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(negi_bootstrap.subprocess, 'run', _run)

    assert negi_bootstrap.main(['status']) == 0
    assert calls[0][0][1:3] == ['-m', 'g3ku']
    assert calls[0][0][3:] == ['status']


def _call_spans(source: str, needle: str) -> list[str]:
    """Each occurrence of `needle` up to its matching close paren."""
    spans = []
    start = source.find(needle)
    while start != -1:
        depth = 0
        index = start + len(needle) - 1
        while index < len(source):
            char = source[index]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    break
            index += 1
        spans.append(source[start:index + 1])
        start = source.find(needle, index + 1)
    return spans


def test_every_probe_child_is_told_not_to_open_a_console() -> None:
    # 图标用 pythonw 起 bootstrap：它自己没有控制台可继承，所以每个探测子进程
    # （解释器版本探测、依赖探测、uv sync）都会另开一个黑窗，用户看到的是连弹几
    # 下才出浏览器。真终端下加这个标记不改变行为——子进程照样继承父控制台。
    source = (REPO_ROOT / 'negi_bootstrap.py').read_text(encoding='utf-8')
    spans = _call_spans(source, 'subprocess.run(')
    assert spans, 'expected probe calls in negi_bootstrap.py'
    unflagged = [span.splitlines()[0] for span in spans if '_no_window_flags()' not in span]
    assert unflagged == [], unflagged


def test_probe_helpers_pass_the_flag_at_runtime(monkeypatch) -> None:
    captured = {}

    def _fake_run(command, **kwargs):
        captured['kwargs'] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(negi_bootstrap.subprocess, 'run', _fake_run)
    negi_bootstrap._is_runnable_python(REPO_ROOT / 'negi_bootstrap.py')
    if os.name == 'nt':
        assert captured['kwargs'].get('creationflags') == negi_bootstrap.subprocess.CREATE_NO_WINDOW
    else:
        assert 'creationflags' not in captured['kwargs']
