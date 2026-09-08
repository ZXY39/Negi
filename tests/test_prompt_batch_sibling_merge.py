"""prompt_batch 批次兄弟输入内容合并测试（P2 内容丢失修复）。

prompt_batch 只以批次最后一条输入驱动回合：frontdoor 请求构建期只展开这一条
输入的内容，用户连续发送的较早消息（文本与图片）会彻底缺席模型请求。
`CeoFrontDoorRuntimeOps._merge_prompt_batch_sibling_contents` 在请求构建期把
同批次其它输入的内容块按顺序并入当前回合内容；转录不受影响（合并只发生在
请求构建期，输入本体不被改写）。
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps


def _ops(*, multimodal: bool = False) -> CeoFrontDoorRuntimeOps:
    """构造只带合并路径所需依赖的实例（绕过完整 __init__）。"""
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(app_config=None)
    ops._ceo_image_multimodal_enabled_for_model_refs = lambda refs: multimodal
    return ops


def test_request_content_block_list_normalizes_shapes() -> None:
    normalize = CeoFrontDoorRuntimeOps._request_content_block_list
    assert normalize("你好") == [{"type": "text", "text": "你好"}]
    assert normalize("  ") == []
    assert normalize(None) == []
    blocks = [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {"url": "u"}}]
    assert normalize(blocks) == blocks
    assert normalize(["片段", {"type": "text", "text": "块"}]) == [
        {"type": "text", "text": "片段"},
        {"type": "text", "text": "块"},
    ]


def test_merge_combines_sibling_batch_contents_before_current() -> None:
    """批次内较早输入的文本与图片必须按顺序并入，当前输入内容排最后。"""
    ops = _ops()
    sibling_a = UserInputMessage(content="第一条消息", metadata={"_transcript_turn_id": "turn-a"})
    sibling_blocks = [
        {"type": "text", "text": "第二条看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    sibling_b = UserInputMessage(content=list(sibling_blocks), metadata={"_transcript_turn_id": "turn-b"})
    current = UserInputMessage(content="最后一条", metadata={"_transcript_turn_id": "turn-current"})
    session = SimpleNamespace(_active_user_batch_inputs=[sibling_a, sibling_b, current])

    merged = ops._merge_prompt_batch_sibling_contents(
        session=session,
        current_turn_id="turn-current",
        current_content="最后一条",
        model_refs=None,
    )

    assert merged == [
        {"type": "text", "text": "第一条消息"},
        {"type": "text", "text": "第二条看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "最后一条"},
    ]
    # 输入本体不被改写：完成回写时各转录行仍按各自原文落盘。
    assert sibling_a.content == "第一条消息"
    assert sibling_b.content == sibling_blocks
    assert current.content == "最后一条"


def test_merge_passthrough_for_single_input_batch() -> None:
    ops = _ops()
    only = UserInputMessage(content="独条", metadata={"_transcript_turn_id": "turn-a"})
    session = SimpleNamespace(_active_user_batch_inputs=[only])

    merged = ops._merge_prompt_batch_sibling_contents(
        session=session,
        current_turn_id="turn-a",
        current_content="独条",
        model_refs=None,
    )

    assert merged == "独条"


def test_merge_passthrough_when_turn_id_missing() -> None:
    """内部回合（心跳/定时）不配置批次上下文；缺 turn id 时不得合并，
    即使批次里有多条输入。"""
    ops = _ops()
    sibling = UserInputMessage(content="别的", metadata={"_transcript_turn_id": "turn-a"})
    other = UserInputMessage(content="另一条", metadata={"_transcript_turn_id": "turn-b"})
    session = SimpleNamespace(_active_user_batch_inputs=[sibling, other])

    merged = ops._merge_prompt_batch_sibling_contents(
        session=session,
        current_turn_id="",
        current_content="当前",
        model_refs=None,
    )

    assert merged == "当前"


def test_merge_dedupes_identical_blocks() -> None:
    ops = _ops()
    sibling = UserInputMessage(content="重复内容", metadata={"_transcript_turn_id": "turn-a"})
    current = UserInputMessage(content="重复内容", metadata={"_transcript_turn_id": "turn-b"})
    session = SimpleNamespace(_active_user_batch_inputs=[sibling, current])

    merged = ops._merge_prompt_batch_sibling_contents(
        session=session,
        current_turn_id="turn-b",
        current_content="重复内容",
        model_refs=None,
    )

    assert merged == [{"type": "text", "text": "重复内容"}]


def test_merge_expands_sibling_web_uploads(tmp_path: Path) -> None:
    """兄弟输入按各自元数据展开：web 上传的图片要以 image_url 块进入合并结果。"""
    ops = _ops(multimodal=True)
    image_path = tmp_path / "up.png"
    image_path.write_bytes(b"png-bytes")
    sibling = UserInputMessage(
        content="看图",
        metadata={
            "_transcript_turn_id": "turn-a",
            "web_ceo_raw_text": "看图",
            "web_ceo_uploads": [
                {"kind": "image", "name": "up.png", "path": str(image_path), "mime_type": "image/png"}
            ],
        },
    )
    current = UserInputMessage(content="结论", metadata={"_transcript_turn_id": "turn-b"})
    session = SimpleNamespace(_active_user_batch_inputs=[sibling, current])

    merged = ops._merge_prompt_batch_sibling_contents(
        session=session,
        current_turn_id="turn-b",
        current_content="结论",
        model_refs=["managed:first"],
    )

    image_blocks = [block for block in merged if block.get("type") == "image_url"]
    assert len(image_blocks) == 1
    expected_url = "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii")
    assert image_blocks[0]["image_url"]["url"] == expected_url
    text_blocks = [block for block in merged if block.get("type") == "text"]
    assert any("看图" in str(block.get("text") or "") for block in text_blocks)
    assert text_blocks[-1] == {"type": "text", "text": "结论"}


def test_merge_keeps_external_image_blocks_from_siblings() -> None:
    """外部桥接输入的 image_url 块（无 web_ceo_uploads 元数据）原样并入。"""
    ops = _ops(multimodal=True)
    sibling = UserInputMessage(
        content=[
            {"type": "text", "text": "Channel attachments:\n- img.png"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,EXT"}},
        ],
        metadata={"_transcript_turn_id": "turn-a", "external_attachments": [{"kind": "image"}]},
    )
    current = UserInputMessage(content="第二张呢", metadata={"_transcript_turn_id": "turn-b"})
    session = SimpleNamespace(_active_user_batch_inputs=[sibling, current])

    merged = ops._merge_prompt_batch_sibling_contents(
        session=session,
        current_turn_id="turn-b",
        current_content="第二张呢",
        model_refs=None,
    )

    assert merged == [
        {"type": "text", "text": "Channel attachments:\n- img.png"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,EXT"}},
        {"type": "text", "text": "第二张呢"},
    ]
