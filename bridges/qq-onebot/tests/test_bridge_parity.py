"""Behavior-parity tests for the QQ/OneBot reference bridge.

Fakes stand in for the OneBot endpoint and the g3ku API; the dispatcher is
exercised against the legacy QQ semantics parity table (control commands,
session mapping, require-at triggering, queued receipts, progress throttling,
final splitting, outbound push).
"""

from __future__ import annotations

import asyncio
import json

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
        self.acked: list[tuple[str, str]] = []
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

    async def ack_outbox(self, session_id, outbox_id):
        self.acked.append((session_id, outbox_id))
        return True

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
async def test_pause_command_with_fullwidth_exclamation_still_pauses():
    """中文输入法打出的"暂停！"必须是停机指令，不是一条交给 agent 的消息。"""
    dispatcher, g3ku, onebot = _dispatcher(send_status="started", turn_id="t-77")
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "开始干活"}
    )
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "暂停！"}
    )
    assert g3ku.paused == ["t-77"]
    assert onebot.private == [("7", "已暂停。")]


def test_control_command_normalization_strips_fullwidth_marks():
    assert normalize_control_command_text("暂停！") == "暂停"
    assert normalize_control_command_text("停止？") == "停止"
    assert normalize_control_command_text("暂停、") == "暂停"
    # 控制词之外的文本不许被改动：只有尾部标点被剥。
    assert normalize_control_command_text("暂停！任务") == "暂停！任务"


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
async def test_consumed_events_close_their_outbox_records():
    """投递确认后必须 ack：账本已不按年龄回收，未销账的记录会被服务端每小时
    重注入一次。回合回复与主动推送现在都带 ``outbox_id``。"""
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "group", "group_id": 42,
         "message": [{"type": "at", "data": {"qq": "999"}}, {"type": "text", "data": {"text": "hi"}}]}
    )
    session_id = g3ku.sessions["qq:group:42"]

    await dispatcher._handle_g3ku_event(
        session_id, {"type": "reply.final", "text": "答案", "outbox_id": "obx-r"}
    )
    await dispatcher._handle_g3ku_event(
        session_id,
        {"type": "outbound.created", "text": "提醒", "external_key": "qq:group:42", "outbox_id": "obx-p"},
    )
    assert g3ku.acked == [(session_id, "obx-r"), (session_id, "obx-p")]

    # 不带 outbox_id 的旧形状无账可销，不得凭空造 ack 调用。
    await dispatcher._handle_g3ku_event(session_id, {"type": "reply.final", "text": "无账可销"})
    assert g3ku.acked == [(session_id, "obx-r"), (session_id, "obx-p")]


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


@pytest.mark.asyncio
async def test_reply_final_drains_every_progress_line_before_the_answer():
    """答案之前必须把里程碑排空，不许留到答案之后的节流 tick。"""
    behavior = BehaviorConfig(bot_user_id=999, progress_max_lines_per_message=2)
    dispatcher, g3ku, onebot = _dispatcher(behavior=behavior)
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "hi"}
    )
    session_id = g3ku.sessions["qq:dm:7"]
    for i in range(5):
        await dispatcher._handle_g3ku_event(session_id, {"type": "progress", "text": f"步骤{i}"})

    await dispatcher._handle_g3ku_event(session_id, {"type": "reply.final", "text": "答案"})

    texts = [text for _, text in onebot.private]
    assert texts == ["步骤0\n步骤1", "步骤2\n步骤3", "步骤4", "答案"]
    assert session_id not in dispatcher._progress


@pytest.mark.asyncio
async def test_event_handler_exception_does_not_stop_the_bridge(monkeypatch):
    """一条消息处理失败只丢那一条：异常穿出 receive_events 会带走整个桥进程，
    而桥没有监督者重拉，QQ 侧从此再没有回复。"""
    from qq_onebot_bridge import onebot as onebot_module

    events = [
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "boom"},
        {"post_type": "message", "message_type": "private", "user_id": 8, "message": "ok"},
    ]

    class _FakeWs:
        def __aiter__(self):
            async def _gen():
                for item in events:
                    yield json.dumps(item)

            return _gen()

    class _Connector:
        def __call__(self, url, **kwargs):
            return self

        async def __aenter__(self):
            return _FakeWs()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(onebot_module.websockets, "connect", _Connector())
    stop = asyncio.Event()
    seen: list[int] = []

    async def _on_event(event):
        if str(event.get("message")) == "boom":
            raise RuntimeError("g3ku returned 500")
        seen.append(int(event["user_id"]))
        stop.set()

    client = onebot_module.OnebotClient(ws_url="ws://fake", http_url="http://fake")

    await asyncio.wait_for(
        client.receive_events(on_event=_on_event, backoff_seconds=0.01, stop=stop),
        timeout=5,
    )

    assert seen == [8]


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


@pytest.mark.asyncio
async def test_outbound_after_text_attachment_goes_after_the_body():
    """after_text 附件（表情包）：正文先发、图后发；失败不降级成链接。"""
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
            "text": "好耶",
            "external_key": "qq:group:42",
            "attachments": [
                {
                    "kind": "image",
                    "name": "happy.gif",
                    "mime_type": "image/gif",
                    "size": 8,
                    "url": "/api/ceo/media/original?token=t4",
                    "after_text": True,
                }
            ],
        },
    )
    assert onebot.group[0] == ("42", "好耶")
    assert onebot.group[1][1].startswith("[CQ:image,file=base64://")
    assert len(onebot.group) == 2


@pytest.mark.asyncio
async def test_outbound_sticker_only_reply_sends_the_image_alone():
    """纯表情回合：正文为空时不补发空文本消息。"""
    dispatcher, g3ku, onebot = _dispatcher()
    await dispatcher.handle_onebot_event(
        {"post_type": "message", "message_type": "private", "user_id": 7, "message": "hi"}
    )
    session_id = g3ku.sessions["qq:dm:7"]
    await dispatcher._handle_g3ku_event(
        session_id,
        {
            "type": "reply.final",
            "text": "",
            "attachments": [
                {
                    "kind": "image",
                    "name": "smug.png",
                    "mime_type": "image/png",
                    "size": 8,
                    "url": "/api/ceo/media/original?token=t5",
                    "after_text": True,
                }
            ],
        },
    )
    assert len(onebot.private) == 1
    assert onebot.private[0][1].startswith("[CQ:image,file=base64://")


def test_media_source_policy_blocks_never_a_donor_address_families():
    from qq_onebot_bridge.onebot import MediaSourcePolicy

    policy = MediaSourcePolicy(donor_http_url="http://127.0.0.1:3000")

    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://fe80::1/a.png",
        "http://[::1]:18790/api/tasks",
        "http://255.255.255.255/x",
        "file:///etc/passwd",
        "not-a-url",
        "http://does-not-exist.invalid/a.png",
    ):
        allowed, _reason = policy.allows(url)
        assert allowed is False, url


def test_media_source_policy_allows_the_configured_donor_and_public_hosts():
    """donor 就是配置里那台：它已经表达了意图，不该再抄一遍白名单。"""
    from qq_onebot_bridge.onebot import MediaSourcePolicy

    policy = MediaSourcePolicy(donor_http_url="http://127.0.0.1:3000")

    assert policy.allows("http://127.0.0.1:3000/media/a.png")[0] is True
    assert policy.allows("https://multimedia.nt.qq.com.cn/a.png")[0] is True


def test_media_source_policy_matches_sockets_not_host_names(monkeypatch):
    """donor 写作 127.0.0.1 时，双栈的 `localhost` 多出一枚 ::1 端点，不算同一个 socket——
    判否并把出路写进理由。解析在测试里钉死，免得结论随机器的地址族配置漂。"""
    from qq_onebot_bridge import onebot as onebot_module
    from qq_onebot_bridge.onebot import MediaSourcePolicy

    def _fake_getaddrinfo(host, port, **kwargs):
        if host == "localhost":
            return [(6, 1, 6, "", ("::1", port)), (2, 1, 6, "", ("127.0.0.1", port))]
        return [(2, 1, 6, "", (host, port))]

    monkeypatch.setattr(onebot_module.socket, "getaddrinfo", _fake_getaddrinfo)
    policy = MediaSourcePolicy(donor_http_url="http://127.0.0.1:3000")

    allowed, reason = policy.allows("http://localhost:3000/media/a.png")
    assert allowed is False
    assert "allowed_media_hosts" in reason

    listed = MediaSourcePolicy(donor_http_url="http://127.0.0.1:3000", allowed_hosts=["localhost:3000"])
    assert listed.allows("http://localhost:3000/media/a.png")[0] is True


def test_media_source_policy_denies_unlisted_private_targets_and_names_the_escape_hatch():
    """同机另一台私有服务默认拒；理由里要写清楚怎么放行。"""
    from qq_onebot_bridge.onebot import MediaSourcePolicy

    policy = MediaSourcePolicy(donor_http_url="http://127.0.0.1:3000")

    allowed, reason = policy.allows("http://127.0.0.1:18790/api/llm/configs")
    assert allowed is False
    assert "allowed_media_hosts" in reason

    listed = MediaSourcePolicy(
        donor_http_url="http://127.0.0.1:3000",
        allowed_hosts=["127.0.0.1:18790", "media.lan"],
    )
    assert listed.allows("http://127.0.0.1:18790/media/a.png")[0] is True
    assert listed.allows("http://127.0.0.1:18791/a.png")[0] is False


@pytest.mark.asyncio
async def test_download_bytes_refuses_a_metadata_url_without_fetching(monkeypatch):
    from qq_onebot_bridge.onebot import OnebotClient

    client = OnebotClient(ws_url="ws://fake", http_url="http://fake")
    attempted: list[str] = []

    def _fake_stream(method, url, **kwargs):
        attempted.append(str(url))
        raise AssertionError("guard must reject before the transport")

    monkeypatch.setattr(client._http, "stream", _fake_stream)

    result = await client.download_bytes("http://169.254.169.254/latest/meta-data/iam/", max_bytes=1024)

    assert result is None
    assert attempted == []
    await client.close()


@pytest.mark.asyncio
async def test_download_bytes_refuses_the_local_admin_surface(monkeypatch):
    """本机管理面对回环无鉴权：借桥读它就是这条闸门要关掉的口子。"""
    from qq_onebot_bridge.onebot import OnebotClient

    client = OnebotClient(ws_url="ws://fake", http_url="http://127.0.0.1:3000")
    attempted: list[str] = []

    def _fake_stream(method, url, **kwargs):
        attempted.append(str(url))
        raise AssertionError("guard must reject before the transport")

    monkeypatch.setattr(client._http, "stream", _fake_stream)

    result = await client.download_bytes("http://127.0.0.1:18790/api/llm/configs", max_bytes=1024)

    assert result is None
    assert attempted == []
    await client.close()
