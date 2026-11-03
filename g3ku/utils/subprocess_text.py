from __future__ import annotations

import locale
import os
from typing import Mapping


def enrich_subprocess_env_for_text(env: Mapping[str, str] | None = None) -> dict[str, str]:
    prepared = dict(env or {})
    if os.name == 'nt':
        prepared.setdefault('PYTHONIOENCODING', 'utf-8')
    return prepared


def _oem_encoding() -> str:
    """控制台输出码页（OEM），不是宿主的 ANSI 首选编码。

    `locale.getpreferredencoding()` 在 en-US 镜像上返回 cp1252，而 GBK 那类双字节输出按
    cp1252 解**一定成功**，只是解成一批假字符——比抛异常更难发现，实盘就是 CI 上读到
    `\\xd5\\xbe...` 而本机 zh-CN 读到正确的"站点经验"。子进程写的是控制台码页，所以先问它。
    """
    if os.name != 'nt':
        return ''
    try:
        import ctypes

        code_page = int(ctypes.windll.kernel32.GetOEMCP())  # type: ignore[attr-defined]
    except Exception:
        return ''
    return f'cp{code_page}' if code_page > 0 else ''


def decode_subprocess_output(data: bytes | None) -> str:
    if not data:
        return ''

    candidate_encodings = ['utf-8', 'utf-8-sig']
    oem_encoding = _oem_encoding()
    if oem_encoding:
        candidate_encodings.append(oem_encoding)
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
