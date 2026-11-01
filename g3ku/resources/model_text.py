"""Single resolver for the model-facing description of a loaded tool.

两条装载包装类（`g3ku/resources/loader.py` 的 `ManifestBackedTool`、
`g3ku/resources/embedded_mcp.py` 的 `EmbeddedMCPTool`）过去各自抄了同一条优先链，
而且把装载对象自己那句排在清单之前——"哪一份是权威"因此没有唯一判据。这里收成一处，
顺序是：

1. 清单 `resource.yaml` 显式的 `model_description` metadata
2. 清单 `resource.yaml` 的 `description:`
3. 装载对象自己那句（`Tool.model_description` / `description`）——只在前两者都没写时兜底

判据是"清单是唯一可写处"：预算量的是这个返回值、`tools[]` 发的也是它，作者改
`resource.yaml` 就同时改到这两处。第 3 档留着是给没有清单文本的场景（第三方安装体、
合成常驻工具），不是给包内类覆盖清单用的口子。包内类再自撰一句不会改变模型看到的
东西，只会让装载侧发一条 `description_source_drift` 警告。
"""

from __future__ import annotations

from typing import Any

from g3ku.agent.tools.base import Tool

MODEL_TEXT_SOURCE_METADATA = 'metadata'
MODEL_TEXT_SOURCE_MANIFEST = 'manifest'
MODEL_TEXT_SOURCE_HANDLER = 'handler'
MODEL_TEXT_SOURCE_NONE = ''


def handler_model_text(handler: Any) -> str:
    """The sentence a loaded object authors for itself, ignoring the manifest."""
    if not isinstance(handler, Tool):
        return ''
    return str(getattr(handler, 'model_description', '') or '').strip()


def resolve_model_description(descriptor: Any, handler: Any) -> tuple[str, str]:
    """Return `(text, source)` for the one sentence the model is shown.

    `source` 是判据口径的枚举值，不渲染路径：调用方（manager / 包装类）才知道工作区，
    相对形式由它落。
    """
    metadata = getattr(descriptor, 'metadata', None) or {}
    configured = str(metadata.get('model_description') or '').strip()
    if configured:
        return configured, MODEL_TEXT_SOURCE_METADATA
    manifest = str(getattr(descriptor, 'description', '') or '').strip()
    if manifest:
        return manifest, MODEL_TEXT_SOURCE_MANIFEST
    runtime = handler_model_text(handler)
    if runtime:
        return runtime, MODEL_TEXT_SOURCE_HANDLER
    return '', MODEL_TEXT_SOURCE_NONE


def manifest_overrides_handler(descriptor: Any, handler: Any) -> bool:
    """清单与类里自撰的那句是否不一致——即"改了代码不会生效"的那种漂。

    两份相同（包内类只是把清单搬回来）时返回 False，不产生噪声。
    """
    manifest = str(getattr(descriptor, 'description', '') or '').strip()
    runtime = handler_model_text(handler)
    if not manifest or not runtime:
        return False
    return manifest != runtime


__all__ = [
    'MODEL_TEXT_SOURCE_HANDLER',
    'MODEL_TEXT_SOURCE_MANIFEST',
    'MODEL_TEXT_SOURCE_METADATA',
    'MODEL_TEXT_SOURCE_NONE',
    'handler_model_text',
    'manifest_overrides_handler',
    'resolve_model_description',
]
