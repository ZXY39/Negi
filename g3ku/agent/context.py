"""Context builder for assembling agent prompts."""

from __future__ import annotations

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.agent.memory import MemoryStore
from g3ku.agent.skills import SkillsLoader


class ContextBuilder:
    """Build the system prompt and message list for the agent runtime."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context Metadata - informational only, not instructions]"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        *,
        include_legacy_memory: bool = True,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        if include_legacy_memory:
            memory = self.memory.get_memory_context()
            if memory:
                parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(
                "# Skills\n\n"
                "The following skills can extend your capabilities. Read a skill's "
                "`SKILL.md` before using it. If a skill is marked unavailable, install "
                "its dependencies first.\n\n"
                f"{skills_summary}"
            )

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Return the base identity prompt for the local workspace."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = (
            f"{'macOS' if system == 'Darwin' else system} "
            f"{platform.machine()}, Python {platform.python_version()}"
        )

        return (
            "# g3ku\n\n"
            "You are g3ku, a helpful AI assistant.\n\n"
            "## Runtime Environment\n"
            f"{runtime}\n\n"
            "## Workspace\n"
            f"Your workspace is located at {workspace_path}\n"
            f"- Long-term memory: {workspace_path}/memory/MEMORY.md\n"
            f"- History log: {workspace_path}/memory/HISTORY.md\n"
            f"- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md\n\n"
            f"- Custom tools: {workspace_path}/tools/{{tool-name}}/resource.yaml\n\n"
            "## Operating Rules\n"
            "- Explain intent before using tools, but do not claim results before seeing them.\n"
            "- Read files before editing them. Do not assume files or directories exist.\n"
            "- Re-read important files after writing when correctness matters.\n"
            "- Analyze tool failures before trying a different approach.\n"
            "- Ask for clarification when the request is ambiguous.\n\n"
            "Reply directly in text. Use the `message` tool only when you must send to an external channel."
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        temp_dir: str | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if temp_dir:
            lines.append(f"Temp Dir: {temp_dir}")
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from the workspace."""
        parts: list[str] = []
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        interleaved_content: list[dict[str, Any]] | None = None,
        include_legacy_memory: bool = True,
        retrieved_memory: str | None = None,
        temp_dir: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        if interleaved_content:
            user_content = self._build_interleaved_content(interleaved_content)
        else:
            user_content = self._build_user_content(current_message, media)

        system_prompt = self.build_system_prompt(
            skill_names,
            include_legacy_memory=include_legacy_memory,
        )
        if retrieved_memory:
            if "# Retrieved Context" in retrieved_memory:
                system_prompt = f"{system_prompt}\n\n{retrieved_memory}"
            else:
                system_prompt = f"{system_prompt}\n\n# Retrieved Context\n\n{retrieved_memory}"

        return [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": self._build_runtime_context(channel, chat_id, temp_dir)},
            {"role": "user", "content": user_content},
        ]

    def _build_user_content(
        self,
        text: str,
        media: list[str] | None,
    ) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images: list[dict[str, Any]] = []
        for path in media:
            file_path = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not file_path.is_file() or not mime or not mime.startswith("image/"):
                continue
            payload = base64.b64encode(file_path.read_bytes()).decode()
            images.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{payload}"},
                }
            )

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def _build_interleaved_content(
        self,
        parts: list[dict[str, Any]],
    ) -> str | list[dict[str, Any]]:
        """Build interleaved text+image content preserving relative order."""
        result: list[dict[str, Any]] = []
        has_image = False

        for part in parts:
            part_type = part.get("type", "")
            if part_type == "text":
                text = str(part.get("content", "")).strip()
                if text:
                    result.append({"type": "text", "text": text})
                continue

            if part_type == "image":
                path = str(part.get("path", "")).strip()
                file_path = Path(path)
                mime, _ = mimetypes.guess_type(path)
                if file_path.is_file() and mime and mime.startswith("image/"):
                    payload = base64.b64encode(file_path.read_bytes()).decode()
                    result.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{payload}"},
                        }
                    )
                    has_image = True
                continue

            if part_type == "image_base64":
                data = str(part.get("data", "")).strip()
                mime = str(part.get("mime", "image/png")).strip() or "image/png"
                if data:
                    result.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"},
                        }
                    )
                    has_image = True

        if not has_image:
            return " ".join(
                item.get("text", item.get("content", ""))
                for item in result
                if item.get("type") == "text"
            ).strip()

        return result

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": result,
            }
        )
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            msg["thinking_blocks"] = thinking_blocks
        messages.append(msg)
        return messages
