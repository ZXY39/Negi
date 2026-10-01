from __future__ import annotations

from pathlib import Path

from g3ku.llm_config import repositories as repositories_module
from g3ku.llm_config.facade import get_llm_config_facade


def test_facade_is_reused_per_workspace(tmp_path: Path) -> None:
    first = get_llm_config_facade(tmp_path)
    assert get_llm_config_facade(tmp_path) is first

    other_root = tmp_path / "other-workspace"
    other_root.mkdir()
    assert get_llm_config_facade(other_root) is not first


def test_repeated_lookup_does_not_construct_the_repository_again(tmp_path: Path, monkeypatch) -> None:
    """路由解析每拍都调；仓储再构造就会再 mkdir 一次 records 目录。"""

    built: list[str] = []
    original = repositories_module.EncryptedConfigRepository.__init__

    def counting(self, storage_root, secret_store):  # type: ignore[no-untyped-def]
        built.append(str(storage_root))
        original(self, storage_root, secret_store)

    monkeypatch.setattr(repositories_module.EncryptedConfigRepository, "__init__", counting)

    get_llm_config_facade(tmp_path)
    get_llm_config_facade(tmp_path)
    get_llm_config_facade(tmp_path)

    assert len(built) == 1
