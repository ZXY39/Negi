import json
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
        # 后台化必须用 CREATE_NO_WINDOW，不能是 DETACHED_PROCESS：venv 的 python.exe
        # 是重定向器（实测启动 PID != 运行 PID），真解释器是它的子进程。父进程完全没有
        # 控制台时，Windows 会给那个子进程新建一个可见控制台——而它就是服务本身，
        # 用户关掉"多出来的黑窗"等于杀掉 Negi。
        flags = captured['kwargs']['creationflags']
        assert flags == subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        assert not flags & subprocess.DETACHED_PROCESS
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
    monkeypatch.setattr(negi_bootstrap, 'reap_existing_runtime_processes', lambda port, **_k: {'remaining': []})
    monkeypatch.setattr(negi_bootstrap, '_wait_port_quiet', lambda port, **_k: True)

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


def test_probe_children_are_quiet_and_the_command_handoff_is_not(monkeypatch) -> None:
    # 探测子进程（版本/依赖检查、uv sync）不许自己开控制台：图标那条的宿主是
    # pythonw，没有可继承的控制台，每个探测都会弹一个黑窗。
    # 但把命令交给 `-m g3ku ...` 那一处必须反过来：实测带上 CREATE_NO_WINDOW 后
    # 子进程拿不到终端句柄，`negi status` / `--version` 会一个字都不输出。
    source = (REPO_ROOT / 'negi_bootstrap.py').read_text(encoding='utf-8')
    spans = _call_spans(source, 'subprocess.run(')
    assert spans, 'expected probe calls in negi_bootstrap.py'
    handoffs = [span for span in spans if '"-m", "g3ku", *args' in span]
    probes = [span for span in spans if '"-m", "g3ku", *args' not in span]
    assert len(handoffs) == 1, handoffs
    assert all('_no_window_flags()' in span for span in probes), probes
    assert '_no_window_flags()' not in handoffs[0]

    captured = {}

    def _fake_run(command, **kwargs):
        captured['kwargs'] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(negi_bootstrap.subprocess, 'run', _fake_run)
    negi_bootstrap.main(['status'])
    assert 'creationflags' not in captured['kwargs']


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


def test_web_bind_default_is_loopback_and_the_container_lane_stays_explicit() -> None:
    """出厂默认只绑回环：管理面没有鉴权依赖，`0.0.0.0` 等于把删任务/改密钥开放给整网段。

    容器车道要对外是它自己显式传 `--host 0.0.0.0`，不靠默认值——所以这个默认改动
    必须同时被钉住"容器那侧仍然显式对外"，否则下次读到默认值的人会以为漏了什么。
    """
    from pathlib import Path

    from g3ku.config.schema import WebConfig

    assert WebConfig().host == "127.0.0.1"
    assert WebConfig().port == 18790

    repo_root = Path(__file__).resolve().parents[2]
    entrypoint = (repo_root / "docker" / "web-entrypoint.sh").read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in entrypoint


# ---------------------------------------------------------------------------
# 入口脚本与旧实例回收：回收从 `start-negi.ps1` 搬进解释器之后，三条入口（.cmd / .ps1 /
# `negi web`）共用同一份语义。.ps1 可能在执行前就被整份拒掉（实测：带低完整性标签的未签名
# 脚本，即使策略是 RemoteSigned 也报 UnauthorizedAccess），那时只有不依赖 PowerShell 的这条道
# 还能起。


def test_entry_switches_are_translated_in_one_place(monkeypatch) -> None:
    monkeypatch.setattr(negi_bootstrap, 'os', SimpleNamespace(
        name=os.name, environ={'G3KU_PROMPT_TRACE': '1', 'G3KU_WEB_KEEP_WORKER': '1'}, chdir=lambda _p: None
    ))

    args, want_browser, wants_help = negi_bootstrap._apply_web_entry_flags([
        '-BindHost', '0.0.0.0', '-Port', '9123', '-OpenBrowser', '-PromptLog', '-KeepWorker', '-Reload',
    ])

    assert wants_help is False
    assert want_browser is True
    # 交给 `python -m g3ku web` 的必须是 CLI 形状；PowerShell 形状只活在入口脚本那一段。
    assert args == ['--host', '0.0.0.0', '--port', '9123', '--reload']
    assert negi_bootstrap.os.environ['G3KU_PROMPT_TRACE'] == '1'
    assert negi_bootstrap.os.environ['G3KU_WEB_KEEP_WORKER'] == '1'

    # 没给开关时两个变量要被清掉：子进程继承的是这一份环境，留着就是"这次没要求却还在记提示词"。
    plain_args, plain_browser, _ = negi_bootstrap._apply_web_entry_flags(['--port', '9124'])
    assert plain_args == ['--port', '9124']
    assert plain_browser is False
    assert 'G3KU_PROMPT_TRACE' not in negi_bootstrap.os.environ
    assert 'G3KU_WEB_KEEP_WORKER' not in negi_bootstrap.os.environ


def test_entry_help_prints_usage_without_touching_the_port(monkeypatch) -> None:
    _patch_env(monkeypatch)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError('help must not reap instances or bind anything')

    monkeypatch.setattr(negi_bootstrap, 'reap_existing_runtime_processes', _forbidden)
    monkeypatch.setattr(negi_bootstrap, '_launch_web', _forbidden)

    assert negi_bootstrap.main(['web', '-Help']) == 0


def test_port_override_prefers_the_last_occurrence() -> None:
    # `start-negi.cmd` 把默认端口写在前面、操作员的追在后面；取第一次会让回收与就绪探测
    # 盯着默认端口，而服务实际绑在自定义端口上，看着就像"起不来"。
    assert negi_bootstrap._port_override(['web', '--port', '18790', '--port', '9555']) == 9555


def test_reaper_matches_all_three_shapes_but_only_this_checkout() -> None:
    root = str(negi_bootstrap.PROJECT_ROOT)
    assert negi_bootstrap._runtime_cmdline_is_managed(f'{root}\\.venv\\Scripts\\python.exe -m g3ku web', root=root)
    assert negi_bootstrap._runtime_cmdline_is_managed(f'{root}/.venv/bin/python -m g3ku worker', root=root)
    assert negi_bootstrap._runtime_cmdline_is_managed(f'{root}/.venv/bin/python negi_bootstrap.py web', root=root)
    # 形状对但别的安装：不收，否则一台机器两个 checkout 会互相杀。
    assert not negi_bootstrap._runtime_cmdline_is_managed(
        'C:\\other\\Negi\\.venv\\bin\\python -m g3ku web', root=root
    )
    assert not negi_bootstrap._runtime_cmdline_is_managed(f'{root}\\.venv\\Scripts\\python.exe -m g3ku status', root=root)


def test_reap_stops_the_old_instance_gracefully_without_killing(monkeypatch) -> None:
    states = [[11, 12], [], [], []]
    calls = []
    monkeypatch.setattr(negi_bootstrap, '_managed_runtime_processes', lambda: list(states.pop(0) if states else []))
    monkeypatch.setattr(negi_bootstrap, '_request_runtime_exit', lambda port, **_k: calls.append(port) or True)

    def _no_kill(pid):
        raise AssertionError(f'graceful exit worked; must not force-kill pid {pid}')

    monkeypatch.setattr(negi_bootstrap, '_terminate_runtime_process', _no_kill)

    lines: list[str] = []
    report = negi_bootstrap.reap_existing_runtime_processes(18790, graceful_timeout=0.2, print_lines=lines)

    assert calls == [18790]
    assert report['found'] == [11, 12]
    assert report['graceful'] is True
    assert report['remaining'] == []
    assert any('Restarting existing Negi' in line for line in lines)


def test_reap_force_kills_only_the_pids_it_asked_to_stop(monkeypatch) -> None:
    # 快照之后新出现的 PID 不动手：那是别人家的（或刚被拉起的）进程，只收拾"我让它退而它没退"的。
    killed = []
    states = [[21, 22], [21, 22, 30], [30], [30]]
    monkeypatch.setattr(negi_bootstrap, '_managed_runtime_processes', lambda: list(states.pop(0) if states else []))
    monkeypatch.setattr(negi_bootstrap, '_request_runtime_exit', lambda port, **_k: False)
    monkeypatch.setattr(negi_bootstrap, '_terminate_runtime_process', lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(negi_bootstrap.time, 'sleep', lambda _s: None)

    lines: list[str] = []
    report = negi_bootstrap.reap_existing_runtime_processes(18790, graceful_timeout=0.0, print_lines=lines)

    assert sorted(killed) == [21, 22]
    assert report['graceful'] is False
    assert report['remaining'] == [30]


def test_reaper_never_targets_the_launcher_itself(monkeypatch) -> None:
    # 入口用绝对路径调 `negi_bootstrap.py` 时，启动器自己的命令行同时命中「checkout 根」
    # 与 `negi_bootstrap.py web` 两条判据。把自己算进回收名单，`taskkill /T` 会在回收途中
    # 杀掉回收者本身：入口以 exit 1 静默消失、一个字都不打印，看起来像"脚本起不来"
    # （实测 `start-negi.cmd` 必现；同一个文件用相对路径调用时不带 checkout 根，所以不复现）。
    # 要排除的是一对 PID：venv 的 python.exe 是重定向器，cmdline 命中判据的是父进程，
    # 真正跑这段代码的是它的子进程（启动 PID ≠ 运行 PID）。
    self_pids = [os.getpid(), os.getppid()]
    other = max(self_pids) + 1000
    if os.name == 'nt':
        stdout = ''.join(f'{pid}\r\n' for pid in self_pids) + f'{other}\r\n'
    else:
        root = str(negi_bootstrap.PROJECT_ROOT)
        stdout = ''.join(
            f'{pid} {root}/.venv/bin/python -m g3ku web\n' for pid in [*self_pids, other]
        )
    monkeypatch.setattr(
        negi_bootstrap,
        '_run_capture',
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout, ''),
    )
    assert negi_bootstrap._managed_runtime_processes() == [other]

    killed = []
    monkeypatch.setattr(negi_bootstrap, '_request_runtime_exit', lambda _port, **_k: False)
    monkeypatch.setattr(
        negi_bootstrap, '_terminate_runtime_process', lambda pid: killed.append(pid) or True
    )
    monkeypatch.setattr(negi_bootstrap.time, 'sleep', lambda _s: None)

    report = negi_bootstrap.reap_existing_runtime_processes(18790, graceful_timeout=0.0)

    assert report['found'] == [other]
    assert killed == [other]


def test_reap_asks_the_old_port_when_the_operator_changed_the_target_port(monkeypatch) -> None:
    # 回收按 checkout 收、不按端口收：这次换 `-Port 18800` 时旧实例还听在 18790 上。
    # 只打新端口等于没发请求，回收会退化成强杀——那是异常中断，正是优雅退出要避免的形状。
    asked = []
    states = [[11], []]
    monkeypatch.setattr(negi_bootstrap, '_managed_runtime_processes', lambda: list(states.pop(0) if states else []))
    monkeypatch.setattr(negi_bootstrap, '_recorded_runtime_port', lambda: 0)
    monkeypatch.setattr(negi_bootstrap, '_load_bootstrap_config', lambda: {'web': {'port': 18790}})
    monkeypatch.setattr(
        negi_bootstrap, '_request_runtime_exit', lambda port, **_k: asked.append(port) or port == 18790
    )

    def _no_kill(pid):
        raise AssertionError(f'graceful exit worked; must not force-kill pid {pid}')

    monkeypatch.setattr(negi_bootstrap, '_terminate_runtime_process', _no_kill)
    monkeypatch.setattr(negi_bootstrap.time, 'sleep', lambda _s: None)

    report = negi_bootstrap.reap_existing_runtime_processes(18800, graceful_timeout=0.1)

    assert asked == [18800, 18790]
    assert report['graceful'] is True


def test_reap_reads_the_port_the_old_instance_recorded(monkeypatch, tmp_path) -> None:
    # 旧实例启动时会把含端口的回调地址写进 `.g3ku/internal-callback.json`（worker 回传终态用）。
    # 配置还是 18790、旧实例却在 18800 上时，这是唯一能拿到旧端口的读数。
    (tmp_path / '.g3ku').mkdir()
    (tmp_path / '.g3ku' / 'internal-callback.json').write_text(
        json.dumps({'url': 'http://127.0.0.1:18800/api/internal/task-terminal', 'token': 't'}),
        encoding='utf-8',
    )
    monkeypatch.setattr(negi_bootstrap, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(negi_bootstrap, '_load_bootstrap_config', lambda: {'web': {'port': 18790}})

    assert negi_bootstrap._runtime_exit_ports(18790) == [18790, 18800]


def test_reap_does_not_report_a_pid_that_vanished_during_the_kill(monkeypatch) -> None:
    # `taskkill /T` 收父进程时子进程已经跟着走了，排在后面的子 PID 再杀一次会报
    # "找不到进程"；只有**杀完仍活着**才算失败，否则每次重启都刷一行假故障。
    lines: list[str] = []
    states = [[21], [21], []]
    monkeypatch.setattr(negi_bootstrap, '_managed_runtime_processes', lambda: list(states.pop(0) if states else []))
    monkeypatch.setattr(negi_bootstrap, '_request_runtime_exit', lambda _port, **_k: False)
    monkeypatch.setattr(negi_bootstrap, '_terminate_runtime_process', lambda _pid: False)
    monkeypatch.setattr(negi_bootstrap, '_process_exists', lambda _pid: False)
    monkeypatch.setattr(negi_bootstrap.time, 'sleep', lambda _s: None)

    report = negi_bootstrap.reap_existing_runtime_processes(18790, graceful_timeout=0.0, print_lines=lines)

    assert report['forced'] == [21]
    assert report['remaining'] == []
    assert not any('Failed to stop' in line for line in lines)


def test_main_refuses_to_start_when_a_second_instance_would_fight_the_port(monkeypatch) -> None:
    _patch_env(monkeypatch)
    monkeypatch.setattr(
        negi_bootstrap,
        '_launch_web',
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('must not spawn while the port is held')),
    )
    monkeypatch.setattr(
        negi_bootstrap, 'reap_existing_runtime_processes', lambda port, **_k: {'found': [5], 'remaining': [5]}
    )

    assert negi_bootstrap.main(['web']) == 1


def test_the_cmd_entry_boots_without_powershell_and_the_ps1_keeps_no_reaper() -> None:
    """入口契约：`.cmd` 不碰 PowerShell，`.ps1` 不再自带回收逻辑。

    `.ps1` 可能在执行前就被整份拒掉（实测：带低完整性标签的未签名脚本，即使策略是
    RemoteSigned 也报 `UnauthorizedAccess`），所以"双击/命令行起不来"必须有第二条道；
    第二条道同时得带上回收，否则旧实例占着端口与新锁，起得来的那个反而是在坏状态上跑。
    """
    cmd = (REPO_ROOT / 'start-negi.cmd').read_text(encoding='utf-8')
    ps1 = (REPO_ROOT / 'start-negi.ps1').read_text(encoding='utf-8')

    assert '-executionpolicy' not in cmd.lower() and '-file' not in cmd.lower()
    assert 'negi_bootstrap.py' in cmd and ' web ' in cmd
    for gone in ('Stop-NegiManagedPythonProcesses', 'Assert-StartPreconditions', 'Get-NegiManagedPythonProcesses'):
        assert gone not in ps1, gone
    # 回收只剩解释器一处。
    assert 'reap_existing_runtime_processes' in (REPO_ROOT / 'negi_bootstrap.py').read_text(encoding='utf-8')


def test_uv_sync_keeps_dev_extras_when_present(monkeypatch, tmp_path):
    """不带 --extra dev 的 sync 会把开发机上装好的 pytest/ruff 裁掉。

    实机后果：`uv sync --frozen --extra dev` 装好后跑一次 `negi`，下一轮就
    `No module named pytest`，而 AGENTS.md 要求改码前后跑 lint 与 pytest。
    """
    calls = []
    monkeypatch.setattr(negi_bootstrap, '_marker_matches_current', lambda: False)
    monkeypatch.setattr(negi_bootstrap, '_venv_has_runtime_deps', lambda: False)
    monkeypatch.setattr(negi_bootstrap, '_uv_executable', lambda: 'uv')
    monkeypatch.setattr(negi_bootstrap, '_pyproject_fingerprint', lambda: 'fp')
    monkeypatch.setattr(negi_bootstrap, 'BOOTSTRAP_MARKER', tmp_path / 'marker')
    monkeypatch.setattr(negi_bootstrap, '_run', lambda argv, **kw: calls.append(list(argv)))

    monkeypatch.setattr(negi_bootstrap, '_python_has_module', lambda python, module: module == 'pytest')
    negi_bootstrap._ensure_project_installed()
    assert calls == [['uv', 'sync', '--frozen', '--extra', 'dev']]

    # 装机形态（venv 里没有 pytest）保持原样，不多带 extras。
    calls.clear()
    monkeypatch.setattr(negi_bootstrap, '_python_has_module', lambda python, module: False)
    negi_bootstrap._ensure_project_installed()
    assert calls == [['uv', 'sync', '--frozen']]
