"""Picture washing tool that calls a DOUBAO-compatible image generation API."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from g3ku.agent.tools.base import Tool


SUPPORTED_RATIOS = {"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2"}


class PictureWashingTool(Tool):
    """Call DOUBAO /v1/images/generations with normalized inputs and outputs."""

    def __init__(
        self,
        defaults: dict[str, Any] | None = None,
        agent_browser_defaults: dict[str, Any] | None = None,
    ):
        cfg = defaults or {}
        self.default_base_url = str(cfg.get("base_url") or "").strip()
        self.default_authorization = str(cfg.get("authorization") or "").strip()
        self.default_style = str(cfg.get("style") or "鍐欏疄").strip() or "鍐欏疄"
        self.default_model = str(cfg.get("model") or "Seedream 4.5").strip() or "Seedream 4.5"
        self.default_stream = bool(cfg.get("stream", False))
        self.default_timeout_s = self._normalize_timeout(cfg.get("timeout_s", 120))

        self.auto_probe_authorization = bool(cfg.get("auto_probe_authorization", True))
        self.default_authorization_probe_url = str(cfg.get("authorization_probe_url") or "").strip()
        self.authorization_probe_timeout_s = self._normalize_timeout(
            cfg.get("authorization_probe_timeout_s", 45)
        )

        cookie_names = cfg.get("authorization_cookie_names") or ["sessionid", "session_id"]
        self.authorization_cookie_names = {
            str(name).strip().lower() for name in cookie_names if str(name).strip()
        }
        if not self.authorization_cookie_names:
            self.authorization_cookie_names = {"sessionid"}

        self.agent_browser_defaults = dict(agent_browser_defaults or {})

    @property
    def name(self) -> str:
        return "picture_washing"

    @property
    def description(self) -> str:
        return (
            "Generate images for picture-washing by calling a DOUBAO-compatible "
            "POST /v1/images/generations endpoint. "
            "Use config-injected defaults for base_url/authorization when not provided by call args. "
            "When authorization is missing, auto-probe session cookie via agent_browser and compose Bearer token."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": (
                        "Optional override. DOUBAO service base URL, e.g. http://localhost:8000. "
                        "If omitted, use tools.pictureWashing.baseUrl from config."
                    ),
                },
                "authorization": {
                    "type": "string",
                    "description": (
                        "Optional override. Bearer token or raw sessionid string. "
                        "If omitted, use tools.pictureWashing.authorization from config, "
                        "or auto-probe session cookie via agent_browser if enabled."
                    ),
                },
                "authorization_probe_url": {
                    "type": "string",
                    "description": (
                        "Optional override. URL used by agent_browser when authorization is missing. "
                        "If omitted, use tools.pictureWashing.authorizationProbeUrl or infer from base_url."
                    ),
                },
                "auto_probe_authorization": {
                    "type": "boolean",
                    "description": "Optional override to enable/disable authorization auto-probing.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Final compliant image prompt.",
                },
                "image": {
                    "type": "string",
                    "description": "Product image URL or data:image/...;base64,...",
                },
                "ratio": {
                    "type": "string",
                    "description": "Target ratio, e.g. 1:1, 3:4, 4:3, 16:9, 9:16.",
                },
                "style": {
                    "type": "string",
                    "description": "Optional generation style override.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional generation model override.",
                },
                "stream": {
                    "type": "boolean",
                    "description": "Optional stream override.",
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "Optional HTTP timeout override in seconds.",
                    "minimum": 10,
                    "maximum": 600,
                },
            },
            "required": ["prompt", "image", "ratio"],
        }

    async def execute(
        self,
        prompt: str,
        image: str,
        ratio: str,
        base_url: str | None = None,
        authorization: str | None = None,
        authorization_probe_url: str | None = None,
        auto_probe_authorization: bool | None = None,
        style: str | None = None,
        model: str | None = None,
        stream: bool | None = None,
        timeout_s: int | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = kwargs.get("__g3ku_runtime") if isinstance(kwargs.get("__g3ku_runtime"), dict) else {}
        resolved_base_url = (base_url or self.default_base_url).strip()
        resolved_authorization = (authorization or self.default_authorization).strip()
        auth_probe_url_value = (
            (authorization_probe_url or self.default_authorization_probe_url).strip()
            or self._infer_probe_url(resolved_base_url)
        )
        auto_probe_enabled = (
            self.auto_probe_authorization
            if auto_probe_authorization is None
            else bool(auto_probe_authorization)
        )

        authorization_source = "call_arg" if authorization else "config" if self.default_authorization else "missing"
        auth_probe_meta: dict[str, Any] | None = None

        if not resolved_authorization and resolved_base_url and auto_probe_enabled:
            await self._emit_progress(runtime, "authorization missing, probing session cookie via agent_browser")
            probed_auth, auth_probe_meta = await self._probe_authorization_via_browser(
                base_url=resolved_base_url,
                probe_url=auth_probe_url_value,
                runtime=runtime,
            )
            if probed_auth:
                resolved_authorization = probed_auth
                authorization_source = "auto_agent_browser"

        missing: list[str] = []
        if not resolved_base_url:
            missing.append("base_url")
        if not resolved_authorization:
            missing.append("authorization")
        if missing:
            return self._dump(
                {
                    "success": False,
                    "error": (
                        "Missing required connection settings: "
                        + ", ".join(missing)
                        + ". Configure tools.pictureWashing in config, pass call-time overrides, "
                        + "or enable authorization auto-probing via agent_browser."
                    ),
                    "requestMeta": {
                        "missing": missing,
                        "authorizationSource": authorization_source,
                        "authorizationProbe": auth_probe_meta,
                        "configFallbackUsed": {
                            "base_url": not bool(base_url),
                            "authorization": not bool(authorization),
                            "authorization_probe_url": not bool(authorization_probe_url),
                            "auto_probe_authorization": auto_probe_authorization is None,
                        },
                    },
                    "images": [],
                    "raw": None,
                }
            )

        endpoint = self._resolve_generation_endpoint(resolved_base_url)
        ratio_value = (ratio or "").strip()
        if ratio_value not in SUPPORTED_RATIOS:
            return self._dump(
                {
                    "success": False,
                    "error": "Invalid ratio. Supported: " + ", ".join(sorted(SUPPORTED_RATIOS)),
                    "requestMeta": {
                        "endpoint": endpoint,
                        "ratio": ratio_value,
                        "authorizationSource": authorization_source,
                        "authorizationProbe": auth_probe_meta,
                    },
                    "images": [],
                    "raw": None,
                }
            )

        prompt_value = (prompt or "").strip()
        image_value = (image or "").strip()
        if not prompt_value or not image_value:
            return self._dump(
                {
                    "success": False,
                    "error": "Both prompt and image must be non-empty strings",
                    "requestMeta": {
                        "endpoint": endpoint,
                        "ratio": ratio_value,
                        "authorizationSource": authorization_source,
                        "authorizationProbe": auth_probe_meta,
                    },
                    "images": [],
                    "raw": None,
                }
            )

        normalized_auth = self._normalize_authorization(resolved_authorization)
        style_value = (style or self.default_style).strip() or self.default_style
        model_value = (model or self.default_model).strip() or self.default_model
        stream_value = self.default_stream if stream is None else bool(stream)
        timeout_value = self.default_timeout_s if timeout_s is None else self._normalize_timeout(timeout_s)

        body = {
            "model": model_value,
            "prompt": prompt_value,
            "image": image_value,
            "ratio": ratio_value,
            "style": style_value,
            "stream": stream_value,
        }
        headers = {"Authorization": normalized_auth, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=float(timeout_value)) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                status = response.status_code
                response.raise_for_status()

            try:
                payload: Any = response.json()
            except Exception:
                payload = {"raw_text": response.text}

            images = self._extract_images(payload, resolved_base_url)
            success = bool(images)
            return self._dump(
                {
                    "success": success,
                    "error": None if success else "No image URLs found in response",
                    "requestMeta": {
                        "endpoint": endpoint,
                        "status": status,
                        "model": body["model"],
                        "ratio": body["ratio"],
                        "style": body["style"],
                        "stream": body["stream"],
                        "authorizationSource": authorization_source,
                        "authorizationProbe": auth_probe_meta,
                        "configFallbackUsed": {
                            "base_url": not bool(base_url),
                            "authorization": not bool(authorization),
                            "authorization_probe_url": not bool(authorization_probe_url),
                            "auto_probe_authorization": auto_probe_authorization is None,
                            "style": style is None,
                            "model": model is None,
                            "stream": stream is None,
                            "timeout_s": timeout_s is None,
                        },
                    },
                    "images": images,
                    "raw": payload,
                }
            )

        except httpx.TimeoutException as exc:
            return self._dump(
                {
                    "success": False,
                    "error": f"Request timeout: {exc}",
                    "requestMeta": {
                        "endpoint": endpoint,
                        "timeout_s": timeout_value,
                        "authorizationSource": authorization_source,
                        "authorizationProbe": auth_probe_meta,
                    },
                    "images": [],
                    "raw": None,
                }
            )
        except httpx.HTTPStatusError as exc:
            response_payload: Any
            try:
                response_payload = exc.response.json()
            except Exception:
                response_payload = exc.response.text[:1200]
            return self._dump(
                {
                    "success": False,
                    "error": f"HTTP {exc.response.status_code}",
                    "requestMeta": {
                        "endpoint": endpoint,
                        "status": exc.response.status_code,
                        "authorizationSource": authorization_source,
                        "authorizationProbe": auth_probe_meta,
                    },
                    "images": [],
                    "raw": response_payload,
                }
            )
        except Exception as exc:
            return self._dump(
                {
                    "success": False,
                    "error": str(exc),
                    "requestMeta": {
                        "endpoint": endpoint,
                        "authorizationSource": authorization_source,
                        "authorizationProbe": auth_probe_meta,
                    },
                    "images": [],
                    "raw": None,
                }
            )

    async def _probe_authorization_via_browser(
        self,
        base_url: str,
        probe_url: str,
        runtime: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        meta: dict[str, Any] = {
            "attempted": True,
            "probeUrl": probe_url,
            "cookieNames": sorted(self.authorization_cookie_names),
        }
        if not probe_url:
            meta["error"] = "authorization probe url is empty"
            return "", meta

        try:
            from g3ku.agent.tools.agent_browser_client import AgentBrowserClient
        except Exception as exc:
            meta["error"] = f"agent_browser unavailable: {exc}"
            return "", meta

        client = AgentBrowserClient(defaults=self.agent_browser_defaults)
        probe_session = str((runtime or {}).get("session_key") or "picture_washing_auth_probe")

        open_payload: dict[str, Any] | None = None
        cookies_payload: dict[str, Any] | None = None

        try:
            open_payload = await self._call_agent_browser(
                client,
                command="open",
                args=[probe_url],
                session=probe_session,
                headless=True,
                timeout_s=self.authorization_probe_timeout_s,
            )
            if not open_payload.get("success"):
                meta["launch"] = open_payload
                meta["error"] = "agent_browser open failed"
                return "", meta

            cookies_payload = await self._call_agent_browser(
                client,
                command="cookies",
                args=[],
                session=probe_session,
                headless=True,
                timeout_s=self.authorization_probe_timeout_s,
            )
            if not cookies_payload.get("success"):
                meta["launch"] = open_payload
                meta["cookies"] = cookies_payload
                meta["error"] = "agent_browser cookies failed"
                return "", meta

            cookies = cookies_payload.get("data", {}).get("cookies", [])
            sessionid = self._extract_sessionid(cookies)
            if not sessionid:
                meta["launch"] = open_payload
                meta["cookies_count"] = len(cookies) if isinstance(cookies, list) else 0
                meta["error"] = "session cookie not found"
                return "", meta

            meta["launch"] = {"success": True}
            meta["goto"] = {"success": True, "url": probe_url}
            meta["cookies_count"] = len(cookies) if isinstance(cookies, list) else 0
            meta["matchedCookie"] = "sessionid"
            return f"Bearer {sessionid}", meta
        except Exception as exc:
            meta["error"] = str(exc)
            return "", meta
        finally:
            close_payload = await self._call_agent_browser(
                client,
                command="close",
                args=[],
                session=probe_session,
                headless=True,
                timeout_s=self.authorization_probe_timeout_s,
            )
            meta["close"] = {"success": bool(close_payload.get("success"))}

    async def _call_agent_browser(
        self,
        client: Any,
        command: str,
        args: list[str],
        session: str,
        headless: bool,
        timeout_s: int,
    ) -> dict[str, Any]:
        return await client.run(
            command=command,
            args=args,
            session=session,
            headless=headless,
            timeout_s=timeout_s,
        )

    def _extract_sessionid(self, cookies: Any) -> str:
        if not isinstance(cookies, list):
            return ""
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            value = str(item.get("value") or "").strip()
            if name in self.authorization_cookie_names and value:
                return value
        return ""

    @staticmethod
    def _infer_probe_url(base_url: str) -> str:
        parsed = urlparse((base_url or "").strip())
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return ""

    @staticmethod
    def _load_json_object(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                return {"success": False, "error": f"Invalid JSON response: {raw[:300]}"}
            if isinstance(parsed, dict):
                return parsed
            return {"success": False, "error": "JSON response is not an object"}
        return {"success": False, "error": f"Unsupported response type: {type(raw).__name__}"}

    async def _emit_progress(self, runtime: dict[str, Any], text: str) -> None:
        callback = runtime.get("on_progress") if isinstance(runtime, dict) else None
        if not callback:
            return
        try:
            await callback(f"[picture_washing] {text}")
        except Exception:
            pass

    @staticmethod
    def _normalize_timeout(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 120
        return max(10, min(parsed, 600))

    @staticmethod
    def _dump(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _normalize_authorization(value: str) -> str:
        token = (value or "").strip()
        if not token:
            return "Bearer "
        if token.lower().startswith("bearer "):
            return token
        return f"Bearer {token}"

    @staticmethod
    def _resolve_generation_endpoint(base_url: str) -> str:
        cleaned = (base_url or "").strip().rstrip("/")
        if cleaned.endswith("/v1/images/generations"):
            return cleaned
        if cleaned.endswith("/v1/responses"):
            return cleaned.rsplit("/", 1)[0] + "/images/generations"
        if cleaned.endswith("/v1"):
            return cleaned + "/images/generations"
        if cleaned.endswith("/v1/images"):
            return cleaned + "/generations"
        return cleaned + "/v1/images/generations"

    def _extract_images(self, payload: Any, base_url: str) -> list[str]:
        urls: list[str] = []

        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    urls.extend(self._collect_images_field(message.get("images"), base_url))
                    urls.extend(self._collect_details_field(message.get("image_details"), base_url))
                    content = message.get("content")
                    if isinstance(content, str):
                        urls.extend(self._extract_urls_from_text(content, base_url))
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and isinstance(block.get("text"), str):
                                urls.extend(self._extract_urls_from_text(block["text"], base_url))

            if not urls:
                urls.extend(self._collect_images_field(payload.get("images"), base_url))

            data = payload.get("data")
            if isinstance(data, list):
                urls.extend(self._collect_images_field(data, base_url))

        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped

    def _collect_images_field(self, value: Any, base_url: str) -> list[str]:
        urls: list[str] = []
        if not isinstance(value, list):
            return urls
        for item in value:
            if isinstance(item, str) and item.strip():
                urls.append(self._to_absolute_url(item.strip(), base_url))
                continue
            if isinstance(item, dict):
                for key in ("url", "original_url"):
                    raw = item.get(key)
                    if isinstance(raw, str) and raw.strip():
                        urls.append(self._to_absolute_url(raw.strip(), base_url))
                        break
        return urls

    def _collect_details_field(self, value: Any, base_url: str) -> list[str]:
        urls: list[str] = []
        if not isinstance(value, list):
            return urls
        for item in value:
            if not isinstance(item, dict):
                continue
            detail_url = item.get("url")
            original_url = item.get("original_url")
            if isinstance(detail_url, str) and detail_url.strip():
                urls.append(self._to_absolute_url(detail_url.strip(), base_url))
            elif isinstance(original_url, str) and original_url.strip():
                urls.append(self._to_absolute_url(original_url.strip(), base_url))
        return urls

    @staticmethod
    def _extract_urls_from_text(text: str, base_url: str) -> list[str]:
        found = re.findall(r"https?://[^\s\]\)\"']+|/v1/images/proxy\?url=[^\s\]\)\"']+", text)
        urls: list[str] = []
        for value in found:
            normalized = PictureWashingTool._to_absolute_url(value, base_url)
            if normalized:
                urls.append(normalized)
        return urls

    @staticmethod
    def _to_absolute_url(url: str, base_url: str) -> str:
        if not url:
            return ""
        if url.startswith("data:image/"):
            return url
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("//"):
            parsed = urlparse(base_url)
            scheme = parsed.scheme or "https"
            return f"{scheme}:{url}"
        parsed = urlparse(base_url)
        root = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else base_url
        return urljoin(root.rstrip("/") + "/", url.lstrip("/"))

