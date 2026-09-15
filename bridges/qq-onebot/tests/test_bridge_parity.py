"""Behavior-parity tests for the QQ/OneBot reference bridge.

Fakes stand in for the OneBot endpoint and the g3ku API; the dispatcher is
exercised against the legacy QQ semantics parity table (control commands,
session mapping, require-at triggering, queued receipts, progress throttling,
final splitting, outbound push).
"""

from __future__ import annotations

import asyncio

import pytest

from qq_onebot_bridge.config import BehaviorConfig
from qq_onebot_bridge.dispatcher import (
    Dispatcher,
    external_key_for_event,
    normalize_control_command_text,
    parse_message_content,
    parse_target,
    split_outbound_text,
)


class _FakeG3ku:
    def __init__(self, *, send_status="started", turn_id="t1"):
        self.sessions: dict[str, str] = {}
        self.sent: list[dict] = []
        self.paused: list[str] = []
        self.cancelled: list[str] = []
        self.send_status = send_status
        self.turn_id = turn_id
        self._active: dict[str, str] = {}
        self.media_downloads: list[str] = []

    async def ensure_session(self, external_key, *, title=None):
        session_id = self.sessions.setdefault(external_key, f"ext:test:{len(self.sessions)}")
        return session_id

    def external_key_for(self, session_id):
        for key, known in self.sessions.items():
            if known == session_id:
                return key
        return None

    async def send_message(self, session_id, *, text, attachments=None, sender=None, idempotency_key=None):
        self.sent.append(
            {
                "session_id": session_id,
                "text": text,
                "attachments": attachments,
                "idempotency_key": idempotency_key,
            }
        )
        body = {"status": self.send_status, "turn_id": self.turn_id if self.send_status == "started" else None}
        if self.send_status == "queued":
            body["receipt"] = "收到，将在当前任务中一并处理。"
        if self.send_status == "started":
            self._active[session_id] = self.turn_id
        return body

    async def pause_turn(self, turn_id):
        self.paused.append(turn_id)
        return True

    async def cancel_session(self, session_id):
        self.cancelled.append(session_id)
        return 1

    def active_turn_id(self, session_id):
        return self._active.get(session_id)

    def forget_turn(self, session_id):
        self._active.pop(session_id, None)

    def last_seq(self, session_id):
        return 0

    async def stream_events(self, session_id, *, on_event, backoff_seconds=3.0, stop=None):
        await asyncio.sleep(3600)

    # -- outbound media (event attachments) ---------------------------------
    media_bytes: bytes = b"file-bytes"

    @property
    def base_url(self):
        return "http://127.0.0.1:18790"

    async def download_media(self, url, *, max_bytes):
        self.media_downloads.append(url)
        return self.media_bytes


class _FakeOnebot:
    def __init__(self):
        self.private: list[tuple] = []
        self.group: list[tuple] = []
        self.downloads: list[str] = []
        self.actions: list[tuple] = []
        self.file_url_response: dict = {"url": "https://cdn.example/group-file.docx"}
        self.fail_actions: set[str] = set()

    async def send_private_msg(self, user_id, text):
        self.private.append((str(user_id), text))

    async def send_group_msg(self, group_id, text):
        self.group.append((str(group_id), text))

    async def download_bytes(self, url, *, max_bytes):
        self.downloads.append(url)
        return b"img-bytes"

    async def call_action(self, action, **params):
        self.actions.append((action, params))
        if action in self.fail_actions:
            raise RuntimeError(f"action failed: {action}")
        if action == "get_group_file_url":
            return dict(self.file_url_response)
        return {}


def _dispatcher(**kwargs) -> tuple[Dispatcher, _FakeG3ku, _FakeOnebot]:
    g3ku = _FakeG3ku(**{k: v for k, v in kwargs.items() if k in {"send_status", "turn_id"}})
    onebot = _FakeOnebot()
    behavior = kwargs.get("behavior") or BehaviorConfig(bot_user_id=999)
    return Dispatcher(g3ku=g3ku, onebot=onebot, behavior=behavior, bot_user_id=999), g3ku, onebot


# -- pure helpers -----------------------------------------------------------


def test_normalize_control_command_variants():
    assert normalize_control_command_text("暂停。") == "暂停"
    assert normalize_control_command_text(" Pause! ") == "pause"
    assert normalize_control_command_text("/pause") == "/pause"
    assert normalize_control_command_text("继续") == "继续"


def test_external_key_for_event():
    assert external_key_for_event({"post_type": "message", "message_type": "private", "user_id": 7}) == "qq:dm:7"
    assert external_key_for_event({"post_type": "message", "message_type": "group", "group_id": 42}) == "qq:group:42"
    assert external_key_for_event({"post_type": "notice"}) is None


def test_parse_message_content_array():
    message = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": " 帮我查一下 "}},
        {"type": "image", "data": {"url": "https://cdn.example/a.png"}},
    ]
    text, images, files, at_bot = parse_message_content(message, bot_user_id=999)
    assert text == "帮我查一下"
    assert images == ["https://cdn.example/a.png"]
    assert files == []
    assert at_bot is True


def test_parse_message_content_cq_string():
    raw = "[CQ:at,qq=999] 看看这个 [CQ:image,file=x,url=https://cdn.example/b.jpg]"
    text, images, files, at_bot = parse_message_content(raw, bot_user_id=999)
    assert "看看这个" in text and "[CQ:" not in text
    assert images == ["https://cdn.example/b.jpg"]
    assert files == []
    assert at_bot is True


def test_parse_message_content_file_segment_array():
    """私聊文件消息：NapCat file 段带直连 url、文件名与大小。"""
    message = [
        {"type": "text", "data": {"text": "看下文档"}},
        {
            "type": "file",
            "data": {
                "file": "abc.docx",
                "url": "https://cdn.example/abc.docx",
                "name": "报告.docx",
                "size": 12345,
            },
        },
    ]
    text, images, files, at_bot = parse_message_content(message, bot_user_id=999)
    assert text == "看下文档"
    assert images == []
    assert at_bot is False
    assert files == [
        {"url": "https://cdn.example/abc.docx", "name": "报告.docx", "size": 12345, "id": None, "busid": None}
    ]


def test_parse_message_content_file_segment_group_without_url():
    """群文件通常只带 id+busid：下载链接稍后经 get_group_file_url 换取。"""
    message = [
        {
            "type": "file",
            "data": {"id": "fid-1", "name": "表格.xlsx", "size": 999, "busid": "bus-7"},
        }
    ]
    text, images, files, at_bot = parse_message_content(message, bot_user_id=999)
    assert text == "" and images == [] and at_bot is False
    assert files == [
        {"url": None, "name": "表格.xlsx", "size": 999, "id": "fid-1", "busid": "bus-7"}
    ]


def test_parse_message_content_file_cq_string():
    raw = "[CQ:file,file=abc.docx,url=https://cdn.example/abc.docx,name=报告.docx,size=123]"
    text, images, files, at_bot = parse_message_content(raw, bot_user_id=999)
    assert text == "" and images == [] and at_bot is False
    assert files == [
        {"url": "https://cdn.example/abc.docx", "name": "报告.docx", "size": 123, "id": None, "busid": None}
    ]


def test_split_outbound_text_prefers_line_boundaries():
    text = "a" * 30 + "\n" + "b" * 30 + "\n" + "c" * 10
    chunks = split_outbound_text(text, max_length=40)
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == "a" * 30 + "b" * 30 + "c" * 10


def test_parse_target():
    assert parse_target("qq:dm:7") == ("private", "7")
    assert parse_target("qq:group:42") == ("group", "42")
    assert parse_target("other:key") is None


# -- dispatch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_message_starts_turn_with_idempotency_key():
    dispatcher, g3ku, _ = _dispatcher()
    await dispatcher.handle_onebot_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 7,
            "message_id": 1001,
            "event_id": "evt-1",
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "sender": {"nickname": "某人"},
        }
    )
    assert len(g3ku.sent) == 1
    assert g3ku.sent[0]["text"] == "你好"
    assert g3ku.sent[0]["idempotency_key"] == "evt-1"
    assert g3ku.sent[0]["session_id"] == g3ku.sessions["qq:dm:7"]


@pytest.mark.asyncio
async def test_group_message_requires_at():
    dispatcher, g3ku, _ = _dispatcher()
    await dispatcher.handle_onebot_event(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 42,
            "message": [{"type": "text", "data": {"text": "没at不触发"}}],
        }
    )
    assert g3ku.sent == []

    await dispatcher.handle_onebot_event(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 42,
            "message": [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": "触发"}},
            ],
        }
    )
    assert len(g3ku.sent) == 1
    assert g3ku.sent[0]["text"] == "触发"


@pytest.mark.asyncio
async def test_pause_command_routes_to_pause_api():
    dispatcher, g3ku, onebot = _dispatcher(send_status="started", turn_id="t-77")
    # prime: send a normal message so an active turn exists
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "开始干活"}
    )
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "暂停。"}
    )
    assert g3ku.paused == ["t-77"]
    assert onebot.private == [("7", "已暂停。")]


@pytest.mark.asyncio
async def test_pause_without_active_turn_replies_no_task():
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "暂停"}
    )
    assert g3ku.paused == []
    assert onebot.private == [("7", "当前没有正在进行的任务。")]


@pytest.mark.asyncio
async def test_stop_command_cancels_session():
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "/stop"}
    )
    assert g3ku.cancelled == [g3ku.sessions["qq:dm:7"]]
    assert onebot.private == [("7", "Stopped 1 task(s).")]


@pytest.mark.asyncio
async def test_queued_status_forwards_receipt():
    dispatcher, g3ku, onebot = _dispatcher(send_status="queued")
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "追一句"}
    )
    assert onebot.private == [("7", "收到，将在当前任务中一并处理。")]


@pytest.mark.asyncio
async def test_image_attachment_downloaded_and_base64():
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 7,
            "message": [
                {"type": "text", "data": {"text": "看图"}},
                {"type": "image", "data": {"url": "https://cdn.example/a.png"}},
            ],
        }
    )
    assert onebot.downloads == ["https://cdn.example/a.png"]
    attachments = g3ku.sent[0]["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["kind"] == "image"
    assert attachments[0]["data_base64"]


@pytest.mark.asyncio
async def test_g3ku_events_route_back_to_qq():
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "group", "group_id": 42,
         "message": [{"type": "at", "data": {"qq": "999"}}, {"type": "text", "data": {"text": "hi"}}]}
    )
    session_id = g3ku.sessions["qq:group:42"]

    await dispatcher._handle_g3ku_event(session_id, {"type": "progress", "text": "🔧 执行 shell"})
    await dispatcher._handle_g3ku_event(session_id, {"type": "reply.final", "text": "最终答复"})
    assert onebot.group == [("42", "🔧 执行 shell"), ("42", "最终答复")]

    await dispatcher._handle_g3ku_event(
        session_id, {"type": "outbound.created", "text": "定时提醒", "external_key": "qq:group:42"}
    )
    assert onebot.group[-1] == ("42", "定时提醒")


@pytest.mark.asyncio
async def test_final_only_ignores_progress():
    behavior = BehaviorConfig(bot_user_id=999, final_only=True)
    dispatcher, g3ku, onebot = _dispatcher(behavior=behavior)
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "hi"}
    )
    session_id = g3ku.sessions["qq:dm:7"]
    await dispatcher._handle_g3ku_event(session_id, {"type": "progress", "text": "过程"})
    await dispatcher._handle_g3ku_event(session_id, {"type": "reply.final", "text": "结果"})
    assert onebot.private == [("7", "结果")]


@pytest.mark.asyncio
async def test_progress_flush_respects_max_lines():
    behavior = BehaviorConfig(bot_user_id=999, progress_max_lines_per_message=2)
    dispatcher, g3ku, onebot = _dispatcher(behavior=behavior)
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "hi"}
    )
    session_id = g3ku.sessions["qq:dm:7"]
    for i in range(5):
        await dispatcher._handle_g3ku_event(session_id, {"type": "progress", "text": f"步骤{i}"})
    await dispatcher._flush_progress(session_id)
    assert onebot.private == [("7", "步骤0\n步骤1")]


# -- inbound files ----------------------------------------------------------


@pytest.mark.asyncio
async def test_file_message_downloaded_and_forwarded():
    """私聊文件消息不再被静默丢弃：下载后以 kind=file 提交，mime 按文件名推断。"""
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 7,
            "message": [
                {
                    "type": "file",
                    "data": {"url": "https://cdn.example/报告.docx", "name": "报告.docx", "size": 123},
                }
            ],
        }
    )
    assert onebot.downloads == ["https://cdn.example/报告.docx"]
    assert len(g3ku.sent) == 1
    payload = g3ku.sent[0]
    assert payload["text"] == ""
    attachments = payload["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["kind"] == "file"
    assert attachments[0]["name"] == "报告.docx"
    assert attachments[0]["mime_type"].endswith("wordprocessingml.document")
    assert attachments[0]["data_base64"]


@pytest.mark.asyncio
async def test_group_file_resolved_via_get_group_file_url():
    """群文件只带 id+busid：桥调 get_group_file_url 换取下载链接。"""
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 42,
            "message": [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "file", "data": {"id": "fid-1", "name": "表格.xlsx", "busid": "bus-7"}},
            ],
        }
    )
    assert onebot.actions == [
        ("get_group_file_url", {"group_id": 42, "file_id": "fid-1", "busid": "bus-7"})
    ]
    assert onebot.downloads == ["https://cdn.example/group-file.docx"]
    attachments = g3ku.sent[0]["attachments"]
    assert len(attachments) == 1 and attachments[0]["kind"] == "file"


@pytest.mark.asyncio
async def test_unresolvable_file_noted_in_text():
    """拿不到下载链接的文件不得静默吞掉：正文追加提示，回合照常提交。"""
    dispatcher, g3ku, onebot = _dispatcher()
    onebot.fail_actions.add("get_group_file_url")
    await dispatcher.handle_onebot_event(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 42,
            "message": [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "file", "data": {"id": "fid-2", "name": "数据.zip", "busid": "bus-8"}},
            ],
        }
    )
    payload = g3ku.sent[0]
    assert "[文件 数据.zip 未能获取]" in payload["text"]
    assert payload["attachments"] is None


# -- outbound files ---------------------------------------------------------


@pytest.mark.asyncio
async def test_outbound_event_attachment_sent_as_upload_action():
    """reply.final 携带附件：桥下载签名 URL 后经 upload_private_file 以
    base64:// 发送，正文随后单独发送。"""
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "hi"}
    )
    session_id = g3ku.sessions["qq:dm:7"]
    await dispatcher._handle_g3ku_event(
        session_id,
        {
            "type": "reply.final",
            "text": "日报已生成：日报",
            "attachments": [
                {"name": "日报.docx", "mime_type": "application/octet-stream", "size": 10, "url": "/api/ceo/media/original?token=t1"}
            ],
        },
    )
    assert g3ku.media_downloads == ["/api/ceo/media/original?token=t1"]
    upload = [action for action in onebot.actions if action[0] == "upload_private_file"]
    assert len(upload) == 1
    params = upload[0][1]
    assert params["user_id"] == 7
    assert params["name"] == "日报.docx"
    assert params["file"].startswith("base64://")
    assert onebot.private == [("7", "日报已生成：日报")]


@pytest.mark.asyncio
async def test_outbound_image_attachment_sent_as_cq_image():
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "group", "group_id": 42,
         "message": [{"type": "at", "data": {"qq": "999"}}, {"type": "text", "data": {"text": "hi"}}]}
    )
    session_id = g3ku.sessions["qq:group:42"]
    await dispatcher._handle_g3ku_event(
        session_id,
        {
            "type": "outbound.created",
            "text": "图表已生成：图表",
            "external_key": "qq:group:42",
            "attachments": [
                {"kind": "image", "name": "chart.png", "mime_type": "image/png", "size": 8, "url": "/api/ceo/media/original?token=t2"}
            ],
        },
    )
    assert len(onebot.group) == 2
    image_message = onebot.group[0][1]
    assert image_message.startswith("[CQ:image,file=base64://") and image_message.endswith("]")
    assert onebot.group[1] == ("42", "图表已生成：图表")


@pytest.mark.asyncio
async def test_outbound_attachment_failure_degrades_to_signed_link():
    """下载失败时附件降级为签名链接文本行，不静默丢失。"""
    dispatcher, g3ku, onebot = _dispatcher()
    g3ku.media_bytes = None
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "hi"}
    )
    session_id = g3ku.sessions["qq:dm:7"]
    await dispatcher._handle_g3ku_event(
        session_id,
        {
            "type": "reply.final",
            "text": "日报已生成：日报",
            "attachments": [
                {"name": "日报.docx", "url": "/api/ceo/media/original?token=t3"}
            ],
        },
    )
    assert onebot.private == [
        ("7", "日报已生成：日报\n📎 日报.docx: http://127.0.0.1:18790/api/ceo/media/original?token=t3")
    ]
