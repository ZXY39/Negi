"""Tests for the CEO media middle layer (thumbnail staging + signed original viewer)."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import ceo_media


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(ceo_media.router, prefix="/api")
    return TestClient(app)


def _write_noisy_png(path: Path, *, size=(1600, 1200), seed=7) -> None:
    from PIL import Image

    rnd = random.Random(seed)
    width, height = size
    img = Image.new("RGB", (width, height))
    img.putdata(
        [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(width * height)]
    )
    img.save(path)


def _viewer_token(url: str) -> str:
    return url.split("token=", 1)[1]


def test_rewrite_stages_thumbnail_and_viewer_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "media" / "big.png"
    source.parent.mkdir()
    _write_noisy_png(source)
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")

    content = (
        f"![big pic]({source})\n\n"
        f"[report]({note})\n\n"
        "[ext](https://example.com/x.png)\n\n"
        f"![missing]({tmp_path / 'nope.png'})"
    )
    rewritten = ceo_media.rewrite_assistant_media_content("sess_a", content)

    image_match = re.search(r'!\[big pic\]\(([^")]+) "([^"]+)"\)', rewritten)
    assert image_match, rewritten
    thumb = Path(image_match.group(1))
    viewer = image_match.group(2)
    assert thumb.exists()
    assert "inline_media" in str(thumb)
    assert thumb.stat().st_size <= ceo_media.THUMBNAIL_MAX_BYTES
    assert viewer.startswith(ceo_media.VIEWER_ROUTE + "?token=")

    link_match = re.search(r"\[report\]\(([^)]+)\)", rewritten)
    assert link_match and link_match.group(1).startswith(ceo_media.VIEWER_ROUTE + "?token=")
    assert "[ext](https://example.com/x.png)" in rewritten
    assert f"![missing]({tmp_path / 'nope.png'})" in rewritten


def test_thumbnail_idempotent_and_invalidated_on_change(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "photo.png"
    _write_noisy_png(source, size=(800, 600), seed=3)
    content = f"![p]({source})"

    first = ceo_media.rewrite_assistant_media_content("sess_b", content)
    second = ceo_media.rewrite_assistant_media_content("sess_b", content)
    first_thumb = re.search(r'!\[p\]\(([^")]+)', first).group(1)
    assert first_thumb in second

    _write_noisy_png(source, size=(800, 600), seed=4)
    third = ceo_media.rewrite_assistant_media_content("sess_b", content)
    third_thumb = re.search(r'!\[p\]\(([^")]+)', third).group(1)
    assert third_thumb != first_thumb


def test_original_endpoint_inline_attachment_and_tamper(monkeypatch, tmp_path, client):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "pic.png"
    _write_noisy_png(image, size=(64, 64), seed=1)
    svg = tmp_path / "evil.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")

    good = client.get(
        "/api/ceo/media/original",
        params={"token": _viewer_token(ceo_media.original_view_url(image))},
    )
    assert good.status_code == 200
    assert "inline" in good.headers.get("content-disposition", "")
    assert good.content == image.read_bytes()

    risky = client.get(
        "/api/ceo/media/original",
        params={"token": _viewer_token(ceo_media.original_view_url(svg))},
    )
    assert risky.status_code == 200
    assert "attachment" in risky.headers.get("content-disposition", "")

    token = _viewer_token(ceo_media.original_view_url(image))
    tampered = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
    assert client.get("/api/ceo/media/original", params={"token": tampered}).status_code == 403


def test_original_endpoint_404_after_delete(monkeypatch, tmp_path, client):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "gone.png"
    _write_noisy_png(image, size=(64, 64), seed=2)
    token = _viewer_token(ceo_media.original_view_url(image))
    image.unlink()
    assert client.get("/api/ceo/media/original", params={"token": token}).status_code == 404


def test_extract_attachments_removes_local_references_from_the_body(monkeypatch, tmp_path):
    """被提取成附件的引用整段从正文消失：文件本体由附件送达，卡片上就带名字，
    正文再留一行标签等于在气泡底下印一串文件名。"""
    monkeypatch.chdir(tmp_path)
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"doc-bytes")
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG fake")

    content = f"已完成：[日报]({doc}) 和 ![]({chart}) 另见 [外部](https://example.com/a.png)"
    text, attachments = ceo_media.extract_local_media_attachments(
        content, allowed_roots=(tmp_path,)
    )

    assert "日报" not in text
    assert "chart.png" not in text and "report.docx" not in text
    assert f"({doc})" not in text and f"({chart})" not in text
    # 非本地的引用照旧原样留着（它不成为附件，由签名改写兜底）。
    assert "[外部](https://example.com/a.png)" in text
    assert text.startswith("已完成：") and text.endswith("另见 [外部](https://example.com/a.png)")
    by_name = {item["name"]: item for item in attachments}
    assert set(by_name) == {"report.docx", "chart.png"}
    assert by_name["report.docx"]["mime_type"].endswith("wordprocessingml.document")
    assert by_name["report.docx"]["size"] == len(b"doc-bytes")
    # kind 是渠道桥决定"发图片还是发文件"的唯一依据
    assert by_name["report.docx"]["kind"] == "file"
    assert by_name["chart.png"]["kind"] == "image"
    for item in attachments:
        assert item["url"].startswith(ceo_media.VIEWER_ROUTE + "?token=")


def test_extract_attachments_records_the_reference_form_on_the_descriptor(monkeypatch, tmp_path):
    """`![...]` 与 `[...](path)` 是模型唯一写下「放成媒体气泡」还是「把文件给我」的地方，
    提取时按形态打在描述符上：渠道桥据此分档，`kind` 只有 image/file 两值担不起这个区分。"""
    monkeypatch.chdir(tmp_path)
    voice = tmp_path / "晚安.mp3"
    voice.write_bytes(b"ID3\x04fake-mp3")

    inline_text, inline_atts = ceo_media.extract_local_media_attachments(
        f"![晚安]({voice})", allowed_roots=(tmp_path,)
    )
    file_text, file_atts = ceo_media.extract_local_media_attachments(
        f"[晚安]({voice})", allowed_roots=(tmp_path,)
    )

    assert inline_text == "" and file_text == ""
    assert inline_atts[0]["inline_media"] is True
    # 链接形态不带这个键（缺省即 False），同一份文件两次引用不会都成为附件。
    assert "inline_media" not in file_atts[0]
    assert file_atts[0]["name"] == "晚安.mp3"


def test_extract_attachments_leaves_no_blank_line_where_the_reference_stood(monkeypatch, tmp_path):
    """引用独占一行时摘掉不能留下空行；写在句中时左边那枚空格跟着一起吞。"""
    monkeypatch.chdir(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG fake")

    own_line, atts = ceo_media.extract_local_media_attachments(
        f"好了，给你：\n\n![结果]({shot})", allowed_roots=(tmp_path,)
    )
    mid_line, _ = ceo_media.extract_local_media_attachments(
        f"看图 ![结果]({shot}) 清楚吗", allowed_roots=(tmp_path,)
    )

    assert own_line == "好了，给你："
    # 吞掉的是引用左边那枚空格，右边那枚留下，句中读起来仍是一个空格。
    assert mid_line == "看图 清楚吗"
    assert len(atts) == 1


def test_extract_attachments_skips_missing_and_dedupes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    doc = tmp_path / "a.txt"
    doc.write_text("x", encoding="utf-8")

    content = f"[一]({doc}) [二]({doc}) [缺]({tmp_path / 'nope.txt'})"
    text, attachments = ceo_media.extract_local_media_attachments(
        content, allowed_roots=(tmp_path,)
    )

    assert len(attachments) == 1
    # 重复引用与不存在的文件保留原样（由签名改写兜底）
    assert f"[二]({doc})" in text and f"[缺]({tmp_path / 'nope.txt'})" in text


def test_extract_attachments_has_no_per_reply_count_cap(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    parts = []
    for index in range(7):
        file = tmp_path / f"f{index}.txt"
        file.write_text(str(index), encoding="utf-8")
        parts.append(f"[f{index}]({file})")

    text, attachments = ceo_media.extract_local_media_attachments(
        " ".join(parts), allowed_roots=(tmp_path,)
    )

    assert len(attachments) == 7
    assert not any(f"[f{index}](" in text for index in range(7))


def test_extract_attachments_rejects_paths_outside_allowed_roots(monkeypatch, tmp_path):
    """渠道白名单：越界路径既不转附件，也不能在签名改写里变成可下载链接。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    allowed = workspace / "ok.png"
    allowed.write_bytes(b"\x89PNG fake")
    outside = tmp_path / "secret.txt"
    outside.write_text("token", encoding="utf-8")

    content = f"![]({allowed}) [泄露]({outside})"
    text, attachments = ceo_media.extract_local_media_attachments(
        content, allowed_roots=(workspace,)
    )

    assert [item["name"] for item in attachments] == ["ok.png"]
    assert f"[泄露]({outside})" in text

    signed = ceo_media.rewrite_media_links_signed(text, allowed_roots=(workspace,))
    assert ceo_media.VIEWER_ROUTE not in signed
    assert str(outside) in signed


def test_extract_attachments_accepts_file_url_form_of_an_audio_target(monkeypatch, tmp_path):
    """`file:///` 形态的本地引用要能被抽成附件：模型抄一条本地文件链接时给的就是这个形状，
    以前它死在 scheme 守卫上，于是渠道收到的是原样的 markdown 文本而不是文件消息。
    带空格的路径以 %20 抵达，也要一起解开。"""
    monkeypatch.chdir(tmp_path)
    voice = tmp_path / "voice note.mp3"
    voice.write_bytes(b"ID3\x04fake-mp3")
    target = "file:///" + voice.as_posix().replace(" ", "%20")

    text, attachments = ceo_media.extract_local_media_attachments(
        f"![早间问候]({target})", allowed_roots=(tmp_path,)
    )

    assert len(attachments) == 1
    assert attachments[0]["name"] == "voice note.mp3"
    assert attachments[0]["mime_type"].startswith("audio/")
    # 标记整段从正文摘掉，投递由附件承担（正文一个字都不留）。
    assert text == ""
    assert "file:///" not in text and "早间问候" not in text


def test_extract_attachments_accepts_paths_with_spaces(tmp_path):
    """Windows 路径带空格是常态；空格不能把 markdown 目标截断，否则整条引用退化成文件名。"""
    workspace = tmp_path / "Negi Data"
    workspace.mkdir()
    gif = workspace / "from assistant.gif"
    gif.write_bytes(b"GIF89a-fake-animated")

    text, attachments = ceo_media.extract_local_media_attachments(
        f"![问候]({gif})", allowed_roots=(workspace,)
    )
    assert text == ""
    assert [item["name"] for item in attachments] == ["from assistant.gif"]

    # 合法写法里的尖括号包目标，以及签名标题槽，都不能被当成路径的一部分。
    text, attachments = ceo_media.extract_local_media_attachments(
        f'![问候](<{gif}> "{ceo_media.VIEWER_ROUTE}?token=x")',
        allowed_roots=(workspace,),
    )
    assert text == ""
    assert [item["name"] for item in attachments] == ["from assistant.gif"]


def test_extract_attachments_still_rejects_file_url_outside_allowed_roots(monkeypatch, tmp_path):
    """归一化只换形态，不放宽闸门：`file:///` 指到工作区外仍然既不转附件也不发链接。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(b"ID3\x04fake-mp3")

    text, attachments = ceo_media.extract_local_media_attachments(
        f"![外](file:///{outside.as_posix()})", allowed_roots=(workspace,)
    )

    assert attachments == []
    assert "file:///" in text

    signed = ceo_media.rewrite_media_links_signed(text, allowed_roots=(workspace,))
    assert ceo_media.VIEWER_ROUTE not in signed
    # 越界的 file:// 引用原样留着：既不变成附件，也不变成可下载链接。
    assert "file:///" in signed


def test_channel_attachment_roots_are_workspace_only():
    roots = ceo_media.channel_attachment_roots()
    assert roots == (ceo_media.workspace_path(),)


def test_channel_attachment_roots_include_the_session_workspace(monkeypatch, tmp_path):
    from g3ku.runtime.frontdoor import session_workspace as session_workspace_module

    session_dir = tmp_path / "session-workspace"
    monkeypatch.setattr(
        session_workspace_module,
        "ceo_session_workspace_dir",
        lambda session_key, create=False: str(session_dir),
    )
    roots = ceo_media.channel_attachment_roots("ext:qq-official:qq:group:g1")
    assert ceo_media.workspace_path() in roots
    assert session_dir.resolve() in roots


def test_session_workspace_media_is_extracted_for_its_own_channel(monkeypatch, tmp_path):
    """会话工作目录里的表情包必须能作为附件发出；QQ 只显示文件名就是丢在这一步。"""
    from g3ku.runtime.frontdoor import session_workspace as session_workspace_module

    session_dir = tmp_path / "session-workspace"
    session_dir.mkdir()
    gif = session_dir / "wave.gif"
    gif.write_bytes(b"GIF89a-fake-animated")
    monkeypatch.setattr(
        session_workspace_module,
        "ceo_session_workspace_dir",
        lambda session_key, create=False: str(session_dir),
    )
    text = f"![wave]({gif})"
    cleaned, attachments = ceo_media.extract_local_media_attachments(
        text, allowed_roots=ceo_media.channel_attachment_roots("ext:qq-official:qq:group:g1")
    )
    assert cleaned == ""
    assert len(attachments) == 1
    assert attachments[0]["name"] == "wave.gif"
    assert attachments[0]["kind"] == "image"
    assert attachments[0]["inline_media"] is True
    # 没有会话键时白名单仍只有全局工作区，越界路径不放行。
    _, denied = ceo_media.extract_local_media_attachments(
        text, allowed_roots=ceo_media.channel_attachment_roots()
    )
    assert denied == []


def test_animated_media_keeps_bytes_and_uses_a_local_src(monkeypatch, tmp_path):
    """gif/webp 不被压成静态 JPEG；src 必须是本地副本（前端只渲染本地 src）。"""
    monkeypatch.chdir(tmp_path)
    payload = b"GIF89a-fake-animated"
    gif = tmp_path / "wave.gif"
    gif.write_bytes(payload)

    rewritten = ceo_media.rewrite_assistant_media_content("sess_anim", f"![a]({gif})")

    match = re.search(r'!\[a\]\(([^)"]+)\s+"([^"]+)"\)', rewritten)
    assert match, rewritten
    staged, viewer = Path(match.group(1)), match.group(2)
    assert staged.exists() and staged.suffix == ".gif"
    # 原样副本：没有被 _build_thumbnail 转成首帧 JPEG
    assert staged.read_bytes() == payload
    assert viewer.startswith(ceo_media.VIEWER_ROUTE + "?token=")


def test_webp_is_classified_as_an_image_descriptor(tmp_path):
    """`.webp` 在部分 Windows 上没有 mimetypes 映射（实测 guess_type → (None, None)）；
    落到 application/octet-stream 会让渠道桥把它当文件发出去（实盘 005.webp 走了
    file_type=4）。栅格后缀必须由本模块的映射兜住。"""
    sticker = tmp_path / "005.webp"
    sticker.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")

    descriptor = ceo_media.build_attachment_descriptor(sticker)

    assert descriptor["mime_type"] == "image/webp"
    assert descriptor["kind"] == "image"

