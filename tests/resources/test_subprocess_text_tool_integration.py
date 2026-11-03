from __future__ import annotations

import pytest

import g3ku.agent.tools.filesystem_mutation as filesystem_mutation_module
import g3ku.utils.subprocess_text as subprocess_text_module
from g3ku.agent.tools.filesystem_mutation import FilesystemTool


@pytest.mark.asyncio
async def test_filesystem_validation_command_decodes_legacy_codepage_output_and_sets_pythonioencoding(monkeypatch, tmp_path) -> None:
    class _StubProcess:
        returncode = 1

        async def communicate(self):
            return (b'', '站点经验\n'.encode('gbk'))

        async def wait(self):
            return 1

        def kill(self):
            return None

    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured['env'] = dict(kwargs.get('env') or {})
        return _StubProcess()

    monkeypatch.setattr(filesystem_mutation_module.os, 'name', 'nt', raising=False)
    monkeypatch.setattr(filesystem_mutation_module.asyncio, 'create_subprocess_exec', _fake_create_subprocess_exec)
    # 这条用例喂的是 GBK 字节，读法必须钉在同一个码页上：否则它测的其实是"跑它的这台
    # 机器是什么 locale"（zh-CN 本机过、en-US runner 上按 cp1252 解出一串假字符还"成功"）。
    monkeypatch.setattr(subprocess_text_module, '_oem_encoding', lambda: 'cp936')

    tool = FilesystemTool(workspace=tmp_path)
    result = await tool._run_validation_command(
        command='python -c "pass"',
        cwd=str(tmp_path),
        timeout_seconds=5,
    )

    assert result['ok'] is False
    assert '站点经验' in result['stderr']
    assert captured['env']['PYTHONIOENCODING'] == 'utf-8'
