"""resource.yaml 注册 schema 与 handler 实现签名的契约测试。

背景事故（task:745f8363566b）：`web_fetch` 的实现在统一 timeout 合同改造中把
`timeout_ms` 退役为 `timeout`（秒），但 resource.yaml 的 `parameters` 块仍声明
`timeout_ms`（含 default 10000）。EmbeddedMCPTool 按注册 schema 构建 FastMCP
签名，FastMCP 对每次调用做默认值填充，于是**所有** web_fetch 调用（即使模型
参数完全正确、甚至不带任何 timeout 参数）都被强注 `timeout_ms=10000` 传给实现，
无差别报 `unexpected keyword argument 'timeout_ms'`，工具整体瘫痪。

本文件防止同类 schema/实现漂移：
1. 清单卫生：resource.yaml 的 parameters.properties 键必须是合法 Python 标识符
   或关键字（关键字名走 ManifestBackedTool 兜底属既有设计；拦截的是被 JSON
   转义字符串写坏的 YAML 产生的病态键名）。
2. 全量交叉校验：对测试环境可构建的每个工具，注册 schema properties 必须是
   handler 实际 dispatch 签名可接受的参数子集（handler 带 **kwargs 时豁免）；
   自持超时工具必须接受统一 `timeout` 参数。web_fetch 必须被覆盖。
3. 事故回归：schema 声明实现不接受的参数时，EmbeddedMCPTool 执行期必须过滤掉
   该参数（含 FastMCP 默认值填充产生的强注），调用仍然成功，并记录漂移告警。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import keyword
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from g3ku.agent.tools.base import Tool
from g3ku.resources.embedded_mcp import (
    EmbeddedMCPTool,
    _handler_parameter_info,
)
from g3ku.resources.models import ResourceKind, ToolResourceDescriptor

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / 'tools'


class _DummyService:
    def __init__(self):
        self.content_store = None
        self.artifact_store = None
        self.store = None

    async def startup(self) -> None:
        return None


class _DummyCronService:
    def add_job(self, **kwargs):
        return SimpleNamespace(name='job', id='job:1')

    def list_jobs(self):
        return []

    def remove_job(self, job_id: str):
        return False


class _DummyMemoryManager:
    async def search_tool_view(self, **kwargs):
        return {}

    async def write_explicit_memory_items(self, **kwargs):
        return {"ok": True, "written": [], "deleted": [], "searchable": True}


class _DummyBus:
    async def publish_outbound(self, message) -> None:
        return None


class _DummyLoop:
    _store_enabled = True
    bus = _DummyBus()


def _runtime_stub(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        workspace=workspace,
        loop=_DummyLoop(),
        app_config=None,
        services=SimpleNamespace(
            main_task_service=_DummyService(),
            cron_service=_DummyCronService(),
            memory_manager=_DummyMemoryManager(),
        ),
    )


def _load_handler(tool_dir: Path, workspace: Path):
    module_path = tool_dir / 'main' / 'tool.py'
    if not module_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f'contract_{tool_dir.name}', module_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, 'build'):
        return None
    return module.build(_runtime_stub(workspace))


def _manifest_parameters(tool_dir: Path) -> dict:
    manifest = yaml.safe_load((tool_dir / 'resource.yaml').read_text(encoding='utf-8')) or {}
    parameters = manifest.get('parameters')
    return parameters if isinstance(parameters, dict) else {}


def _dispatch_target(handler):
    if isinstance(handler, Tool):
        return handler.execute
    if hasattr(handler, 'execute'):
        return handler.execute
    return handler


def test_all_resource_yaml_parameter_names_are_valid_identifiers():
    # Python 关键字（如 task_stats_cn 的 `from`）是合法设计：这类名字会触发
    # ManifestBackedTool 兜底路径，handler 经 **kwargs 接收。要拦截的是"既非
    # 标识符也非关键字"的病态名字——典型是把 JSON 转义字符串直接写进 YAML
    # 产生的 `max_chars:\n      type: integer\n ...` 这类损坏键。
    offenders: list[str] = []
    for manifest_path in sorted(TOOLS_ROOT.glob('*/resource.yaml')):
        properties = _manifest_parameters(manifest_path.parent).get('properties') or {}
        for name in properties:
            text = str(name or '').strip()
            if not text or keyword.iskeyword(text):
                continue
            if not text.isidentifier():
                offenders.append(f'{manifest_path.parent.name}: {text[:60]!r}')
    assert offenders == [], (
        'resource.yaml parameters.properties 出现非法参数名'
        '（疑似 YAML 被转义字符串写坏；这类键会破坏 schema 语义）：\n'
        + '\n'.join(offenders)
    )


def test_resource_yaml_properties_are_accepted_by_handler_signature(tmp_path: Path):
    build_failures: dict[str, str] = {}
    checked: list[str] = []
    violations: list[str] = []
    for tool_dir in sorted(p for p in TOOLS_ROOT.iterdir() if p.is_dir()):
        manifest_path = tool_dir / 'resource.yaml'
        if not manifest_path.exists():
            continue
        try:
            handler = _load_handler(tool_dir, tmp_path)
        except Exception as exc:  # 测试环境缺依赖/服务导致的构建失败不属于契约漂移
            build_failures[tool_dir.name] = f'{type(exc).__name__}: {exc}'
            continue
        if handler is None:
            build_failures[tool_dir.name] = 'no build() entrypoint'
            continue
        info = _handler_parameter_info(handler)
        properties = set(_manifest_parameters(tool_dir).get('properties') or {})
        checked.append(tool_dir.name)
        if info is None:
            continue
        names, has_var_keyword = info
        if has_var_keyword:
            continue
        drifted = sorted(properties - names)
        if drifted:
            violations.append(f'{tool_dir.name}: schema declares {drifted}, handler accepts {sorted(names)}')
        # 自持超时工具会被执行层显式传入统一 timeout 参数，实现必须接受它。
        if bool(getattr(handler, 'self_enforced_timeout', False)) and 'timeout' not in names:
            violations.append(f'{tool_dir.name}: self_enforced_timeout handler must accept `timeout`')

    assert 'web_fetch' in checked, 'web_fetch 必须被本契约测试覆盖（事故工具）'
    assert violations == [], 'resource.yaml properties 必须是实现签名可接受参数的子集：\n' + '\n'.join(violations)


class _DriftHandler:
    """只接受 url/timeout 的实现；注册 schema 却仍声明 timeout_ms（复刻事故形态）。"""

    self_enforced_timeout = True

    def __init__(self) -> None:
        self.received: list[dict] = []

    async def __call__(self, url: str, timeout: float | None = None) -> dict:
        self.received.append({'url': url, 'timeout': timeout})
        return {'ok': True, 'url': url}


def _drift_descriptor() -> ToolResourceDescriptor:
    return ToolResourceDescriptor(
        kind=ResourceKind.TOOL,
        name='drift_fetch',
        description='synthetic drifted tool',
        root=REPO_ROOT,
        manifest_path=REPO_ROOT / 'resource.yaml',
        fingerprint='test',
        parameters={
            'type': 'object',
            'additionalProperties': False,
            'required': ['url'],
            'properties': {
                'url': {'type': 'string'},
                'timeout_ms': {
                    'type': 'integer',
                    'minimum': 1000,
                    'maximum': 30000,
                    'default': 10000,
                    'description': 'legacy milliseconds timeout',
                },
            },
        },
    )


@pytest.mark.asyncio
async def test_embedded_mcp_filters_schema_default_injected_drift_argument(caplog: pytest.LogCaptureFixture):
    handler = _DriftHandler()
    tool = EmbeddedMCPTool(_drift_descriptor(), handler)

    # 构建期就应记录 schema/实现漂移告警
    assert any('timeout_ms' in record.getMessage() for record in caplog.records)

    # 模型参数完全正确（不带 timeout_ms）：FastMCP 会按 schema 默认值强注
    # timeout_ms=10000，执行层必须把它过滤掉，调用仍然成功。
    result = await tool.execute(url='https://example.com', timeout=30)
    assert json.loads(result) == {'ok': True, 'url': 'https://example.com'}
    assert handler.received[-1] == {'url': 'https://example.com', 'timeout': 30}

    # 模型显式误传 timeout_ms（被文档/schema 诱导）：同样过滤，不再整体失败。
    result = await tool.execute(url='https://example.org', timeout_ms=15000)
    assert json.loads(result) == {'ok': True, 'url': 'https://example.org'}
    assert handler.received[-1] == {'url': 'https://example.org', 'timeout': None}
    # 同一漂移键的执行期告警做过去重（防日志刷屏）：两次调用只记录一次。
    drop_warnings = [r for r in caplog.records if 'dropping arguments' in r.getMessage()]
    assert len(drop_warnings) == 1


def test_embedded_mcp_signature_still_validates_declared_parameters():
    handler = _DriftHandler()
    tool = EmbeddedMCPTool(_drift_descriptor(), handler)
    signature = inspect.signature(_dispatch_target(handler))
    assert 'timeout_ms' not in signature.parameters
    # 注册 schema（含注入的统一 timeout）仍是模型可见的调用面
    assert set(tool.parameters.get('properties') or {}) >= {'url', 'timeout_ms', 'timeout'}
