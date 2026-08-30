from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.content import artifact_ref_from_id
from g3ku.content.navigation import ContentNavigationService, _artifact_id_from_ref
from main.service.task_terminal_callback import (
    TASK_TERMINAL_OUTPUT_INLINE_CHAR_LIMIT,
    TASK_TERMINAL_OUTPUT_INLINE_LINE_LIMIT,
    build_terminal_output_resolver,
    build_task_terminal_payload,
    enrich_task_terminal_payload,
)


def test_artifact_ref_from_id_keeps_single_prefix() -> None:
    assert artifact_ref_from_id("artifact:87386a520c68") == "artifact:87386a520c68"
    assert artifact_ref_from_id("87386a520c68") == "artifact:87386a520c68"
    assert artifact_ref_from_id("") == ""
    assert artifact_ref_from_id(None) == ""


def test_artifact_id_from_ref_accepts_single_and_legacy_double_prefix() -> None:
    assert _artifact_id_from_ref("artifact:87386a520c68") == "artifact:87386a520c68"
    assert _artifact_id_from_ref("artifact:artifact:87386a520c68") == "artifact:87386a520c68"


class _FakeArtifactLookup:
    def __init__(self) -> None:
        self._artifacts: dict[str, SimpleNamespace] = {}

    def add(self, artifact_id: str, *, path: Path, title: str = "report", kind: str = "node_final_output", mime_type: str = "text/markdown") -> None:
        self._artifacts[artifact_id] = SimpleNamespace(
            artifact_id=artifact_id,
            path=str(path),
            title=title,
            kind=kind,
            mime_type=mime_type,
        )

    def get_artifact(self, artifact_id: str):
        return self._artifacts.get(str(artifact_id or "").strip())


def _make_service(tmp_path: Path, lookup: _FakeArtifactLookup) -> ContentNavigationService:
    return ContentNavigationService(workspace=tmp_path, artifact_lookup=lookup)


def test_content_open_resolves_single_prefix_ref(tmp_path) -> None:
    lookup = _FakeArtifactLookup()
    artifact_path = tmp_path / "report.txt"
    artifact_path.write_text("full report body", encoding="utf-8")
    lookup.add("artifact:87386a520c68", path=artifact_path)
    service = _make_service(tmp_path, lookup)

    payload = service.read(ref="artifact:87386a520c68")
    assert payload["ok"] is True
    assert payload["content"] == "full report body"


def test_content_open_still_resolves_legacy_double_prefix_ref(tmp_path) -> None:
    lookup = _FakeArtifactLookup()
    artifact_path = tmp_path / "report.txt"
    artifact_path.write_text("legacy report body", encoding="utf-8")
    lookup.add("artifact:87386a520c68", path=artifact_path)
    service = _make_service(tmp_path, lookup)

    payload = service.read(ref="artifact:artifact:87386a520c68")
    assert payload["ok"] is True
    assert payload["content"] == "legacy report body"


def test_content_open_missing_artifact_guides_against_guessing(tmp_path) -> None:
    lookup = _FakeArtifactLookup()
    service = _make_service(tmp_path, lookup)

    with pytest.raises(FileNotFoundError) as excinfo:
        service.read(ref="artifact:doesnotexist00")
    message = str(excinfo.value)
    assert "artifact not found" in message
    assert "cannot be guessed" in message


def test_content_open_unsupported_ref_points_to_path_param(tmp_path) -> None:
    lookup = _FakeArtifactLookup()
    service = _make_service(tmp_path, lookup)

    with pytest.raises(ValueError) as excinfo:
        service.read(ref="hotdata/all_parsed.txt")
    message = str(excinfo.value)
    assert "unsupported content ref" in message
    assert "`path`" in message


class _FakeContentStore:
    def __init__(self) -> None:
        self.contents: dict[str, tuple[str, str]] = {}

    def add(self, ref: str, content: str, mime_type: str = "text/markdown") -> None:
        self.contents[ref] = (content, mime_type)

    def read(self, *, ref: str, view: str = "canonical"):
        item = self.contents.get(str(ref or "").strip())
        if item is None:
            raise FileNotFoundError(f"artifact not found: {ref}")
        content, mime_type = item
        return {"ok": True, "content": content, "handle": {"mime_type": mime_type}}


def test_terminal_output_resolver_inlines_small_text() -> None:
    store = _FakeContentStore()
    store.add("artifact:small", "small report", mime_type="text/markdown")
    resolver = build_terminal_output_resolver(store)
    assert resolver is not None
    assert resolver("artifact:small") == "small report"


def test_terminal_output_resolver_rejects_oversized_text() -> None:
    store = _FakeContentStore()
    store.add("artifact:big", "x" * (TASK_TERMINAL_OUTPUT_INLINE_CHAR_LIMIT + 1))
    resolver = build_terminal_output_resolver(store)
    assert resolver is not None
    assert resolver("artifact:big") == ""


def test_terminal_output_resolver_rejects_too_many_lines() -> None:
    store = _FakeContentStore()
    store.add("artifact:many-lines", "\n".join(["line"] * (TASK_TERMINAL_OUTPUT_INLINE_LINE_LIMIT + 1)))
    resolver = build_terminal_output_resolver(store)
    assert resolver is not None
    assert resolver("artifact:many-lines") == ""


def test_terminal_output_resolver_rejects_non_text() -> None:
    store = _FakeContentStore()
    store.add("artifact:image", "binary", mime_type="image/png")
    resolver = build_terminal_output_resolver(store)
    assert resolver is not None
    assert resolver("artifact:image") == ""


def test_terminal_output_resolver_requires_content_store() -> None:
    assert build_terminal_output_resolver(None) is None
    assert build_terminal_output_resolver(SimpleNamespace()) is None


def test_enrich_task_terminal_payload_inlines_externalized_output() -> None:
    task_id = "task:demo-inline"
    task = SimpleNamespace(
        task_id=task_id,
        session_id="web:shared",
        title="demo inline task",
        status="success",
        root_node_id="node:root",
        metadata={},
        final_output="Externalized final-output:node:root (262 lines, 8067 chars). Use content_open with ref=artifact:87386a520c68.",
        final_output_ref="artifact:87386a520c68",
        failure_reason="",
        finished_at="2026-08-30T23:37:33+08:00",
        brief_text="daily report ready",
    )

    store = _FakeContentStore()
    store.add("artifact:87386a520c68", "FULL DAILY REPORT BODY")
    resolver = build_terminal_output_resolver(store)

    payload = enrich_task_terminal_payload(
        build_task_terminal_payload(task),
        task=task,
        output_resolver=resolver,
    )

    assert payload["terminal_output"] == "FULL DAILY REPORT BODY"
    assert payload["terminal_output_ref"] == "artifact:87386a520c68"
    assert payload["root_output"] == "FULL DAILY REPORT BODY"


def test_enrich_task_terminal_payload_keeps_summary_when_resolver_cannot_inline() -> None:
    task_id = "task:demo-keep"
    summary = "Externalized final-output:node:root (999 lines, 99999 chars). Use content_open with ref=artifact:toobig."
    task = SimpleNamespace(
        task_id=task_id,
        session_id="web:shared",
        title="demo keep task",
        status="success",
        root_node_id="node:root",
        metadata={},
        final_output=summary,
        final_output_ref="artifact:toobig",
        failure_reason="",
        finished_at="2026-08-30T23:37:33+08:00",
        brief_text="report ready",
    )

    store = _FakeContentStore()
    store.add("artifact:toobig", "x" * (TASK_TERMINAL_OUTPUT_INLINE_CHAR_LIMIT + 1))
    resolver = build_terminal_output_resolver(store)

    payload = enrich_task_terminal_payload(
        build_task_terminal_payload(task),
        task=task,
        output_resolver=resolver,
    )

    assert payload["terminal_output"] == summary
    assert payload["terminal_output_ref"] == "artifact:toobig"
