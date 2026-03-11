from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

from g3ku.capabilities.models import CapabilityIndexCandidate, CapabilityIndexSource
from g3ku.capabilities.utils import check_compat


class CapabilityIndexRegistry:
    """Load and search structured capability index files."""

    def __init__(self, workspace: Path, *, index_paths: list[Path] | None = None):
        self.workspace = Path(workspace)
        self.index_paths = [Path(path) for path in (index_paths or [])]
        self._candidates: list[CapabilityIndexCandidate] = []
        self.refresh()

    def refresh(self) -> None:
        items: list[CapabilityIndexCandidate] = []
        for index_path in self.index_paths:
            path = Path(index_path).expanduser()
            if not path.is_absolute():
                path = (self.workspace / path).resolve()
            if not path.exists():
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            index_name = str(data.get("name") or path.stem)
            capabilities = data.get("capabilities") or {}
            if not isinstance(capabilities, dict):
                continue
            for capability_name, payload in capabilities.items():
                if not isinstance(payload, dict):
                    continue
                display_name = str(payload.get("display_name") or capability_name)
                versions = payload.get("versions") or []
                if not isinstance(versions, list):
                    continue
                for version_item in versions:
                    if not isinstance(version_item, dict):
                        continue
                    source_data = version_item.get("source") or {}
                    if not isinstance(source_data, dict):
                        continue
                    raw_uri = str(source_data.get("uri") or "").strip()
                    if not raw_uri:
                        continue
                    source_type = str(source_data.get("type") or "local").strip().lower()
                    resolved_uri = raw_uri
                    if source_type == "local":
                        candidate_path = Path(raw_uri)
                        if not candidate_path.is_absolute():
                            resolved_uri = str((path.parent / candidate_path).resolve())
                    items.append(
                        CapabilityIndexCandidate(
                            name=str(capability_name),
                            version=str(version_item.get("version") or "0.0.0"),
                            display_name=display_name,
                            source=CapabilityIndexSource(
                                type=source_type,
                                uri=resolved_uri,
                                ref=(source_data.get("ref") if source_data.get("ref") is None else str(source_data.get("ref"))),
                            ),
                            compat={
                                str(k): str(v)
                                for k, v in dict(version_item.get("compat") or {}).items()
                            },
                            metadata=dict(version_item),
                            index_name=index_name,
                            index_path=str(path),
                        )
                    )
        self._candidates = sorted(items, key=self._sort_key, reverse=True)

    def list_candidates(self, query: str | None = None) -> list[CapabilityIndexCandidate]:
        query_text = str(query or "").strip().lower()
        if not query_text:
            return list(self._candidates)
        return [
            item
            for item in self._candidates
            if query_text in item.name.lower() or query_text in item.display_name.lower()
        ]

    def list_latest(self, query: str | None = None) -> list[CapabilityIndexCandidate]:
        latest: dict[tuple[str, str | None], CapabilityIndexCandidate] = {}
        for item in self.list_candidates(query):
            key = (item.name, item.index_path)
            if key not in latest:
                latest[key] = item
        return list(latest.values())

    def select_version(
        self,
        name: str,
        *,
        version: str | None = None,
        index_name: str | None = None,
        index_path: str | None = None,
    ) -> CapabilityIndexCandidate | None:
        candidates = [item for item in self._candidates if item.name == name]
        if index_name:
            candidates = [item for item in candidates if item.index_name == index_name]
        if index_path:
            candidates = [item for item in candidates if item.index_path == index_path]
        compatible = [item for item in candidates if not check_compat(item.compat)[0]]
        if version:
            compatible = [item for item in compatible if item.version == version]
        return compatible[0] if compatible else None

    @staticmethod
    def _sort_key(item: CapabilityIndexCandidate) -> tuple[int, Any, str]:
        try:
            parsed = Version(item.version)
            return (1, parsed, item.name)
        except InvalidVersion:
            return (0, item.version, item.name)
