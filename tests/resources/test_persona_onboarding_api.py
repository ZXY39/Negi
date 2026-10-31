from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import main  # noqa: F401  # the app imports main before the runtime API package
from g3ku.runtime.api import persona_onboarding as api
from g3ku.runtime.persona_onboarding import (
    PERSONA_DESCRIPTION_MAX_CHARS,
    OnboardingStore,
    build_photo_session_prompt,
    list_persona_photos,
    persona_photos_dir,
    store_persona_photo,
)
from g3ku.runtime.session_trust import is_owner_session


def _store(tmp_path: Path) -> OnboardingStore:
    return OnboardingStore(tmp_path)


def test_onboarding_store_first_run_halfway_and_old_install(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.should_run(has_models=False) is True
    assert store.exists()

    store.update(description="你是玲音。", persona_step_done=True)
    assert store.should_run(has_models=True) is True
    assert store.load()["description"] == "你是玲音。"

    store.update(completed=True)
    assert store.should_run(has_models=True) is False

    fresh_workspace = tmp_path / "old-install"
    fresh_workspace.mkdir()
    old_store = OnboardingStore(fresh_workspace)
    assert old_store.should_run(has_models=True) is False
    assert old_store.load()["completed"] is True

    reset = store.reset()
    assert reset["completed"] is False
    assert reset["description"] == ""
    assert store.should_run(has_models=True) is True


def _png_bytes() -> bytes:
    # A valid 1x1 PNG; the upload helper only checks the container bytes, not pixels.
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def test_persona_photos_are_stored_in_my_image_with_unique_names(tmp_path: Path) -> None:
    first = store_persona_photo(tmp_path, _png_bytes(), filename="me.png", mime_type="image/png")
    second = store_persona_photo(tmp_path, _png_bytes(), filename="me.png", mime_type="image/png")
    assert first["name"] == "me.png"
    assert second["name"] == "me_2.png"
    assert Path(first["path"]).parent == persona_photos_dir(tmp_path)
    assert Path(first["path"]).exists()
    assert list_persona_photos(tmp_path)[0]["name"] == "me_2.png"

    (persona_photos_dir(tmp_path) / "notes.txt").write_text("not a photo", encoding="utf-8")
    assert [item["name"] for item in list_persona_photos(tmp_path)] == ["me_2.png", "me.png"]

    with pytest.raises(ValueError):
        store_persona_photo(tmp_path, b"not an image", filename="me.txt", mime_type="text/plain")
    with pytest.raises(ValueError):
        store_persona_photo(tmp_path, b"x" * 16, filename="big.png", max_bytes=8)


def test_photo_session_uses_the_internal_namespace() -> None:
    assert api.PERSONA_PHOTO_SESSION_KEY == "internal:persona_photo"
    assert api.PERSONA_PHOTO_SESSION_KEY.startswith("internal:")


def test_photo_session_prompt_names_every_photo_and_the_folder(tmp_path: Path) -> None:
    prompt = build_photo_session_prompt(tmp_path, ["a.png", "b.jpg"])
    assert "a.png" in prompt
    assert "b.jpg" in prompt
    assert str(persona_photos_dir(tmp_path)) in prompt
    assert "memory_update" in prompt
    assert "silent" in prompt
    assert "不要再复制" in prompt


def test_complete_onboarding_requires_ceo_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    store.update(description="你是玲音。", persona_step_done=True)
    monkeypatch.setattr(api, "_store", lambda: store)
    monkeypatch.setattr(api, "_role_chain", lambda scope: [])

    with pytest.raises(api.HTTPException) as excinfo:
        import asyncio

        asyncio.run(api.complete_onboarding())
    assert excinfo.value.status_code == 409
    detail = excinfo.value.detail
    assert detail["code"] == "onboarding_chains_missing"
    assert detail["missing"] == ["ceo"]
    assert store.load()["completed"] is False


def test_complete_onboarding_allows_empty_memory_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记忆 Agent 的模型链是可选的：人设 note 照写，但不排注定 blocked 的整理请求。"""
    store = _store(tmp_path)
    photo = store_persona_photo(tmp_path, _png_bytes(), filename="me.png", mime_type="image/png")
    store.update(description="你是玲音。", persona_step_done=True, photos=[photo["name"]])

    calls: dict[str, object] = {}

    class _MemoryManager:
        def set_persona(self, text: str) -> dict:
            calls["persona"] = text
            return {"ref": "note_persona"}

        async def enqueue_operator_request(self, **kwargs):
            raise AssertionError(f"must not enqueue without a memory model: {kwargs}")

    monkeypatch.setattr(api, "_store", lambda: store)
    monkeypatch.setattr(api, "_role_chain", lambda scope: ["ceo-model"] if scope == "ceo" else [])
    monkeypatch.setattr(api, "_memory_manager", lambda: _MemoryManager())
    monkeypatch.setattr(
        api,
        "_start_photo_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not start a memory_update photo turn without a memory model")
        ),
    )

    import asyncio

    result = asyncio.run(api.complete_onboarding())
    assert result["ok"] is True
    assert result["jobs"] == ["persona_saved", "photo_session_skipped_memory_missing"]
    assert calls["persona"] == "你是玲音。"
    state = store.load()
    assert state["completed"] is True
    assert state["description"] == ""
    assert state["persona_request_id"] == ""
    assert state["photo_session_started_at"] == ""


def test_complete_onboarding_writes_enqueues_and_starts_the_photo_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    photo = store_persona_photo(tmp_path, _png_bytes(), filename="me.png", mime_type="image/png")
    store.update(description="你是玲音。", persona_step_done=True, photos=[photo["name"]])

    calls: dict[str, object] = {}

    class _MemoryManager:
        def set_persona(self, text: str) -> dict:
            calls["persona"] = text
            return {"ref": "note_persona"}

        async def enqueue_operator_request(self, **kwargs):
            calls["enqueue"] = kwargs
            return {"ok": True, "request_id": "req-1"}

    manager = _MemoryManager()
    monkeypatch.setattr(api, "_store", lambda: store)
    monkeypatch.setattr(api, "_role_chain", lambda scope: [f"{scope}-model"])
    monkeypatch.setattr(api, "_memory_manager", lambda: manager)

    def _start(store_arg, names):
        calls["photos"] = list(names)
        return True

    monkeypatch.setattr(api, "_start_photo_session", _start)

    import asyncio

    result = asyncio.run(api.complete_onboarding())
    assert result["ok"] is True
    assert calls["persona"] == "你是玲音。"
    enqueue = calls["enqueue"]
    assert enqueue["trigger_source"] == "persona_onboarding"
    assert enqueue["op"] == "write"
    assert enqueue["decision_source"] == "user"
    assert enqueue["session_key"] == api.PERSONA_MEMORY_SESSION_KEY
    assert is_owner_session(str(enqueue["session_key"]))
    assert "你是玲音。" in enqueue["payload_text"]
    assert calls["photos"] == ["me.png"]
    state = store.load()
    assert state["completed"] is True
    assert state["persona_request_id"] == "req-1"
    assert state["description"] == ""
    assert state["photo_session_started_at"]

    second = asyncio.run(api.complete_onboarding())
    assert second["already_completed"] is True
    assert calls["persona"] == "你是玲音。"


def test_onboarding_status_reports_pending_input_and_album(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    store_persona_photo(tmp_path, _png_bytes(), filename="me.png", mime_type="image/png")
    store.update(description="你是玲音。", persona_step_done=True, photos=["me.png"])
    monkeypatch.setattr(api, "_store", lambda: store)
    monkeypatch.setattr(api, "_has_chat_models", lambda: False)
    monkeypatch.setattr(api, "_memory_manager", lambda: SimpleNamespace(persona_text=lambda: ""))
    monkeypatch.setattr(api, "workspace_path", lambda: tmp_path)

    import asyncio

    status = asyncio.run(api.get_onboarding_status())
    assert status["should_run"] is True
    assert status["persona_step_done"] is True
    assert status["has_persona_input"] is True
    assert status["description"] == "你是玲音。"
    assert status["photos"] == ["me.png"]
    assert status["photo_album"][0]["name"] == "me.png"


def test_onboarding_status_reads_persona_file_before_the_agent_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(api, "_store", lambda: store)
    monkeypatch.setattr(api, "_has_chat_models", lambda: False)
    monkeypatch.setattr(api, "workspace_path", lambda: tmp_path)

    def _no_agent():
        raise RuntimeError("no CEO model configured")

    monkeypatch.setattr(api, "_memory_manager", _no_agent)
    note_path = tmp_path / "memory" / "notes" / "note_persona.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("你是玲音。\n", encoding="utf-8")

    import asyncio

    status = asyncio.run(api.get_onboarding_status())
    assert status["has_persona"] is True
    persona = asyncio.run(api.get_persona())
    assert persona["description"] == "你是玲音。"


def test_save_persona_from_settings_writes_and_enqueues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class _MemoryManager:
        def set_persona(self, text: str) -> dict:
            calls["persona"] = text
            return {"ref": "note_persona"}

        async def enqueue_operator_request(self, **kwargs):
            calls["enqueue"] = kwargs
            return {"ok": True, "request_id": "req-settings"}

    monkeypatch.setattr(api, "_memory_manager", lambda: _MemoryManager())
    monkeypatch.setattr(api, "_role_chain", lambda scope: ["memory-model"] if scope == "memory" else [])
    import asyncio

    result = asyncio.run(api.save_persona({"description": "你是玲音。"}))
    assert result["ok"] is True
    assert result["enqueued"] is True
    assert calls["persona"] == "你是玲音。"
    enqueue = calls["enqueue"]
    assert enqueue["trigger_source"] == "persona_settings"
    assert enqueue["op"] == "write"
    assert enqueue["session_key"] == api.PERSONA_MEMORY_SESSION_KEY
    assert is_owner_session(str(enqueue["session_key"]))
    assert "你是玲音。" in enqueue["payload_text"]


def test_save_persona_skips_enqueue_without_a_memory_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class _MemoryManager:
        def set_persona(self, text: str) -> dict:
            calls["persona"] = text
            return {"ref": "note_persona"}

        async def enqueue_operator_request(self, **kwargs):
            calls["enqueue"] = kwargs
            return {"ok": True, "request_id": "unexpected"}

    monkeypatch.setattr(api, "_memory_manager", lambda: _MemoryManager())
    monkeypatch.setattr(api, "_role_chain", lambda scope: [])
    import asyncio

    result = asyncio.run(api.save_persona({"description": "你是玲音。"}))
    assert result["enqueued"] is False
    assert calls["persona"] == "你是玲音。"
    assert "enqueue" not in calls


def test_save_persona_without_runtime_writes_the_reserved_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "workspace_path", lambda: tmp_path)

    def _no_runtime():
        raise api.HTTPException(status_code=503, detail={"code": "memory_manager_unavailable"})

    monkeypatch.setattr(api, "_memory_manager", _no_runtime)
    import asyncio

    result = asyncio.run(api.save_persona({"description": "你是玲音。"}))
    assert result["ok"] is True
    assert result["enqueued"] is False
    note = tmp_path / "memory" / "notes" / "note_persona.md"
    assert note.read_text(encoding="utf-8").strip() == "你是玲音。"


def test_save_persona_falls_back_when_no_ceo_model_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "workspace_path", lambda: tmp_path)

    def _no_model():
        raise ValueError("No model configured for role 'ceo'.")

    monkeypatch.setattr(api, "_memory_manager", _no_model)
    import asyncio

    result = asyncio.run(api.save_persona({"description": "你是玲音。"}))
    assert result["ok"] is True
    assert result["enqueued"] is False
    note = tmp_path / "memory" / "notes" / "note_persona.md"
    assert note.read_text(encoding="utf-8").strip() == "你是玲音。"


def test_save_persona_rejects_empty_and_over_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    with pytest.raises(api.HTTPException) as empty:
        asyncio.run(api.save_persona({"description": "   "}))
    assert empty.value.status_code == 422
    assert empty.value.detail["code"] == "persona_empty"

    with pytest.raises(api.HTTPException) as too_long:
        asyncio.run(api.save_persona({"description": "x" * (PERSONA_DESCRIPTION_MAX_CHARS + 1)}))
    assert too_long.value.status_code == 422
    assert too_long.value.detail["code"] == "persona_too_long"


def test_onboarding_persona_writes_the_note_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: dict[str, object] = {}

    class _MemoryManager:
        def set_persona(self, text: str) -> dict:
            calls["persona"] = text
            return {"ref": "note_persona"}

    monkeypatch.setattr(api, "_store", lambda: store)
    monkeypatch.setattr(api, "_memory_manager", lambda: _MemoryManager())
    import asyncio

    result = asyncio.run(api.save_onboarding_persona({"description": "你是玲音。"}))
    assert result["written"] is True
    assert calls["persona"] == "你是玲音。"
    state = store.load()
    assert state["description"] == "你是玲音。"
    assert state["persona_step_done"] is True


def test_onboarding_persona_falls_back_to_the_note_file_without_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(api, "_store", lambda: store)
    monkeypatch.setattr(api, "workspace_path", lambda: tmp_path)

    def _no_model():
        raise ValueError("No model configured for role 'ceo'.")

    monkeypatch.setattr(api, "_memory_manager", _no_model)
    import asyncio

    result = asyncio.run(api.save_onboarding_persona({"description": "你是玲音。"}))
    assert result["written"] is True
    note = tmp_path / "memory" / "notes" / "note_persona.md"
    assert note.read_text(encoding="utf-8").strip() == "你是玲音。"


def test_onboarding_persona_rejects_over_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(api, "_store", lambda: store)

    import asyncio

    with pytest.raises(api.HTTPException) as excinfo:
        asyncio.run(
            api.save_onboarding_persona({"description": "x" * (PERSONA_DESCRIPTION_MAX_CHARS + 1)})
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "onboarding_persona_too_long"
