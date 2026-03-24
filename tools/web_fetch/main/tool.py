from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


_CACHE_TTL_SECONDS = 300
_DEFAULT_TIMEOUT_SECONDS = 12.0
_MAX_REDIRECTS = 5
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_TEXT_CHARS = 20_000
_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254"}
_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class _CacheEntry:
    def __init__(self, *, expires_at: float, payload: dict[str, Any]) -> None:
        self.expires_at = expires_at
        self.payload = payload


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._capture = False
        self._title_chunks: list[str] = []
        self._text_parts: list[str] = []
        self._current_href: str | None = None
        self._links: list[dict[str, str]] = []
        self._meta_description = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "iframe"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if self._skip_depth:
            return
        if tag in {"main", "article", "section", "body"}:
            self._capture = True
            self._text_parts.append("\n")
        if tag in {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._text_parts.append("\n")
        if tag == "meta" and attrs_map.get("name", "").lower() == "description":
            self._meta_description = attrs_map.get("content", "")
        if tag == "a":
            href = attrs_map.get("href", "").strip()
            self._current_href = href or None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "iframe"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in {"main", "article", "section", "body"}:
            self._text_parts.append("\n")
        if tag == "a":
            self._current_href = None

    def handle_data(self, data: str) -> None:
        if not data or self._skip_depth:
            return
        cleaned = _normalize_whitespace(data)
        if not cleaned:
            return
        if self._in_title:
            self._title_chunks.append(cleaned)
        self._text_parts.append(cleaned + " ")
        if self._current_href and len(self._links) < 20:
            self._links.append({"href": self._current_href, "text": cleaned[:200]})

    @property
    def title(self) -> str:
        return _normalize_whitespace(" ".join(self._title_chunks))

    @property
    def meta_description(self) -> str:
        return _normalize_whitespace(self._meta_description)

    @property
    def text(self) -> str:
        text = html.unescape("".join(self._text_parts))
        text = re.sub(r"\n{3,}", "\n\n", text)
        return _normalize_whitespace_per_line(text)

    @property
    def links(self) -> list[dict[str, str]]:
        return self._links


class WebFetchTool:
    def __init__(self, workspace: str | Path) -> None:
        workspace_path = Path(workspace)
        self.workspace = workspace_path
        self.cache_dir = workspace_path / ".g3ku" / "cache" / "web_fetch"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: dict[str, _CacheEntry] = {}

    async def __call__(
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

    async def _fetch_once(
        self,
        url: str,
        *,
        max_chars: int,
        extract_main_content: bool,
        include_raw_html: bool,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(timeout_seconds)
        limits = httpx.Limits(max_connections=5, max_keepalive_connections=2)
        headers = {
            "User-Agent": "G3KU-WebFetch/0.1 (+https://local.invalid)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,application/json;q=0.8,*/*;q=0.5",
        }
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True, max_redirects=_MAX_REDIRECTS, limits=limits) as client:
            response = await client.get(url)
            final_url = str(response.url)
            _assert_url_is_safe(final_url)
            body = response.content[:_MAX_RESPONSE_BYTES]
            content_type = response.headers.get("content-type", "")
            text = _decode_body(body, content_type)
            parser = _ArticleParser()
            parsed_title = ""
            parsed_description = ""
            links: list[dict[str, str]] = []
            extracted_text = text
            if "html" in content_type.lower() or text.lstrip().startswith("<"):
                try:
                    parser.feed(text)
                    parsed_title = parser.title
                    parsed_description = parser.meta_description
                    links = parser.links
                    if extract_main_content and parser.text:
                        extracted_text = parser.text
                except Exception:
                    extracted_text = _strip_html_fallback(text)
            else:
                extracted_text = text

            extracted_text = extracted_text[:max_chars]
            body_preview = _wrap_untrusted(extracted_text)
            raw_html = _wrap_untrusted(text[:max_chars]) if include_raw_html else None
            return {
                "ok": True,
                "url": url,
                "final_url": final_url,
                "status_code": response.status_code,
                "content_type": content_type,
                "title": parsed_title,
                "description": parsed_description,
                "text": body_preview,
                "links": links,
                "raw_html": raw_html,
                "security": {
                    "untrusted_content_wrapped": True,
                    "ssrf_guard": "blocked private, loopback, link-local, localhost and non-http(s) targets before and after redirect",
                    "max_response_bytes": _MAX_RESPONSE_BYTES,
                    "allow_javascript_execution": False,
                },
                "implementation": {
                    "main_content_extraction": "heuristic html parser + fallback tag stripping",
                    "browser_fallback": False,
                    "javascript_rendering": False,
                },
            }

    def _load_cache(self, cache_key: str) -> dict[str, Any] | None:
        now = time.time()
        entry = self._memory_cache.get(cache_key)
        if entry and entry.expires_at > now:
            return json.loads(json.dumps(entry.payload))
        disk_path = self.cache_dir / f"{cache_key}.json"
        if disk_path.exists():
            try:
                payload = json.loads(disk_path.read_text(encoding="utf-8"))
                if float(payload.get("_expires_at", 0)) > now:
                    payload.pop("_expires_at", None)
                    self._memory_cache[cache_key] = _CacheEntry(expires_at=now + _CACHE_TTL_SECONDS, payload=payload)
                    return payload
            except Exception:
                return None
        return None

    def _store_cache(self, cache_key: str, payload: dict[str, Any]) -> None:
        expires_at = time.time() + _CACHE_TTL_SECONDS
        safe_payload = json.loads(json.dumps(payload))
        self._memory_cache[cache_key] = _CacheEntry(expires_at=expires_at, payload=safe_payload)
        disk_payload = json.loads(json.dumps(payload))
        disk_payload["_expires_at"] = expires_at
        (self.cache_dir / f"{cache_key}.json").write_text(json.dumps(disk_payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_url(url: str) -> str:
    normalized = (url or "").strip()
    if not normalized:
        raise ValueError("url is required")
    return normalized


def _assert_url_is_safe(url: str) -> None:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError("only http and https URLs are allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("url host is required")
    if host in _BLOCKED_HOSTS or host.endswith('.local'):
        raise ValueError("target host is blocked by SSRF policy")
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(host, parsed.port or (443 if scheme == 'https' else 80), type=socket.SOCK_STREAM):
            ip_text = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip_text)
            if any(ip_obj in net for net in _PRIVATE_NETS):
                raise ValueError("target resolves to a private or local address")
    except socket.gaierror as exc:
        raise ValueError(f"host resolution failed: {exc}") from exc


def _cache_key(url: str, max_chars: int, extract_main_content: bool, include_raw_html: bool) -> str:
    digest = hashlib.sha256(f"{url}|{max_chars}|{extract_main_content}|{include_raw_html}".encode("utf-8")).hexdigest()
    return digest[:24]


def _decode_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w-]+)", content_type, flags=re.I)
    encodings = [charset_match.group(1)] if charset_match else []
    encodings += ["utf-8", "utf-16", "latin-1"]
    for encoding in encodings:
        try:
            return body.decode(encoding)
        except Exception:
            continue
    return body.decode("utf-8", errors="replace")


def _strip_html_fallback(text: str) -> str:
    no_scripts = re.sub(r"<script.*?</script>", " ", text, flags=re.I | re.S)
    no_styles = re.sub(r"<style.*?</style>", " ", no_scripts, flags=re.I | re.S)
    no_tags = re.sub(r"<[^>]+>", " ", no_styles)
    return _normalize_whitespace_per_line(html.unescape(no_tags))


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_whitespace_per_line(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _wrap_untrusted(text: str) -> str:
    return (
        "UNTRUSTED_EXTERNAL_CONTENT_BEGIN\n"
        + text.strip()
        + "\nUNTRUSTED_EXTERNAL_CONTENT_END"
    )


def build(runtime):
    return WebFetchTool(workspace=runtime.workspace)
