"""图片能力按"这一发落到的那颗模型"判，不支持时降级成"路径 + 原因"而不是报错。

实盘依据（2026-10-13，QQ 渠道会话 `ext:qq-official-1903529517`）：把已勾多模态的
`workbuddy-deepseek-v4.1-flash` 放到 CEO 链首后，链尾三颗未勾使整链 AND 判"不收图"，
图片被静默剥掉——链首那颗其实能看。7 天里 32 个带图跳有 21 个走的是这条静默道。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import g3ku.runtime.image_capability as image_capability_module
import main.runtime.chat_backend as chat_backend_module
import main.runtime.react_loop as react_loop_module
from g3ku.config.model_manager import image_multimodal_target_decision
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.image_capability import (
    build_image_degradation_text,
    degrade_image_blocks_in_messages,
)


class _Config:
    def __init__(self, models: dict[str, tuple[str, bool]]) -> None:
        self._models = {
            key: SimpleNamespace(name=name, image_multimodal_enabled=enabled)
            for key, (name, enabled) in models.items()
        }

    def get_managed_model(self, key: str):
        return self._models.get(str(key or "").strip())

    def get_model_runtime_profile(self, ref):  # ConfigChatBackend 构造期要
        return None


def _image_message(path_note: str = "") -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
        {"role": "assistant", "content": "上一轮回答"},
    ]


def test_target_decision_reads_the_one_model_not_the_chain() -> None:
    config = _Config({"m_head": ("链首多模态", True), "m_tail": ("链尾纯文本", False)})

    assert image_multimodal_target_decision(config, "m_head") == (True, "链首多模态")
    assert image_multimodal_target_decision(config, "m_tail") == (False, "链尾纯文本")
    # 解析不到这一颗按不收图：运行时发不出去的东西不该假装能看图。
    assert image_multimodal_target_decision(config, "m_missing") == (False, "m_missing")


def test_degradation_text_names_the_config_and_keeps_the_path() -> None:
    text = build_image_degradation_text(path=r"C:\g3ku\uploads\a.png", display_name="链尾纯文本")

    assert r"C:\g3ku\uploads\a.png" in text
    assert "当前模型配置名称是(链尾纯文本)没有勾选多模态能力因此无法看图" in text


def test_degrade_replaces_image_blocks_and_keeps_the_text_block() -> None:
    messages, degraded = degrade_image_blocks_in_messages(
        _image_message(),
        display_name="链尾纯文本",
        path_lookup=lambda block: r"C:\g3ku\uploads\a.png",
    )

    assert degraded == 1
    content = messages[0]["content"]
    assert not image_capability_module.message_content_has_image_blocks(content)
    joined = "\n".join(str(block.get("text") or "") for block in content)
    assert "看这张图" in joined and "没有勾选多模态能力" in joined
    assert messages[1]["content"] == "上一轮回答"


def test_degrade_without_path_source_marks_inline_instead_of_inventing_a_path() -> None:
    messages, degraded = degrade_image_blocks_in_messages(_image_message(), display_name="X")

    assert degraded == 1
    assert image_capability_module.INLINE_IMAGE_PATH_PLACEHOLDER in str(messages[0]["content"])


def test_degrade_is_a_noop_when_no_image_blocks() -> None:
    messages = [{"role": "user", "content": "只有文字"}]

    output, degraded = degrade_image_blocks_in_messages(messages, display_name="X")

    assert degraded == 0
    assert output == messages


def test_chat_backend_throat_degrades_for_the_target_model_only() -> None:
    backend = chat_backend_module.ConfigChatBackend(config=_Config({"m_a": ("A", False)}))
    preview_payload: dict = {"messages": _image_message()}

    degraded_messages = backend._degrade_image_blocks_for_target(
        target=SimpleNamespace(provider_ref="m_a"),
        request_messages=_image_message(),
        preview_payload=preview_payload,
    )

    assert not image_capability_module.message_content_has_image_blocks(degraded_messages[0]["content"])
    # 落盘 artifact 与实发体同一份，否则取证会读到"发出去了图"的假象。
    assert not image_capability_module.message_content_has_image_blocks(preview_payload["messages"][0]["content"])

    enabled_backend = chat_backend_module.ConfigChatBackend(config=_Config({"m_a": ("A", True)}))
    kept = enabled_backend._degrade_image_blocks_for_target(
        target=SimpleNamespace(provider_ref="m_a"),
        request_messages=_image_message(),
        preview_payload={},
    )
    assert image_capability_module.message_content_has_image_blocks(kept[0]["content"])


def test_node_lane_judges_by_chain_head(monkeypatch) -> None:
    config = _Config({"m_head": ("链首", True), "m_tail": ("链尾", False)})
    monkeypatch.setattr(react_loop_module, "get_runtime_config", lambda force=False: (config, 7, False))

    assert react_loop_module.ReActToolLoop._image_multimodal_for_request(["m_head", "m_tail"]) == (True, "链首")
    assert react_loop_module.ReActToolLoop._image_multimodal_for_request(["m_tail", "m_head"]) == (False, "链尾")


def test_frontdoor_note_appends_one_line_per_image() -> None:
    note = CeoFrontDoorRuntimeOps._web_ceo_image_capability_note(
        text="原问题",
        uploads=[
            {"kind": "image", "path": "a.png"},
            {"kind": "image", "path": "b.png"},
            {"kind": "file", "path": "c.txt"},
        ],
        display_name="链尾纯文本",
    )

    lines = [line for line in note.splitlines() if "图片未进请求" in line]
    assert len(lines) == 2
    assert note.startswith("原问题")
    assert "c.txt" not in note


@pytest.mark.asyncio
async def test_frontdoor_expansion_returns_text_only_when_model_cannot_see_images() -> None:
    runner = object.__new__(CeoFrontDoorRuntimeOps)
    runner._ceo_image_multimodal_for_request = lambda refs: (False, "链尾纯文本")
    uploads = [{"kind": "image", "name": "a.png", "path": "a.png"}]

    content = runner._expand_web_ceo_uploads_for_current_request_content(
        content="这张图里是谁",
        metadata={"web_ceo_uploads": uploads, "web_ceo_raw_text": "这张图里是谁"},
        model_refs=["m_tail"],
    )

    text = content if isinstance(content, str) else "\n".join(
        str(block.get("text") or "") for block in content if isinstance(block, dict)
    )
    assert "没有勾选多模态能力因此无法看图" in text
    assert not isinstance(content, list), "不收图时不该再产出多模态 content 块"
