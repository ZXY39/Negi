from pathlib import Path
p = Path(r"D:\projects\G3KU\tools\web_fetch\main\tool.py")
s = p.read_text(encoding="utf-8")
start = s.index("    async def __call__(\n")
end = s.index("    async def _fetch_once(\n")
new_block = '''    async def __call__(
        self,
        url: str,
        max_chars: int = _MAX_TEXT_CHARS,
        extract_main_content: bool = True,
        include_raw_html: bool = False,
        use_cache: bool = True,
        timeout_ms: int = int(_DEFAULT_TIMEOUT_SECONDS * 1000),
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        normalized_url = _normalize_url(url)
        _assert_url_is_safe(normalized_url)
        max_chars = max(500, min(int(max_chars), _MAX_TEXT_CHARS))
        if timeout_seconds is None:
            effective_timeout_seconds = max(1.0, min(float(timeout_ms) / 1000.0, 30.0))
        else:
            effective_timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))

        cache_key = _cache_key(normalized_url, max_chars, extract_main_content, include_raw_html)
        if use_cache:
            cached = self._load_cache(cache_key)
            if cached is not None:
                cached["cache"] = {"hit": True, "mode": cached.get("cache", {}).get("mode", "memory_or_disk")}
                return cached

        result = await self._fetch_once(
            url=normalized_url,
            max_chars=max_chars,
            extract_main_content=extract_main_content,
            include_raw_html=include_raw_html,
            timeout_seconds=effective_timeout_seconds,
        )
        result["cache"] = {"hit": False, "mode": "write-through" if use_cache else "disabled"}
        if use_cache:
            self._store_cache(cache_key, result)
        return result

'''
p.write_text(s[:start] + new_block + s[end:], encoding="utf-8")
print("patched")
