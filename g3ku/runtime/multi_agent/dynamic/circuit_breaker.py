from __future__ import annotations

import json
from collections import deque
from typing import Any, Deque

from g3ku.runtime.multi_agent.dynamic.prompt_builder import COMPLETION_PROMISE_TOKEN


class RepeatedActionCircuitBreaker:
    def __init__(self, *, window: int = 3, threshold: int = 3) -> None:
        self.window = max(1, int(window or 1))
        self.threshold = max(1, int(threshold or 1))
        self._recent: Deque[str] = deque(maxlen=self.window)

    def record(self, signature: str) -> None:
        normalized = str(signature or "").strip()
        if not normalized:
            return
        self._recent.append(normalized)
        if len(self._recent) < self.threshold:
            return
        tail = list(self._recent)[-self.threshold:]
        if len(set(tail)) == 1:
            raise RuntimeError(f"Repeated action circuit breaker triggered for signature: {normalized}")


class DynamicSubagentTerminationGuard:
    def __init__(
        self,
        *,
        window: int = 3,
        threshold: int = 3,
        max_browser_steps: int = 10,
        browser_no_progress_threshold: int = 3,
        max_summaries: int = 8,
    ) -> None:
        self._repeated = RepeatedActionCircuitBreaker(window=window, threshold=threshold)
        self.max_browser_steps = max(1, int(max_browser_steps or 1))
        self.browser_no_progress_threshold = max(1, int(browser_no_progress_threshold or 1))
        self._recent_summaries: Deque[str] = deque(maxlen=max(1, int(max_summaries or 1)))
        self._browser_steps = 0
        self._browser_stall_count = 0
        self._last_browser_fingerprint: str | None = None
        self._last_browser_summary: str = ""
        self._tripped_reason: str | None = None

    @property
    def tripped_reason(self) -> str | None:
        return self._tripped_reason

    @property
    def summary_count(self) -> int:
        return len(self._recent_summaries)

    def before_tool(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        if self._tripped_reason:
            return self.guard_result_message(self._tripped_reason)
        signature = json.dumps({"tool": tool_name, "args": arguments}, ensure_ascii=False, sort_keys=True)
        try:
            self._repeated.record(signature)
        except RuntimeError as exc:
            self._trip(str(exc))
            return self.guard_result_message(self._tripped_reason or str(exc))
        return None

    def after_tool(self, tool_name: str, arguments: dict[str, Any], result_content: str) -> str | None:
        self._remember_summary(tool_name, arguments, result_content)
        if tool_name != "agent_browser":
            return self.guard_result_message(self._tripped_reason) if self._tripped_reason else None

        command = str(arguments.get("command") or "").strip().lower()
        if command:
            self._browser_steps += 1
        payload = self._parse_json(result_content)
        fingerprint = self._browser_fingerprint(payload)
        if fingerprint:
            if fingerprint == self._last_browser_fingerprint:
                self._browser_stall_count += 1
            else:
                self._browser_stall_count = 0
            self._last_browser_fingerprint = fingerprint
            self._last_browser_summary = self._browser_summary(payload)
        elif command in {"click", "fill", "press", "wait", "snapshot", "state"}:
            self._browser_stall_count += 1

        if self._browser_steps >= self.max_browser_steps:
            self._trip(
                f"Browser step budget exceeded ({self._browser_steps}/{self.max_browser_steps}) without a stable final answer"
            )
        elif self._browser_stall_count >= self.browser_no_progress_threshold:
            self._trip(
                f"Browser no-progress guard triggered after {self._browser_stall_count} consecutive unchanged states"
            )

        return self.guard_result_message(self._tripped_reason) if self._tripped_reason else None

    def build_fallback_output(self, *, reason: str) -> str:
        lines = [
            f"Dynamic subagent stopped early: {reason}.",
            "",
            "Observed progress:",
        ]
        recent = list(self._recent_summaries)[-5:]
        if recent:
            lines.extend(f"- {item}" for item in recent)
        else:
            lines.append("- No verified tool output was captured before the stop condition.")
        if self._last_browser_summary:
            lines.append(f"- Last browser state: {self._last_browser_summary}")
        lines.extend(
            [
                "",
                "Return the best verified partial result only, clearly list blockers or missing evidence, and stop.",
                COMPLETION_PROMISE_TOKEN,
            ]
        )
        return "\n".join(lines)

    def guard_result_message(self, reason: str) -> str:
        return (
            f"[SUBAGENT_GUARD] {reason}. Stop calling tools. Return the best verified partial result, "
            f"list blockers, and end the final answer with {COMPLETION_PROMISE_TOKEN}."
        )

    def _trip(self, reason: str) -> None:
        if not self._tripped_reason:
            self._tripped_reason = str(reason or "Dynamic subagent termination guard triggered.").strip()

    def _remember_summary(self, tool_name: str, arguments: dict[str, Any], result_content: str) -> None:
        command = str(arguments.get("command") or "").strip().lower()
        if tool_name == "agent_browser":
            payload = self._parse_json(result_content)
            summary = self._browser_summary(payload)
            if summary:
                label = f"agent_browser {command}".strip()
                self._recent_summaries.append(f"{label}: {summary}")
                return
        snippet = " ".join(str(result_content or "").split())
        if snippet:
            label = f"{tool_name} {command}".strip()
            self._recent_summaries.append(f"{label}: {snippet[:180]}")

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _browser_fingerprint(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        fingerprint = {
            "url": data.get("url") or data.get("origin") or ((data.get("page") or {}).get("url") if isinstance(data.get("page"), dict) else None),
            "title": data.get("title") or ((data.get("page") or {}).get("title") if isinstance(data.get("page"), dict) else None),
            "refs": len(data.get("refs") or {}) if isinstance(data.get("refs"), dict) else None,
        }
        compact = {key: value for key, value in fingerprint.items() if value not in {None, ""}}
        if not compact:
            return None
        return json.dumps(compact, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _browser_summary(payload: dict[str, Any] | None) -> str:
        if not payload:
            return ""
        data = payload.get("data")
        if not isinstance(data, dict):
            return ""
        parts: list[str] = []
        title = str(data.get("title") or ((data.get("page") or {}).get("title") if isinstance(data.get("page"), dict) else "")).strip()
        url = str(data.get("url") or data.get("origin") or ((data.get("page") or {}).get("url") if isinstance(data.get("page"), dict) else "")).strip()
        refs = data.get("refs")
        if title:
            parts.append(f"title={title}")
        if url:
            parts.append(f"url={url}")
        if isinstance(refs, dict):
            parts.append(f"refs={len(refs)}")
        error = str(payload.get("error") or "").strip()
        if error:
            parts.append(f"error={error[:120]}")
        return ", ".join(parts)

