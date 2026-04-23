from __future__ import annotations

import locale
import os
from typing import Mapping


def enrich_subprocess_env_for_text(env: Mapping[str, str] | None = None) -> dict[str, str]:
    prepared = dict(env or {})
    if os.name == 'nt':
        prepared.setdefault('PYTHONIOENCODING', 'utf-8')
    return prepared


def decode_subprocess_output(data: bytes | None) -> str:
    if not data:
        return ''

    candidate_encodings = ['utf-8', 'utf-8-sig']
    preferred_encoding = str(locale.getpreferredencoding(False) or '').strip()
    if preferred_encoding:
        candidate_encodings.append(preferred_encoding)
    if os.name == 'nt':
        candidate_encodings.extend(['mbcs', 'cp936', 'gbk'])

    seen: set[str] = set()
    unique_encodings: list[str] = []
    for raw in candidate_encodings:
        text = str(raw or '').strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_encodings.append(text)

    for encoding in unique_encodings:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    for encoding in unique_encodings:
        if encoding.lower() not in {'utf-8', 'utf-8-sig'}:
            try:
                return data.decode(encoding, errors='replace')
            except LookupError:
                continue
    return data.decode('utf-8', errors='replace')


__all__ = ['decode_subprocess_output', 'enrich_subprocess_env_for_text']
