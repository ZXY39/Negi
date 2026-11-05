"""常驻行只留入口：上限来源、方法论必须挂 note、minimal 不许抄做法、前门取用面——三载体同改。

实盘形状：`main/prompts/memory_agent.md`、`_memory_agent_user_prompt` 的内联提交要求、
`tools/memory_runtime/resource.yaml` 的上限值是三处独立编辑的。只改一处，记忆 agent
就拿到两份互相矛盾的指令；只改数字不改判据，"步骤能在 300 字内说清 ⇒ 不挂 note"这条
许可仍然让 190–298 字的自足行合法，前门照旧可以不读 note 答完一节。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The rule that makes a methodology row carry a note, worded once and pinned in both carriers.
METHODOLOGY_MARKER = "内容会被复述成答案"
# The rule that keeps a compressed row an entry, not a shrink-wrapped answer.
MINIMAL_MARKER = "右侧只写动作名"
# The frontdoor sentence that must no longer gate note reads on the printed handle.
FRONTDOOR_GATE = "打开前先确认本轮上下文里列出了该 note"
# What the memory_note tool description must now carry besides the parameter shape.
NOTE_TRIGGER = "before you state"


def _md_text() -> str:
    return (REPO_ROOT / "main" / "prompts" / "memory_agent.md").read_text(encoding="utf-8")


def _runtime_source() -> str:
    return (REPO_ROOT / "g3ku" / "agent" / "memory_agent_runtime.py").read_text(encoding="utf-8")


def _frontdoor_prompt() -> str:
    return (REPO_ROOT / "g3ku" / "runtime" / "prompts" / "ceo_frontdoor.md").read_text(encoding="utf-8")


def _note_tool_source() -> str:
    return (REPO_ROOT / "g3ku" / "agent" / "tools" / "memory_note.py").read_text(encoding="utf-8")


def _note_tool_manifest() -> str:
    return (REPO_ROOT / "tools" / "memory_note" / "resource.yaml").read_text(encoding="utf-8")


def test_resident_line_cap_is_read_from_config_and_not_written_into_prose() -> None:
    text = _md_text()
    assert "summary_max_chars" in text
    assert "300" not in text, "the cap lives in resource.yaml; prose that repeats it drifts"


def test_configured_cap_is_the_entry_shape_budget_derivation() -> None:
    manifest = (REPO_ROOT / "tools" / "memory_runtime" / "resource.yaml").read_text(encoding="utf-8")
    # The cap is twice the measured entry-line shape (触发条件 + 这套做法解决什么 +
    # note 里有什么), so a row that still holds a restatable answer is over budget
    # by construction. The manifest is what the runtime actually loads.
    assert "summary_max_chars: 200" in manifest


def test_methodology_requires_a_note_in_both_carriers() -> None:
    text = _md_text()
    source = _runtime_source()
    assert METHODOLOGY_MARKER in text
    assert METHODOLOGY_MARKER in source
    for carrier in (text, source):
        assert "说得清就不挂" not in carrier
        assert "300 字内说清" not in carrier


def test_compressed_row_may_not_carry_the_method_in_both_carriers() -> None:
    text = _md_text()
    source = _runtime_source()
    assert MINIMAL_MARKER in text
    assert MINIMAL_MARKER in source
    assert "右侧写做法本身" not in text


def test_frontdoor_no_longer_gates_note_reads_on_the_handle() -> None:
    # 72 of 91 resident rows print no handle, so "confirm it is listed first" reads as
    # "there is nothing to open" precisely on the rows that hold the most detail.
    assert FRONTDOOR_GATE not in _frontdoor_prompt()


def test_note_tool_description_carries_the_trigger_in_both_carriers() -> None:
    tool = _note_tool_source()
    manifest = _note_tool_manifest()
    assert NOTE_TRIGGER in tool
    assert NOTE_TRIGGER in manifest
