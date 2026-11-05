# -*- coding: utf-8 -*-
"""Bilibili capability pack for Negi (native tool, no DSH).

提供 B 站只读能力（热榜/排行/搜索/视频详情/UP 主信息与投稿）、登录后能力
（字幕/评论/收藏夹/稍后再看）、扫码登录，以及「刷一轮 B站」日报工作流。

发送类动作（评论/私信/@）默认关闭：需要先通过 `bili config` 显式开启并设置
单日配额，发送前再次核验开关与配额，发送后把回执（rpid / msg_key）记入台账。

与 dsh-bili-scroll 的能力思路同源（bilibili-api-python），但完全脱离 DSH：
不依赖 dsh 内核 / cordis / bundle patch / DSH profile，也不依赖 @deepseek-ai/* 包。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool

# ============ 常量 ============

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_PASSPORT_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate?source=main-fe-header"
_PASSPORT_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={key}&source=main-fe-header"
_SPI = "https://api.bilibili.com/x/frontend/finger/spi"
_HTTP_TIMEOUT = 25

# ============ 投稿上传/列表/删稿（member 侧 web 接口，2026-11 实测可用） ============

_PREUPLOAD_URL = "https://member.bilibili.com/preupload"
_ARCHIVE_ADD_URL = "https://member.bilibili.com/x/vu/web/add/v3"
_ARCHIVE_LIST_URL = "https://member.bilibili.com/x/web/archives"
_ARCHIVE_DELETE_URL = "https://member.bilibili.com/x/web/archive/delete"
_COVER_UP_URL = "https://member.bilibili.com/x/vu/web/cover/up"
# 允许投稿的视频后缀（白名单，防止误投脚本/压缩包）
_ALLOWED_VIDEO_EXT = {".mp4", ".flv", ".avi", ".wmv", ".mov", ".webm", ".mkv"}
_ALLOWED_COVER_EXT = {".png", ".jpg", ".jpeg", ".webp"}

_ORDER_MAP = {
    "totalrank": "TOTALRANK",
    "click": "CLICK",
    "pubdate": "PUBDATE",
    "dm": "DM",
}

_RANK_TYPE_MAP = {
    "all": "All", "original": "Original", "rookie": "Rookie",
    "douga": "Douga", "music": "Music", "dance": "Dance", "game": "Game",
    "knowledge": "Knowledge", "technology": "Technology", "sports": "Sports",
    "car": "Car", "life": "Life", "food": "Food", "animal": "Animal",
    "fashion": "Fashion", "ent": "Ent", "cinephile": "Cinephile",
}

_ACTIONS = [
    "search", "hot", "rank", "video_info", "user_info", "user_videos",
    "login", "login_check", "check_credential", "status",
    "subtitle", "comments", "danmaku", "favorite_lists", "favorite_content", "watch_later",
    "following", "followers", "like",
    "reply", "send_message", "config", "daily_report",
    "my_videos", "publish_video", "delete_video",
]

# ============ 目录与状态 ============


def _state_dir() -> Path:
    """私有状态目录：<data_root>/.g3ku/bili（凭据、开关、台账、二维码）。"""
    from g3ku.deployment.data_root import data_g3ku_path

    return data_g3ku_path("bili", create=True)


def _output_dir() -> Path:
    """正式报告默认落盘目录：<data_root>/output。"""
    from g3ku.deployment.data_root import data_work_path

    return data_work_path("output", create=True)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ============ 文件读写（原子写） ============


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


# ============ 凭证：读写 + 权限收紧 ============


def _cred_file() -> Path:
    return _state_dir() / "bili_credential.json"


def _current_user_sid() -> str | None:
    try:
        whoami = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
        out = subprocess.run(
            [str(whoami), "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
        m = re.search(r'"(S-1-[\d-]+)"', out)
        return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None


def _harden_perms(file: Path) -> bool:
    """Windows: icacls 去掉继承、只留当前用户；非 Windows: chmod 600。

    按 SID 授权（*S-1-...），避免机器账户解析问题；收紧后自检，读不回就恢复继承。
    """
    try:
        if os.name == "nt":
            sid = _current_user_sid()
            who = ("*" + sid) if sid else os.environ.get("USERNAME", "")
            subprocess.run(
                ["icacls", str(file), "/inheritance:r", "/grant:r", who + ":F"],
                capture_output=True, timeout=15, check=False,
            )
            try:
                with open(file, encoding="utf-8"):
                    pass
            except OSError:
                subprocess.run(["icacls", str(file), "/reset"], capture_output=True, timeout=15)
                return False
        else:
            file.chmod(0o600)
        return True
    except Exception:  # noqa: BLE001
        return False


def _save_credential(cred: dict[str, str]) -> Path:
    file = _cred_file()
    _atomic_write_json(
        file,
        {
            "sessdata": cred.get("sessdata") or "",
            "bili_jct": cred.get("bili_jct") or "",
            "buvid3": cred.get("buvid3") or "",
            "dedeuserid": cred.get("dedeuserid") or "",
        },
    )
    _harden_perms(file)
    return file


def _load_credential() -> dict[str, str]:
    data = _read_json(_cred_file(), {})
    if not isinstance(data, dict):
        return {}
    return {
        "sessdata": str(data.get("sessdata") or ""),
        "bili_jct": str(data.get("bili_jct") or ""),
        "buvid3": str(data.get("buvid3") or ""),
        "dedeuserid": str(data.get("dedeuserid") or ""),
    }


def _credential_obj(cred: dict[str, str] | None = None):
    """把凭证转成 bilibili_api 的 Credential；无凭证返回 None。"""
    from bilibili_api import Credential

    data = cred if cred is not None else _load_credential()
    if not data or not data.get("sessdata"):
        return None
    try:
        return Credential(
            sessdata=data.get("sessdata"),
            bili_jct=data.get("bili_jct"),
            buvid3=data.get("buvid3") or "",
            dedeuserid=data.get("dedeuserid") or "",
        )
    except Exception:  # noqa: BLE001
        return None


def _masked(cred: dict[str, str]) -> dict[str, Any]:
    """凭证信息只回显 uid 与是否存在的状态，绝不泄露 cookie/token 内容。"""
    return {
        "has_credential": bool(cred and cred.get("sessdata")),
        "uid": cred.get("dedeuserid") or "",
        "cookie_fields_present": [k for k in ("sessdata", "bili_jct", "buvid3", "dedeuserid") if cred.get(k)],
    }


# ============ 发送闸门：开关 + 单日配额 + 台账 ============


def _send_conf() -> dict[str, Any]:
    return _read_json(_state_dir() / "send_config.json", {"enabled": False, "daily_quota": 0})


def _send_usage() -> dict[str, Any]:
    return _read_json(_state_dir() / "send_usage.json", {"date": _today(), "count": 0})


def _quota_check() -> tuple[bool, str]:
    conf = _send_conf()
    if not bool(conf.get("enabled")):
        return False, "发送类动作默认关闭。请先执行 bili config(send_enabled=true) 显式开启。"
    quota = int(conf.get("daily_quota") or 0)
    usage = _send_usage()
    if usage.get("date") != _today():
        usage = {"date": _today(), "count": 0}
    if quota <= 0:
        return False, "单日配额为 0，未开启发送。请用 bili config(daily_quota=N) 设置今日配额。"
    if int(usage.get("count") or 0) >= quota:
        return False, f"今日发送配额已用完（{int(usage.get('count') or 0)}/{quota}）。"
    return True, ""


def _bump_usage() -> int:
    conf = _send_conf()
    usage = _send_usage()
    if usage.get("date") != _today():
        usage = {"date": _today(), "count": 0}
    usage["count"] = int(usage.get("count") or 0) + 1
    _atomic_write_json(_state_dir() / "send_usage.json", usage)
    return int(conf.get("daily_quota") or 0)


def _ledger_append(text: str, bvid: str, receipt: str = "", kind: str = "comment") -> None:
    try:
        p = _state_dir() / "comments.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        line = f"- {_today()} {datetime.now().strftime('%H:%M')} [{kind}] {bvid} receipt={receipt or '??'} · {' '.join(str(text).split())}"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ============ 扫码登录（原生 passport API，规避 login_v2 空字段问题） ============


def _pick_cookies(set_cookie_list: list[str]) -> dict[str, str]:
    want = {
        "SESSDATA": "sessdata", "bili_jct": "bili_jct",
        "DedeUserID": "dedeuserid", "buvid3": "buvid3",
    }
    jar: dict[str, str] = {}
    for raw in set_cookie_list or []:
        first = str(raw).split(";")[0]
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        name = name.strip()
        if name in want and value.strip():
            jar[want[name]] = value.strip()
    return jar


async def _ensure_buvid3(session: Any, jar: dict[str, str]) -> dict[str, str]:
    if jar.get("buvid3"):
        return jar
    try:
        import aiohttp

        if session is None:
            async with aiohttp.ClientSession() as s:
                async with s.get(_SPI, headers={"User-Agent": _UA}, timeout=_HTTP_TIMEOUT) as resp:
                    j = await resp.json()
        else:
            async with session.get(_SPI, headers={"User-Agent": _UA}, timeout=_HTTP_TIMEOUT) as resp:
                j = await resp.json()
        v = (j.get("data") or {}).get("b_3")
        if v:
            jar["buvid3"] = v
    except Exception:  # noqa: BLE001
        pass
    return jar


async def _qr_generate() -> dict[str, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(_PASSPORT_GEN, headers={"User-Agent": _UA}, timeout=_HTTP_TIMEOUT) as resp:
            j = await resp.json()
    data = j.get("data") or {}
    if not data.get("qrcode_key") or not data.get("url"):
        raise RuntimeError(f"生成二维码失败：{j.get('message') or '返回里没有 qrcode_key'}")
    return {"key": data["qrcode_key"], "url": data["url"]}


async def _qr_poll(key: str) -> dict[str, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(
            _PASSPORT_POLL.format(key=key),
            headers={"User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
        ) as resp:
            set_cookies = resp.headers.getall("Set-Cookie", []) if hasattr(resp.headers, "getall") else []
            body = {}
            try:
                body = await resp.json()
            except Exception:  # noqa: BLE001
                pass
    code = (body.get("data") or {}).get("code")
    if code == 0:
        jar = await _ensure_buvid3(None, _pick_cookies(set_cookies))
        return {"state": "ok", "code": code, "credential": jar}
    state_map = {86101: "waiting", 86090: "scanned", 86038: "expired"}
    return {"state": state_map.get(code, "unknown"), "code": code, "message": body.get("message") or ""}


def _render_qr_png(url: str, out_path: Path) -> bool:
    """用 qrcode + Pillow 渲染二维码 PNG（qrcode_terminal 不参与此路径）。"""
    try:
        import qrcode

        img = qrcode.make(url)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False


# ============ 数据动作实现 ============


def _clean_title(title: str) -> str:
    return str(title or "").replace('<em class="keyword">', "").replace("</em>", "")


async def _action_search(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import search

    keyword = str(kwargs.get("keyword") or "").strip()
    if not keyword:
        return {"ok": False, "error": "keyword 不能为空"}
    num = max(1, min(int(kwargs.get("num") or 10), 50))
    order_key = str(kwargs.get("order") or "totalrank")
    page = max(1, int(kwargs.get("page") or 1))
    order_enum = getattr(search.OrderVideo, _ORDER_MAP.get(order_key, "TOTALRANK"))
    result = await search.search_by_type(
        keyword=keyword,
        search_type=search.SearchObjectType.VIDEO,
        page=page,
        order_type=order_enum,
    )
    videos = []
    for item in (result.get("result") or [])[:num]:
        videos.append(
            {
                "bvid": item.get("bvid", ""),
                "aid": item.get("aid", 0),
                "title": _clean_title(item.get("title", "")),
                "author": item.get("author", ""),
                "play": item.get("play", 0),
                "review": item.get("review", 0),
                "danmaku": item.get("video_review", 0),
                "duration": item.get("duration", ""),
                "pubdate": item.get("pubdate", 0),
                "description": (item.get("description", "") or "")[:200],
            }
        )
    return {"ok": True, "keyword": keyword, "count": len(videos), "videos": videos}


async def _action_hot(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import hot

    pn = max(1, int(kwargs.get("pn") or 1))
    ps = max(1, min(int(kwargs.get("ps") or 20), 50))
    result = await hot.get_hot_videos(pn=pn, ps=ps)
    videos = []
    for item in (result.get("list") or []):
        stat = item.get("stat") or {}
        videos.append(
            {
                "bvid": item.get("bvid", ""),
                "title": item.get("title", ""),
                "author": (item.get("owner") or {}).get("name", ""),
                "play": stat.get("view", 0),
                "like": stat.get("like", 0),
                "danmaku": stat.get("danmaku", 0),
                "reply": stat.get("reply", 0),
                "desc": (item.get("desc", "") or "")[:100],
                "duration": item.get("duration", 0),
                "tname": item.get("tname", ""),
            }
        )
    return {"ok": True, "page": pn, "count": len(videos), "videos": videos}


async def _action_rank(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import rank

    category = str(kwargs.get("category") or "all").lower()
    day = int(kwargs.get("day") or 3)
    rank_type = getattr(rank.RankType, _RANK_TYPE_MAP.get(category, "All"))
    rank_day = rank.RankDayType.THREE_DAY if day != 7 else rank.RankDayType.WEEK
    result = await rank.get_rank(type_=rank_type, day=rank_day)
    videos = []
    for item in (result.get("list") or []):
        stat = item.get("stat") or {}
        videos.append(
            {
                "bvid": item.get("bvid", ""),
                "title": item.get("title", ""),
                "author": (item.get("owner") or {}).get("name", ""),
                "play": stat.get("view", 0),
                "like": stat.get("like", 0),
                "coin": stat.get("coin", 0),
                "score": item.get("score", 0),
                "tname": item.get("tname", ""),
            }
        )
    return {"ok": True, "category": category, "day": day, "count": len(videos), "videos": videos}


async def _action_video_info(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import video

    bvid = str(kwargs.get("bvid") or "").strip()
    if not bvid:
        return {"ok": False, "error": "bvid 不能为空"}
    cred = _credential_obj()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    stat = info.get("stat") or {}
    return {
        "ok": True,
        "bvid": info.get("bvid"),
        "aid": info.get("aid"),
        "title": info.get("title"),
        "description": info.get("desc"),
        "author": (info.get("owner") or {}).get("name"),
        "duration": info.get("duration"),
        "pages": len(info.get("pages", [])),
        "tags": [t.get("tag_name") for t in info.get("tag", []) if t.get("tag_name")],
        "stat": {
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "reply": stat.get("reply", 0),
            "favorite": stat.get("favorite", 0),
            "coin": stat.get("coin", 0),
            "like": stat.get("like", 0),
            "share": stat.get("share", 0),
        },
    }


async def _action_user_info(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import user

    uid = int(kwargs.get("uid") or 0)
    if uid <= 0:
        return {"ok": False, "error": "uid 必须为正整数"}
    cred = _credential_obj()
    u = user.User(uid=uid, credential=cred)
    info = await u.get_user_info()
    up_stat, relation = {}, {}
    try:
        up_stat = await u.get_up_stat()
    except Exception:  # noqa: BLE001
        pass
    try:
        relation = await u.get_relation_info()
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "uid": uid,
        "name": info.get("name", ""),
        "sign": info.get("sign", ""),
        "level": info.get("level", 0),
        "fans": relation.get("follower", info.get("follower", 0)),
        "following": relation.get("following", info.get("following", 0)),
        "likes": up_stat.get("likes", 0),
        "archive_view": (up_stat.get("archive") or {}).get("view", 0),
    }


async def _action_user_videos(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import user

    uid = int(kwargs.get("uid") or 0)
    if uid <= 0:
        return {"ok": False, "error": "uid 必须为正整数"}
    cred = _credential_obj()
    pn = max(1, int(kwargs.get("pn") or 1))
    ps = max(1, min(int(kwargs.get("ps") or 30), 50))
    order = str(kwargs.get("order") or "pubdate")
    keyword = str(kwargs.get("keyword") or "")
    order_map = {"pubdate": user.VideoOrder.PUBDATE, "click": user.VideoOrder.VIEW, "stow": user.VideoOrder.FAVORITE}
    order_enum = order_map.get(order, user.VideoOrder.PUBDATE)
    u = user.User(uid=uid, credential=cred)
    result = await u.get_videos(pn=pn, ps=ps, order=order_enum, keyword=keyword)
    videos = []
    for item in (result.get("list") or {}).get("vlist", []):
        videos.append(
            {
                "bvid": item.get("bvid", ""),
                "title": item.get("title", ""),
                "play": item.get("play", 0),
                "comment": item.get("comment", 0),
                "created": item.get("created", 0),
                "length": item.get("length", ""),
                "description": (item.get("description", "") or "")[:120],
            }
        )
    return {"ok": True, "uid": uid, "count": len(videos), "videos": videos}


def _require_cred() -> tuple[dict[str, str] | None, str]:
    cred = _load_credential()
    if not cred or not cred.get("sessdata"):
        return None, "未登录。登录后可用能力（字幕/评论/收藏夹/稍后再看/发送）需要凭证，请先执行 bili login 扫码。"
    return cred, ""


async def _action_subtitle(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import video

    bvid = str(kwargs.get("bvid") or "").strip()
    if not bvid:
        return {"ok": False, "error": "bvid 不能为空"}
    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "error": err}
    v = video.Video(bvid=bvid, credential=_credential_obj(cred))
    info = await v.get_info()
    cid = info.get("cid", 0)
    if not cid and info.get("pages"):
        cid = info["pages"][0].get("cid", 0)
    if not cid:
        return {"ok": False, "error": "无法获取 cid"}
    subtitle_list = await v.get_subtitle(cid=cid)
    subtitles = subtitle_list.get("subtitles") or []
    if not subtitles:
        return {"ok": True, "bvid": bvid, "message": "该视频没有字幕", "text": ""}
    target = None
    for s in subtitles:
        if s.get("lan") in ("ai-zh", "zh-CN", "zh"):
            target = s
            break
    if not target:
        target = subtitles[0]
    import aiohttp

    sub_url = target.get("subtitle_url", "")
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    async with aiohttp.ClientSession() as session:
        async with session.get(sub_url, headers={"User-Agent": _UA}, timeout=_HTTP_TIMEOUT) as resp:
            sub_data = await resp.json()
    texts = [item.get("content", "") for item in (sub_data.get("body") or [])]
    return {
        "ok": True,
        "bvid": bvid,
        "title": info.get("title", ""),
        "language": target.get("lan_doc", ""),
        "segments": len(texts),
        "text": "\n".join(texts),
    }


async def _action_comments(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import comment, video

    bvid = str(kwargs.get("bvid") or "").strip()
    if not bvid:
        return {"ok": False, "error": "bvid 不能为空"}
    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "error": err}
    num = max(1, min(int(kwargs.get("num") or 30), 200))
    v = video.Video(bvid=bvid, credential=_credential_obj(cred))
    info = await v.get_info()
    aid = info["aid"]
    comments_out: list[dict[str, Any]] = []
    fetch_error = None
    page = 1
    while len(comments_out) < num:
        try:
            resp = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                order=comment.OrderType.LIKE,
                credential=_credential_obj(cred),
            )
            replies = resp.get("replies") or []
            if not replies:
                break
            for r in replies:
                member = r.get("member", {})
                content = r.get("content", {})
                c: dict[str, Any] = {
                    "rpid": r.get("rpid", 0),
                    "user": member.get("uname", ""),
                    "content": content.get("message", ""),
                    "like": r.get("like", 0),
                    "reply_count": r.get("rcount", 0),
                    "time": r.get("ctime", 0),
                }
                comments_out.append(c)
            page += 1
            await asyncio.sleep(0.4)
        except Exception as e:  # noqa: BLE001
            fetch_error = f"{type(e).__name__}: {e}"
            break
    out = {"ok": True, "bvid": bvid, "count": len(comments_out[:num]), "comments": comments_out[:num]}
    if fetch_error:
        out["error"] = fetch_error
    return out


async def _action_danmaku(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import video

    bvid = str(kwargs.get("bvid") or "").strip()
    if not bvid:
        return {"ok": False, "error": "bvid 不能为空"}
    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "error": err}
    num = max(1, min(int(kwargs.get("num") or 100), 500))
    v = video.Video(bvid=bvid, credential=_credential_obj(cred))
    danmakus = await v.get_danmakus(page_index=0)
    texts = [{"time": getattr(d, "dm_time", ""), "text": d.text} for d in (danmakus[:num] if hasattr(danmakus, "__getitem__") else [])]
    return {"ok": True, "bvid": bvid, "count": len(texts), "danmakus": texts}


async def _action_favorite_lists(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import favorite_list

    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "error": err}
    uid = int(kwargs.get("uid") or 0)
    if uid == 0:
        uid = int(cred.get("dedeuserid") or 0)
    result = await favorite_list.get_video_favorite_list(uid=uid, credential=_credential_obj(cred))
    lists = []
    for item in (result.get("list") or [])[:200]:
        lists.append(
            {
                "id": item.get("id", 0),
                "title": item.get("title", ""),
                "media_count": item.get("media_count", 0),
                "fav_state": item.get("fav_state", 0),
            }
        )
    return {"ok": True, "uid": uid, "count": len(lists), "lists": lists}


async def _action_favorite_content(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import favorite_list

    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "error": err}
    media_id = int(kwargs.get("media_id") or 0)
    if media_id <= 0:
        return {"ok": False, "error": "media_id 必须为正整数"}
    page = max(1, int(kwargs.get("page") or 1))
    keyword = str(kwargs.get("keyword") or "") or None
    result = await favorite_list.get_video_favorite_list_content(
        media_id=media_id,
        page=page,
        keyword=keyword,
        credential=_credential_obj(cred),
    )
    medias = []
    for item in (result.get("medias") or []):
        medias.append(
            {
                "bvid": item.get("bvid", ""),
                "title": item.get("title", ""),
                "play": (item.get("cnt_info") or {}).get("play", 0),
                "collect": (item.get("cnt_info") or {}).get("collect", 0),
                "author": (item.get("upper") or {}).get("name", ""),
                "duration": item.get("duration", 0),
                "fav_time": item.get("fav_time", 0),
            }
        )
    return {
        "ok": True,
        "media_id": media_id,
        "page": page,
        "has_more": result.get("has_more", False),
        "count": len(medias),
        "medias": medias,
    }


async def _action_watch_later(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import user

    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "error": err}
    result = await user.get_toview_list(credential=_credential_obj(cred))
    out = {"ok": True, "count": 0, "videos": []}
    lst = result.get("list") or result.get("data") or []
    if isinstance(lst, dict):
        lst = lst.get("list") or []
    for item in (lst or [])[:100]:
        out["videos"].append(
            {
                "bvid": item.get("bvid", ""),
                "title": item.get("title", ""),
                "author": (item.get("owner") or {}).get("name", ""),
                "played": item.get("progress", 0),
                "duration": item.get("duration", 0),
            }
        )
    out["count"] = len(out["videos"])
    return out


# ============ 关注 / 粉丝 / 点赞（原生 relation & archive 接口） ============


def _cookie_header(cred: dict[str, str]) -> str:
    """B站 Web 接口 cookie 头；键名必须是 SESSDATA / bili_jct / buvid3 / DedeUserID（大小写敏感）。"""
    return (
        f"SESSDATA={cred.get('sessdata') or ''}; "
        f"bili_jct={cred.get('bili_jct') or ''}; "
        f"buvid3={cred.get('buvid3') or ''}; "
        f"DedeUserID={cred.get('dedeuserid') or ''}"
    )


def _api_headers(cred: dict[str, str], referer: str) -> dict[str, str]:
    return {"User-Agent": _UA, "Referer": referer, "Cookie": _cookie_header(cred)}


async def _get_json(url: str, headers: dict[str, str]) -> tuple[dict[str, Any] | None, str]:
    """GET 一个 B站 web 接口，返回 (json dict, 错误说明)。"""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:200]}"
    if not isinstance(data, dict):
        return None, f"响应不是 JSON 对象（HTTP {status}）"
    return data, ""


async def _post_form_json(
    url: str, form: dict[str, str], headers: dict[str, str]
) -> tuple[dict[str, Any] | None, str]:
    """POST 表单到 B站 web 接口（csrf 会出现在 form data 里），返回 (json dict, 错误说明)。"""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:200]}"
    if not isinstance(data, dict):
        return None, f"响应不是 JSON 对象（HTTP {status}）"
    return data, ""


async def _relation_page(
    kind: str, uid: int, pn: int, ps: int, cred: dict[str, str]
) -> dict[str, Any]:
    """拉取关注（followings）或粉丝（followers）列表的一页。"""
    endpoint = "followings" if kind == "following" else "followers"
    url = f"https://api.bilibili.com/x/relation/{endpoint}?vmid={uid}&pn={pn}&ps={ps}"
    headers = _api_headers(cred, f"https://space.bilibili.com/{uid}")
    data, why = await _get_json(url, headers)
    if data is None:
        return {"ok": False, "action": kind, "error": why}
    code = int(data.get("code") or 0)
    if code != 0:
        return {
            "ok": False,
            "action": kind,
            "code": code,
            "error": f"{data.get('message') or '接口返回错误'}（code={code}）",
        }
    body = data.get("data") or {}
    items = [
        {
            "mid": it.get("mid", 0),
            "uname": it.get("uname", ""),
            "sign": (it.get("sign") or "")[:200],
        }
        for it in (body.get("list") or [])
        if isinstance(it, dict)
    ]
    return {
        "ok": True,
        "action": kind,
        "code": 0,
        "uid": uid,
        "total": int(body.get("total") or 0),
        "page": pn,
        "ps": ps,
        "count": len(items),
        "list": items,
    }


async def _action_following(kwargs: dict[str, Any]) -> dict[str, Any]:
    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "action": "following", "error": err}
    uid = int(kwargs.get("uid") or cred.get("dedeuserid") or 0)
    if uid <= 0:
        return {"ok": False, "action": "following", "error": "uid 必须为正整数（或先执行 bili login 登录）"}
    pn = max(1, int(kwargs.get("pn") or 1))
    ps = max(1, min(int(kwargs.get("ps") or 20), 50))
    return await _relation_page("following", uid, pn, ps, cred)


async def _action_followers(kwargs: dict[str, Any]) -> dict[str, Any]:
    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "action": "followers", "error": err}
    uid = int(kwargs.get("uid") or cred.get("dedeuserid") or 0)
    if uid <= 0:
        return {"ok": False, "action": "followers", "error": "uid 必须为正整数（或先执行 bili login 登录）"}
    pn = max(1, int(kwargs.get("pn") or 1))
    ps = max(1, min(int(kwargs.get("ps") or 20), 50))
    return await _relation_page("followers", uid, pn, ps, cred)


async def _read_like_state(
    cred: dict[str, str], bvid: str, aid: int
) -> tuple[int | None, str]:
    """读当前是否已点赞：返 (1/0, "")；读不到返 (None, 原因)。"""
    query = f"bvid={bvid}" if bvid else f"aid={aid}"
    url = f"https://api.bilibili.com/x/web-interface/archive/has/like?{query}"
    referer = f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com"
    data, why = await _get_json(url, _api_headers(cred, referer))
    if data is None:
        return None, why
    code = int(data.get("code") or 0)
    if code != 0:
        return None, f"has/like 不可用：{data.get('message') or '接口返回错误'}（code={code}）"
    raw = data.get("data")
    if isinstance(raw, int):  # 含 bool，True->1
        return (1 if raw else 0), ""
    return None, f"has/like 返回结构异常：{raw!r}"


async def _read_stat_like(
    cred: dict[str, str], bvid: str, aid: int
) -> tuple[int | None, str]:
    """回退方案：读 video view 接口的 stat.like 数值。"""
    query = f"bvid={bvid}" if bvid else f"aid={aid}"
    url = f"https://api.bilibili.com/x/web-interface/view?{query}"
    referer = f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com"
    data, why = await _get_json(url, _api_headers(cred, referer))
    if data is None:
        return None, why
    code = int(data.get("code") or 0)
    if code != 0:
        return None, f"view 接口不可用：{data.get('message') or '接口返回错误'}（code={code}）"
    stat = (data.get("data") or {}).get("stat") or {}
    val = stat.get("like")
    if isinstance(val, int):
        return val, ""
    return None, "view 接口未返回 stat.like"


async def _action_like(kwargs: dict[str, Any]) -> dict[str, Any]:
    """点赞 / 取消点赞（幂等：已处于目标状态不重复提交；提交后复读状态确认）。"""
    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "action": "like", "error": err}
    bvid = str(kwargs.get("bvid") or "").strip()
    aid = int(kwargs.get("aid") or 0)
    if not bvid and aid <= 0:
        return {"ok": False, "action": "like", "error": "bvid 与 aid 至少提供一个"}
    like = bool(kwargs.get("like", True))
    target = 1 if like else 0
    csrf = cred.get("bili_jct") or ""
    if not csrf:
        return {"ok": False, "action": "like", "error": "凭证缺少 bili_jct，无法点赞；请重新执行 bili login。"}
    referer = f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com"
    headers = _api_headers(cred, referer)
    base: dict[str, Any] = {
        "action": "like",
        "bvid": bvid,
        "aid": aid or None,
        "target_state": target,
    }
    # 只有 bvid 时补一次 aid，便于 POST 用 aid 定视频（两处都带上更稳）
    if aid <= 0 and bvid:
        view, _why = await _get_json(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", headers
        )
        if view and int(view.get("code") or 0) == 0:
            resolved = int((view.get("data") or {}).get("aid") or 0)
            if resolved > 0:
                aid = resolved
                base["aid"] = aid

    before, why_before = await _read_like_state(cred, bvid, aid)
    stat_before: int | None = None
    if before is None:  # has/like 不可用 → 回退 stat.like 数值对比
        stat_before, why_stat = await _read_stat_like(cred, bvid, aid)
        if stat_before is None:
            base.update({"ok": False, "error": f"无法读取当前点赞状态：{why_before or why_stat}"})
            return base

    if before is not None and before == target:
        base.update(
            {
                "ok": True,
                "code": 0,
                "like_before": before,
                "like_after": before,
                "changed": False,
                "already_in_target_state": True,
                "confirmed": True,
                "message": "已处于目标状态，未重复提交 POST",
            }
        )
        return base

    form: dict[str, str] = {"like": "1" if like else "2", "csrf": csrf}
    if bvid:
        form["bvid"] = bvid
    if aid > 0:
        form["aid"] = str(aid)
    posted, why_post = await _post_form_json(
        "https://api.bilibili.com/x/web-interface/archive/like", form, headers
    )
    if posted is None:
        base.update(
            {
                "ok": False,
                "like_before": before,
                "like_after": before,
                "changed": False,
                "already_in_target_state": False,
                "error": f"点赞请求失败：{why_post}",
            }
        )
        return base
    code = int(posted.get("code") or 0)
    if code != 0:
        base.update(
            {
                "ok": False,
                "code": code,
                "like_before": before,
                "like_after": before,
                "changed": False,
                "already_in_target_state": False,
                "error": f"{posted.get('message') or '点赞接口返回错误'}（code={code}）",
            }
        )
        return base

    after, why_after = await _read_like_state(cred, bvid, aid)
    if after is not None and after != target:
        # B 站 has/like 接口有点延迟：刚 POST 完立刻读常拿到旧值，
        # 轮询几次再判定，避免把「已经点上了」误报成「未确认」。
        for _ in range(6):
            await asyncio.sleep(0.8)
            again, why_again = await _read_like_state(cred, bvid, aid)
            if again is None:
                why_after = why_after or why_again
                break
            after = again
            if after == target:
                break
    if after is not None:
        like_before: Any = before
        like_after: Any = after
        changed = before != after if before is not None else stat_before != after
        confirmed = after == target
        if before is None and stat_before is not None:
            like_before = stat_before
    else:
        stat_after, why_stat_after = await _read_stat_like(cred, bvid, aid)
        if stat_after is None or stat_before is None:
            base.update(
                {
                    "ok": False,
                    "code": 0,
                    "like_before": before if before is not None else stat_before,
                    "like_after": before if before is not None else stat_before,
                    "changed": False,
                    "already_in_target_state": False,
                    "error": f"点赞已提交（code=0），但无法复读状态确认：{why_after or why_stat_after}",
                }
            )
            return base
        like_before = stat_before
        like_after = stat_after
        delta = stat_after - stat_before
        changed = delta != 0
        confirmed = delta == 1 if like else delta == -1

    base.update(
        {
            "ok": bool(confirmed),
            "code": 0,
            "like_before": like_before,
            "like_after": like_after,
            "changed": bool(changed),
            "already_in_target_state": False,
            "confirmed": bool(confirmed),
        }
    )
    if not confirmed:
        base["error"] = "点赞接口返回 code=0，但复读到的状态未变成目标状态"
    return base


async def _action_reply(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import comment, video

    bvid = str(kwargs.get("bvid") or "").strip()
    text = str(kwargs.get("text") or "").strip()
    if not bvid or not text:
        return {"ok": False, "error": "bvid 与 text 不能为空"}
    rpid = int(kwargs.get("rpid") or 0)
    root = int(kwargs.get("root") or 0)
    at_name = str(kwargs.get("at_name") or "").strip()
    if at_name:
        text = f"@{at_name} {text}"
    ok, reason = _quota_check()
    if not ok:
        return {"ok": False, "sent": False, "blocked_by": "quota", "error": reason}
    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "sent": False, "error": err}
    v = video.Video(bvid=bvid, credential=_credential_obj(cred))
    info = await v.get_info()
    aid = info["aid"]
    try:
        if rpid == 0:
            result = await comment.send_comment(
                text=text,
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                credential=_credential_obj(cred),
            )
        else:
            actual_root = root if root != 0 else rpid
            result = await comment.send_comment(
                text=text,
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                root=actual_root,
                parent=rpid,
                credential=_credential_obj(cred),
            )
        out: dict[str, Any] = {"ok": True, "sent": True, "message": "评论发送成功"}
        if isinstance(result, dict):
            for key in ("rpid", "rpid_str", "root", "parent"):
                val = result.get(key)
                if isinstance(val, (int, str)) and str(val).strip() and str(val) != "0":
                    out[key] = val
        _bump_usage()
        _ledger_append(text, bvid, str(out.get("rpid") or ""), kind="comment")
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sent": False, "error": f"{type(e).__name__}: {e}"}


async def _action_send_message(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import session

    receiver_uid = int(kwargs.get("receiver_uid") or 0)
    text = str(kwargs.get("text") or "").strip()
    if receiver_uid <= 0 or not text:
        return {"ok": False, "error": "receiver_uid 与 text 不能为空"}
    ok, reason = _quota_check()
    if not ok:
        return {"ok": False, "sent": False, "blocked_by": "quota", "error": reason}
    cred, err = _require_cred()
    if not cred:
        return {"ok": False, "sent": False, "error": err}
    try:
        result = await session.send_msg(
            credential=_credential_obj(cred),
            receiver_id=receiver_uid,
            msg_type=session.EventType.TEXT,
            content=text,
        )
        data = result if isinstance(result, dict) else {}
        code = data.get("code", 0)
        inner = data.get("data")
        msg_key = data.get("msg_key") or (inner.get("msg_key") if isinstance(inner, dict) else None)
        if code not in (0, None):
            return {
                "ok": False,
                "sent": False,
                "error": f"接口拒绝（code={code}）：{data.get('message') or data.get('msg') or '未知原因'}",
            }
        if not msg_key:
            return {
                "ok": False,
                "sent": False,
                "error": "接口回了成功，但**没有投递回执（msg_key）** ⇒ 可能没真正送到（常见于：对方不接受陌生人私信 / 被风控）",
            }
        _bump_usage()
        _ledger_append(text, "DM:" + str(receiver_uid), str(msg_key)[:32], kind="dm")
        return {"ok": True, "sent": True, "message": f"私信已投递（回执 {str(msg_key)[:12]}…）", "receipt": str(msg_key)[:32]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sent": False, "error": f"{type(e).__name__}: {e}"}


# ============ 扫码登录动作 ============

_login_session: dict[str, Any] = {}


async def _action_login(kwargs: dict[str, Any]) -> dict[str, Any]:
    global _login_session
    cred = _load_credential()
    if cred and cred.get("sessdata"):
        return {"ok": True, "status": "already_logged_in", "message": "已登录B站，无需重复登录", "credential": _masked(cred)}
    qr = await _qr_generate()
    _login_session = {"key": qr["key"], "url": qr["url"], "created_at": time.time()}
    qr_path = _state_dir() / "qrcode_login.png"
    rendered = _render_qr_png(qr["url"], qr_path)
    return {
        "ok": True,
        "status": "qrcode_ready",
        "message": "请用B站App扫描二维码登录（180秒内有效）",
        "qrcode_url": qr["url"],
        "qrcode_file": str(qr_path) if rendered else "",
        "expires_in_seconds": 180,
        "next_step": "用户扫码后调用 bili login_check 检查登录状态（code=0 时凭证自动落盘）",
    }


async def _action_login_check(kwargs: dict[str, Any]) -> dict[str, Any]:
    global _login_session
    if not _login_session.get("key"):
        cred = _load_credential()
        if cred and cred.get("sessdata"):
            return {"ok": True, "status": "already_logged_in", "message": "已登录B站", "credential": _masked(cred)}
        return {"ok": False, "status": "no_session", "message": "没有进行中的登录，请先执行 bili login"}
    r = await _qr_poll(_login_session["key"])
    if r.get("state") == "ok":
        jar = r.get("credential") or {}
        if not jar.get("sessdata"):
            return {
                "ok": True,
                "status": "unknown",
                "message": "登录成功了，但响应里没有 SESSDATA —— 请重试 login 或检查网络后重试",
            }
        file = _save_credential(jar)
        _login_session = {}
        return {
            "ok": True,
            "status": "done",
            "message": "登录成功！凭证已保存（权限已收紧）",
            "credential_file": str(file),
            "credential": _masked(jar),
        }
    state_map = {"waiting": "未扫码，请用B站App扫描二维码", "scanned": "已扫码，请在手机上确认", "expired": "二维码已过期，请重新执行 bili login"}
    return {"ok": True, "status": r.get("state"), "message": state_map.get(r.get("state"), r.get("message") or "unknown")}


async def _action_check_credential(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import user

    cred = _load_credential()
    if not cred or not cred.get("sessdata"):
        return {"ok": True, "logged_in": False, "message": "未登录，请执行 bili login 扫码登录"}
    try:
        uid = int(cred.get("dedeuserid") or 0)
        u = user.User(uid=uid, credential=_credential_obj(cred))
        my_info = await u.get_user_info()
        return {
            "ok": True,
            "logged_in": True,
            "uid": cred.get("dedeuserid"),
            "username": my_info.get("name", ""),
            "message": "凭证有效",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "logged_in": False, "message": f"凭证已失效或接口风控：{type(e).__name__}: {e}"}


async def _action_status(kwargs: dict[str, Any]) -> dict[str, Any]:
    cred = _load_credential()
    conf = _send_conf()
    usage = _send_usage()
    return {
        "ok": True,
        "state_dir": str(_state_dir()),
        "output_dir": str(_output_dir()),
        "credential": _masked(cred),
        "send_enabled": bool(conf.get("enabled")),
        "daily_quota": int(conf.get("daily_quota") or 0),
        "used_today": int(usage.get("count") or 0) if usage.get("date") == _today() else 0,
        "actions": _ACTIONS,
    }


async def _action_config(kwargs: dict[str, Any]) -> dict[str, Any]:
    conf = _send_conf()
    if "send_enabled" in kwargs and kwargs.get("send_enabled") is not None:
        conf["enabled"] = bool(kwargs.get("send_enabled"))
    if "daily_quota" in kwargs and kwargs.get("daily_quota") is not None:
        conf["daily_quota"] = max(0, int(kwargs.get("daily_quota")))
    conf["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write_json(_state_dir() / "send_config.json", conf)
    usage = _send_usage()
    return {
        "ok": True,
        "send_enabled": bool(conf.get("enabled")),
        "daily_quota": int(conf.get("daily_quota") or 0),
        "used_today": int(usage.get("count") or 0) if usage.get("date") == _today() else 0,
        "warning": "发送类动作会以你的账号对外发言/发私信，请确认已了解风险；发送者在台账中留痕（comments.md）。",
    }


# ============ 刷一轮 B站 日报 ============


async def _fetch_subtitle_for_report(bvid: str, cred_dict: dict[str, str] | None) -> dict[str, Any]:
    """读单条视频字幕原文；无登录/无字幕/失败都如实返回 reason，不编造。"""
    if not cred_dict:
        return {"ok": False, "reason": "无字幕/跳过（未登录，无法读取字幕）"}
    try:
        from bilibili_api import video

        v = video.Video(bvid=bvid, credential=_credential_obj(cred_dict) if cred_dict else None)
        info = await v.get_info()
        cid = info.get("cid", 0)
        if not cid and info.get("pages"):
            cid = info["pages"][0].get("cid", 0)
        if not cid:
            return {"ok": False, "reason": "无法获取cid"}
        subtitle_list = await v.get_subtitle(cid=cid)
        subtitles = subtitle_list.get("subtitles") or []
        if not subtitles:
            return {"ok": False, "reason": "无字幕"}
        target = None
        for s in subtitles:
            if s.get("lan") in ("ai-zh", "zh-CN", "zh"):
                target = s
                break
        if not target:
            target = subtitles[0]
        import aiohttp

        sub_url = target.get("subtitle_url", "")
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        async with aiohttp.ClientSession() as session:
            async with session.get(sub_url, headers={"User-Agent": _UA}, timeout=_HTTP_TIMEOUT) as resp:
                sub_data = await resp.json()
        texts = [item.get("content", "") for item in (sub_data.get("body") or [])]
        return {"ok": True, "text": "\n".join(texts), "segments": len(texts), "title": info.get("title", ""), "owner": (info.get("owner") or {}).get("name", "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:160]}"}


async def _action_daily_report(kwargs: dict[str, Any]) -> dict[str, Any]:
    from bilibili_api import search

    keywords_raw = str(kwargs.get("keywords") or "").strip()
    if not keywords_raw:
        return {"ok": False, "error": "keywords 不能为空（多个关键词用逗号/分号/换行分隔）"}
    keywords = [k.strip() for k in re.split(r"[，,;；\n]+", keywords_raw) if k.strip()]
    days = max(1, min(int(kwargs.get("days") or 7), 30))
    limit = max(1, min(int(kwargs.get("limit") or 8), 20))
    picks = max(1, min(int(kwargs.get("picks") or 5), 10))
    out_dir_raw = str(kwargs.get("out_dir") or "").strip()
    out_dir = Path(out_dir_raw).expanduser() if out_dir_raw else _output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    cred = _load_credential()
    cred_dict = cred if cred and cred.get("sessdata") else None

    now_ts = int(time.time())
    cutoff = now_ts - days * 86400
    candidates: dict[str, dict[str, Any]] = {}
    searched = 0
    for kw in keywords:
        try:
            result = await search.search_by_type(
                keyword=kw,
                search_type=search.SearchObjectType.VIDEO,
                page=1,
                order_type=search.OrderVideo.PUBDATE,
            )
            items = result.get("result") or []
            searched += len(items)
            for it in items:
                pubdate = int(it.get("pubdate") or 0)
                if pubdate < cutoff:
                    continue
                bvid = it.get("bvid", "")
                if not bvid or bvid in candidates:
                    continue
                candidates[bvid] = {
                    "bvid": bvid,
                    "title": _clean_title(it.get("title", "")),
                    "author": it.get("author", ""),
                    "play": it.get("play", 0),
                    "pubdate": pubdate,
                    "description": (it.get("description", "") or "")[:200],
                    "keywords": [kw],
                }
        except Exception:  # noqa: BLE001
            continue
    # 去重关键词标记
    for bvid, cand in candidates.items():
        for kw in keywords:
            if kw in cand["title"]:
                cand["keywords"].append(kw)

    cand_list = sorted(candidates.values(), key=lambda x: x["pubdate"], reverse=True)[:limit]
    reviewed: list[dict[str, Any]] = []
    for cand in cand_list:
        sub = await _fetch_subtitle_for_report(cand["bvid"], cred_dict)
        if not sub.get("ok"):
            reviewed.append(
                {"bvid": cand["bvid"], "title": cand["title"], "author": cand["author"],
                 "play": cand["play"], "subtitle": "", "status": "跳过", "reason": sub.get("reason", "无字幕")}
            )
            continue
        # 统计关键词出现次数作为“为什么值得”的证据
        kw_hits = {kw: sub["text"].count(kw) for kw in keywords if kw in sub["text"]}
        first_seg = sub["text"].split("\n")[0] if sub["text"] else ""
        reviewed.append(
            {"bvid": cand["bvid"], "title": cand["title"], "author": cand["author"],
             "play": cand["play"], "subtitle": sub["text"], "status": "候选",
             "reason": "", "kw_hits": kw_hits, "first_seg": first_seg}
        )
        await asyncio.sleep(0.4)

    picked = [r for r in reviewed if r["status"] == "候选"]
    picked.sort(key=lambda x: x["play"], reverse=True)
    picked = picked[:picks]

    date_str = _today()
    report_path = out_dir / f"bili-scroll-{date_str}.md"
    lines: list[str] = []
    lines.append(f"# B站 刷一轮报告 {date_str}")
    lines.append("")
    lines.append(f"- 关键词：{' / '.join(keywords)}")
    lines.append(f"- 时间范围：近 {days} 天（截止 {datetime.fromtimestamp(cutoff).strftime('%Y-%m-%d')}）")
    lines.append(f"- 本次：搜索命中 {len(candidates)} 条 → 逐条读字幕 {len(reviewed)} 条 → 挑中 {len(picked)} 条")
    lines.append(f"- 登录状态：{'已登录（可读字幕）' if cred_dict else '**未登录**：字幕读不到，逐条按“无字幕/跳过”处理'}")
    lines.append("")
    lines.append("## 挑中的（为什么值得）")
    lines.append("")
    for r in picked:
        lines.append(f"### {r['bvid']}")
        lines.append(f"- 标题：{r['title']}")
        lines.append(f"- UP主：{r['author']} · 播放：{r['play']}")
        why = f"播放量 {r['play']}；字幕中命中关键词：{'、'.join(f'{k}×{v}' for k, v in r['kw_hits'].items()) if r['kw_hits'] else '无直接关键词命中'}"
        lines.append(f"- 为什么值得：{why}")
        lines.append(f"- 一句话结论：{r['first_seg'][:80] if r['first_seg'] else r['title'][:80]}")
        lines.append("")
    lines.append("## 跳过（如实记录，不编造）")
    lines.append("")
    skipped = [r for r in reviewed if r["status"] != "候选"]
    if not skipped:
        lines.append("无。")
    for r in skipped:
        lines.append(f"- {r['bvid']} {r['title']} — {r['reason'] or '无字幕/跳过'}")
    lines.append("")
    report_text = "\n".join(lines)
    report_path.write_text(report_text, encoding="utf-8")

    return {
        "ok": True,
        "report_file": str(report_path),
        "keywords": keywords,
        "searched": searched,
        "candidates": len(candidates),
        "reviewed": len(reviewed),
        "picked": len(picked),
        "skipped": len(skipped),
        "login_used": bool(cred_dict),
        "picks": [{"bvid": r["bvid"], "title": r["title"], "play": r["play"]} for r in picked],
    }


# ============ 投稿：我的稿件 / 发布视频 / 删除稿件（2026-11 实测链路） ============
#
# 真实链路（每一步都留原始响应，code/OK 字段如实回传，绝不把"上传成功"当"发布成功"）：
#   1. GET  _PREUPLOAD_URL        -> OK=1，拿 endpoint / upos_uri / auth / biz_id / chunk_size
#   2. POST <endpoint>/<upos_uri 去 upos://>?uploads  -> OK=1，拿 upload_id
#   3. PUT  同一 URL，分片 body + partNumber/uploadId -> 文本 MULTIPART_PUT_SUCCESS
#   4. POST 同一 URL + parts JSON                    -> OK=1，key => videos[].filename / cid
#   5. POST _ARCHIVE_ADD_URL (csrf + JSON meta)      -> code=0，data.aid/bvid
#   6. GET  _ARCHIVE_LIST_URL                        -> 回读稿件
#   7. POST _ARCHIVE_DELETE_URL                      -> 删稿
# 注意：member 侧 `/x/vu/web/delete` 在 2026-11 已 404，改用 `/x/web/archive/delete`。


def _member_headers(cred: dict[str, str]) -> dict[str, str]:
    """member.bilibili.com 侧请求头（Referer 落到创作中心，部分接口校验来源）。"""
    return {
        "User-Agent": _UA,
        "Referer": "https://member.bilibili.com/platform/upload/center",
        "Origin": "https://member.bilibili.com",
        "Cookie": _cookie_header(cred),
    }


async def _post_json_req(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[dict[str, Any] | None, str, int]:
    """POST JSON 到 member 接口，返回 (json, 错误说明, HTTP 状态)。"""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:200]}", -1
    if not isinstance(data, dict):
        return None, f"响应不是 JSON 对象（HTTP {status}）", status
    return data, "", status


def _tags_to_list(raw: Any) -> list[str]:
    """tags 接受 list 或逗号/分号/空格分隔的字符串，统一成 list（最多 10 个）。"""
    if isinstance(raw, list):
        items = [str(t).strip() for t in raw]
    else:
        items = [t.strip() for t in re.split(r"[，,;；\s]+", str(raw or ""))]
    out: list[str] = []
    for t in items:
        if t and t not in out:
            out.append(t)
    return out[:10]


async def _my_archives_page(
    cred: dict[str, str], pn: int, ps: int, status: str
) -> dict[str, Any]:
    """拉一页本账号稿件列表（member 侧）。"""
    url = (
        f"{_ARCHIVE_LIST_URL}?status={status}&pn={pn}&ps={ps}"
        "&coop=1&interactive=1&order=mtime&sort=desc&draft=1"
    )
    data, why = await _get_json(url, _member_headers(cred))
    if data is None:
        return {"ok": False, "error": why, "url": url}
    code = int(data.get("code") or 0)
    if code != 0:
        return {
            "ok": False,
            "url": url,
            "code": code,
            "message": data.get("message"),
            "error": f"稿件列表接口返回 code={code}: {data.get('message')}",
        }
    return {"ok": True, "url": url, "data": data.get("data") or {}}


async def _action_my_videos(kwargs: dict[str, Any]) -> dict[str, Any]:
    """回读本账号稿件列表（含审核中/已发布/未通过），用于发布后的平台侧证据。"""
    cred = _load_credential()
    if not cred or not cred.get("sessdata"):
        return {"ok": False, "error": "未登录：缺少 SESSDATA，请先执行 bili login"}
    pn = max(1, int(kwargs.get("pn") or 1))
    ps = max(1, min(int(kwargs.get("ps") or 20), 50))
    status = str(kwargs.get("status") or "is_pubing,pubed,not_pubed").strip() or "is_pubing,pubed,not_pubed"

    page = await _my_archives_page(cred, pn, ps, status)
    if not page.get("ok"):
        return {
            "ok": False,
            "action": "my_videos",
            "error": page.get("error"),
            "code": page.get("code"),
            "message": page.get("message"),
            "url": page.get("url"),
        }

    data = page.get("data") or {}
    raw_items = data.get("arc_audits") or data.get("archives") or []
    items: list[dict[str, Any]] = []
    for it in raw_items:
        archive = it.get("Archive") if isinstance(it, dict) and "Archive" in it else it
        archive = archive or {}
        items.append(
            {
                "aid": archive.get("aid"),
                "bvid": archive.get("bvid", ""),
                "title": archive.get("title", ""),
                "state": archive.get("state"),
                "state_desc": archive.get("state_desc", ""),
                "pubtime": archive.get("ptime") or archive.get("pubdate"),
                "ctime": archive.get("ctime"),
                "duration": archive.get("duration"),
            }
        )
    page_info = data.get("page") or {}
    return {
        "ok": True,
        "action": "my_videos",
        "query": {"pn": pn, "ps": ps, "status": status},
        "url": page.get("url"),
        "code": 0,
        "total": page_info.get("count", len(items)),
        "class": data.get("class"),
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(UTC).isoformat(),
    }


async def _preupload_video(cred: dict[str, str], video: Path, size: int) -> dict[str, Any]:
    """第 1 步：预上传，拿 endpoint / upos_uri / auth / biz_id / chunk_size。"""
    import aiohttp

    params = {
        "profile": "ugcfx/bup",
        "name": video.name,
        "size": str(size),
        "r": "upos",
        "ssl": "0",
        "version": "2.14.0",
        "build": "2100400",
        "upcdn": "bda2",
        "probe_version": "20221109",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _PREUPLOAD_URL, params=params, headers=_member_headers(cred), timeout=_HTTP_TIMEOUT
            ) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "step": "preupload", "error": f"{type(e).__name__}: {str(e)[:200]}"}
    if not isinstance(data, dict):
        return {"ok": False, "step": "preupload", "error": f"响应不是 JSON（HTTP {status}）"}
    if int(data.get("OK") or 0) != 1:
        return {
            "ok": False,
            "step": "preupload",
            "http_status": status,
            "response": data,
            "error": f"预上传失败：OK={data.get('OK')} message={data.get('message')}",
        }
    return {"ok": True, "step": "preupload", "http_status": status, "response": data, "data": data}


async def _upload_video_file(
    cred: dict[str, str], video: Path, pre: dict[str, Any]
) -> dict[str, Any]:
    """第 2-4 步：方向取 upload_id → 分片 PUT → 合片，返回 filename / cid。"""
    import aiohttp

    size = video.stat().st_size
    endpoint = str(pre.get("endpoint") or "")
    host = ("https:" + endpoint) if endpoint.startswith("//") else endpoint
    upos_uri = str(pre.get("upos_uri") or "")
    url_up = f"{host}/{upos_uri.removeprefix('upos://')}"
    auth = str(pre.get("auth") or "")
    biz_id = pre.get("biz_id")
    chunk_size = max(1, int(pre.get("chunk_size") or 10485760))

    if not host or not upos_uri:
        return {
            "ok": False,
            "step": "upload",
            "error": f"预上传返回缺少 endpoint/upos_uri：endpoint={endpoint!r} upos_uri={upos_uri!r}",
        }
    if chunk_size > size:
        chunk_size = size

    steps: list[dict[str, Any]] = []
    try:
        async with aiohttp.ClientSession() as session:
            # --- 取 upload_id ---
            init_params = {
                "uploads": "",
                "output": "json",
                "profile": "ugcfx/bup",
                "filesize": str(size),
                "partsize": str(chunk_size),
                "biz_id": str(biz_id),
            }
            init_headers = {
                "User-Agent": _UA,
                "Referer": "https://www.bilibili.com",
                "Cookie": _cookie_header(cred),
                "x-upos-auth": auth,
            }
            async with session.post(
                url_up, params=init_params, headers=init_headers, timeout=_HTTP_TIMEOUT
            ) as resp:
                init_status = resp.status
                init = await resp.json(content_type=None)
            steps.append({"step": "init_upload_id", "url": url_up, "params": init_params,
                          "http_status": init_status, "response": init})
            if not isinstance(init, dict) or int(init.get("OK") or 0) != 1:
                return {"ok": False, "step": "init_upload_id", "steps": steps,
                        "error": f"取 upload_id 失败：{init}"}
            upload_id = init.get("upload_id")

            # --- 分片 PUT ---
            blob = video.read_bytes()
            offsets = list(range(0, size, chunk_size))
            total_chunks = len(offsets)
            for idx, offset in enumerate(offsets):
                chunk = blob[offset:offset + chunk_size]
                put_params = {
                    "partNumber": str(idx + 1),
                    "uploadId": str(upload_id),
                    "chunk": str(idx),
                    "chunks": str(total_chunks),
                    "size": str(len(chunk)),
                    "start": str(offset),
                    "end": str(offset + len(chunk)),
                    "total": str(size),
                }
                async with session.put(
                    url_up, data=chunk, params=put_params,
                    headers={"User-Agent": _UA, "x-upos-auth": auth},
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    part_status = resp.status
                    part_body = (await resp.text())[:200]
                steps.append({"step": f"chunk_put_{idx + 1}_of_{total_chunks}", "url": url_up,
                              "params": put_params, "http_status": part_status,
                              "response": part_body, "bytes": len(chunk)})
                if part_status >= 400 or part_body not in ("MULTIPART_PUT_SUCCESS", ""):
                    return {"ok": False, "step": f"chunk_put_{idx + 1}", "steps": steps,
                            "error": f"分片上传失败：HTTP {part_status} body={part_body!r}"}

            # --- 合片 ---
            parts = [{"partNumber": i + 1, "eTag": "etag"} for i in range(total_chunks)]
            done_params = {
                "output": "json",
                "name": video.name,
                "profile": "ugcfx/bup",
                "uploadId": str(upload_id),
                "biz_id": str(biz_id),
            }
            async with session.post(
                url_up, data=json.dumps({"parts": parts}), params=done_params,
                headers={
                    "User-Agent": _UA,
                    "Referer": "https://www.bilibili.com",
                    "Cookie": _cookie_header(cred),
                    "x-upos-auth": auth,
                    "content-type": "application/json; charset=UTF-8",
                },
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                done_status = resp.status
                done = await resp.json(content_type=None)
            steps.append({"step": "complete_upload", "url": url_up, "params": done_params,
                          "http_status": done_status, "response": done})
            if not isinstance(done, dict) or int(done.get("OK") or 0) != 1:
                return {"ok": False, "step": "complete_upload", "steps": steps,
                        "error": f"合片失败：{done}"}
            key = str(done.get("key") or "")
            filename = os.path.splitext(key.removeprefix("/"))[0]
            if not filename:
                return {"ok": False, "step": "complete_upload", "steps": steps,
                        "error": f"合片返回缺少 key：{done}"}
            return {
                "ok": True,
                "steps": steps,
                "upload_id": upload_id,
                "filename": filename,
                "cid": biz_id,
                "location": done.get("location"),
            }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "step": "upload", "steps": steps,
                "error": f"{type(e).__name__}: {str(e)[:300]}"}


async def _upload_cover_file(cred: dict[str, str], cover: Path) -> dict[str, Any]:
    """可选：把封面图以 dataURI 方式传到 member 侧，返回封面 URL。"""
    import base64

    mime = "image/png" if cover.suffix.lower() == ".png" else "image/jpeg"
    try:
        b64 = base64.b64encode(cover.read_bytes()).decode("utf-8")
    except OSError as e:
        return {"ok": False, "error": f"读取封面失败：{e}"}
    form = {"cover": f"data:{mime};base64,{b64}", "csrf": cred.get("bili_jct", "")}
    url = f"{_COVER_UP_URL}?csrf={cred.get('bili_jct', '')}"
    data, why = await _post_form_json(url, form, _member_headers(cred))
    if data is None:
        return {"ok": False, "error": why, "url": url}
    if int(data.get("code") or 0) != 0:
        return {"ok": False, "url": url, "code": data.get("code"),
                "message": data.get("message"), "error": f"封面上传失败 code={data.get('code')}"}
    return {"ok": True, "url": url, "code": 0, "data": data.get("data") or {},
            "cover_url": (data.get("data") or {}).get("url", "")}


async def _action_publish_video(kwargs: dict[str, Any]) -> dict[str, Any]:
    """上传并投稿一个视频。dry_run=True 只走到合片，不提交稿件。"""
    cred = _load_credential()
    if not cred or not cred.get("sessdata"):
        return {"ok": False, "error": "未登录：缺少 SESSDATA，请先执行 bili login"}
    if not cred.get("bili_jct"):
        return {"ok": False, "error": "缺少 bili_jct（csrf），无法提交稿件，请重新 bili login"}

    raw_path = str(kwargs.get("video_path") or "").strip()
    if not raw_path:
        return {"ok": False, "error": "video_path 不能为空（必须是绝对路径）"}
    video = Path(raw_path).expanduser()
    if not video.is_absolute():
        return {"ok": False, "error": f"video_path 必须是绝对路径：{raw_path}"}
    if not video.is_file():
        return {"ok": False, "error": f"视频文件不存在：{video}"}
    if video.suffix.lower() not in _ALLOWED_VIDEO_EXT:
        return {
            "ok": False,
            "error": f"不支持的视频后缀 {video.suffix}；仅支持 {sorted(_ALLOWED_VIDEO_EXT)}",
        }
    size = video.stat().st_size
    if size <= 0:
        return {"ok": False, "error": f"视频文件为空（0 字节）：{video}"}

    title = str(kwargs.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title 不能为空"}
    if len(title) > 80:
        return {"ok": False, "error": f"title 超过 80 字（当前 {len(title)} 字）"}
    desc = str(kwargs.get("desc") or "")[:2000]
    tags = _tags_to_list(kwargs.get("tags"))
    if not tags:
        return {"ok": False, "error": "tags 至少需要 1 个标签"}
    try:
        tid = int(kwargs.get("tid") or 21)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"tid 必须是整数分区 id：{kwargs.get('tid')!r}"}
    copyright_val = 1 if int(kwargs.get("copyright") or 1) == 1 else 2
    dry_run = bool(kwargs.get("dry_run"))

    cover_path_raw = str(kwargs.get("cover_path") or "").strip()
    cover: Path | None = None
    if cover_path_raw:
        cover = Path(cover_path_raw).expanduser()
        if not cover.is_file():
            return {"ok": False, "error": f"封面文件不存在：{cover}"}
        if cover.suffix.lower() not in _ALLOWED_COVER_EXT:
            return {"ok": False, "error": f"不支持的封面后缀 {cover.suffix}；仅支持 {sorted(_ALLOWED_COVER_EXT)}"}

    steps: list[dict[str, Any]] = []

    # 第 1 步：预上传
    pre_res = await _preupload_video(cred, video, size)
    steps.append({k: v for k, v in pre_res.items() if k != "data"})
    if not pre_res.get("ok"):
        return {"ok": False, "action": "publish_video", "step": "preupload",
                "steps": steps, "error": pre_res.get("error"),
                "response": pre_res.get("response")}
    pre = pre_res["data"]

    # 第 2-4 步：取 upload_id → 分片 → 合片
    up = await _upload_video_file(cred, video, pre)
    steps.extend(up.get("steps") or [])
    if not up.get("ok"):
        return {"ok": False, "action": "publish_video", "step": up.get("step"),
                "steps": steps, "error": up.get("error")}

    result: dict[str, Any] = {
        "ok": True,
        "action": "publish_video",
        "dry_run": dry_run,
        "video_path": str(video),
        "video_size_bytes": size,
        "preupload": {
            "endpoint": pre.get("endpoint"),
            "upos_uri": pre.get("upos_uri"),
            "biz_id": pre.get("biz_id"),
            "chunk_size": pre.get("chunk_size"),
        },
        "upload": {
            "upload_id": up.get("upload_id"),
            "filename": up.get("filename"),
            "cid": up.get("cid"),
            "location": up.get("location"),
        },
        "steps": steps,
        "submitted": False,
    }

    if dry_run:
        result["message"] = "dry_run=True：已完成预上传/分片/合片校验，未提交稿件（线上无新稿件）"
        return result

    # 可选封面
    cover_url = ""
    if cover is not None:
        cov = await _upload_cover_file(cred, cover)
        result["cover_upload"] = {k: v for k, v in cov.items() if k != "data"}
        if not cov.get("ok"):
            return {"ok": False, "action": "publish_video", "step": "cover_up",
                    "steps": steps, "upload": result["upload"],
                    "error": cov.get("error"), "code": cov.get("code"), "message": cov.get("message")}
        cover_url = cov.get("cover_url", "")

    # 第 5 步：提交稿件
    meta: dict[str, Any] = {
        "title": title,
        "copyright": copyright_val,
        "tid": tid,
        "tag": ",".join(tags),
        "desc_format_id": 9999,
        "desc": desc,
        "recreate": -1,
        "dynamic": "",
        "interactive": 0,
        "act_reserve_create": 0,
        "no_disturbance": 0,
        "no_reprint": 1,
        "subtitle": {"open": 0, "lan": ""},
        "dolby": 0,
        "lossless_music": 0,
        "up_selection_reply": False,
        "up_close_reply": False,
        "up_close_danmu": False,
        "web_os": 1,
        "source": "",
        "videos": [
            {
                "title": title[:80],
                "desc": desc[:250],
                "filename": up.get("filename"),
                "cid": up.get("cid"),
            }
        ],
        "cover": cover_url,
    }
    add_url = f"{_ARCHIVE_ADD_URL}?t={int(time.time() * 1000)}&csrf={cred.get('bili_jct')}"
    add_data, why, add_status = await _post_json_req(add_url, meta, _member_headers(cred))
    steps.append({"step": "submit_archive", "url": add_url, "http_status": add_status,
                  "response": add_data if add_data is not None else why,
                  "request_meta": {k: v for k, v in meta.items() if k != "cover"}})
    result["steps"] = steps
    if add_data is None:
        return {"ok": False, "action": "publish_video", "step": "submit_archive",
                "steps": steps, "upload": result["upload"], "error": why}
    add_code = int(add_data.get("code") or 0)
    if add_code != 0:
        return {
            "ok": False,
            "action": "publish_video",
            "step": "submit_archive",
            "steps": steps,
            "upload": result["upload"],
            "code": add_code,
            "message": add_data.get("message"),
            "response": add_data,
            "error": f"投稿接口返回 code={add_code}: {add_data.get('message')}",
        }

    data = add_data.get("data") or {}
    result.update(
        {
            "submitted": True,
            "code": add_code,
            "message": add_data.get("message"),
            "aid": data.get("aid"),
            "bvid": data.get("bvid"),
            "title": title,
            "tid": tid,
            "tags": tags,
            "copyright": copyright_val,
            "cover_url": cover_url,
            "submit_response": add_data,
            "note": "submitted=True 只代表稿件提交成功；是否过审/已发布需用 my_videos 回读 state/state_desc 确认。",
        }
    )
    return result


async def _action_delete_video(kwargs: dict[str, Any]) -> dict[str, Any]:
    """删除稿件。confirm 不为 True 时只返回预览，不动线上数据。"""
    cred = _load_credential()
    if not cred or not cred.get("sessdata"):
        return {"ok": False, "error": "未登录：缺少 SESSDATA，请先执行 bili login"}
    if not cred.get("bili_jct"):
        return {"ok": False, "error": "缺少 bili_jct（csrf），无法删除稿件，请重新 bili login"}

    aid = kwargs.get("aid")
    bvid = str(kwargs.get("bvid") or "").strip()
    if not aid and not bvid:
        return {"ok": False, "error": "aid 与 bvid 至少给一个"}
    confirm = bool(kwargs.get("confirm"))

    resolved: dict[str, Any] = {"aid": None, "bvid": bvid, "title": "", "state": None, "state_desc": ""}
    # bvid → aid：走公开 view 接口
    if not aid and bvid:
        info, why = await _get_json(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            _api_headers(cred, "https://www.bilibili.com"),
        )
        if info is None:
            return {"ok": False, "action": "delete_video", "error": f"bvid 换 aid 失败：{why}"}
        if int(info.get("code") or 0) != 0:
            return {"ok": False, "action": "delete_video", "code": info.get("code"),
                    "message": info.get("message"),
                    "error": f"bvid 换 aid 接口返回 code={info.get('code')}: {info.get('message')}"}
        vdata = info.get("data") or {}
        owner_mid = str((vdata.get("owner") or {}).get("mid") or "")
        if owner_mid and owner_mid != str(cred.get("dedeuserid") or ""):
            return {"ok": False, "action": "delete_video",
                    "error": f"该 bvid 不属于当前账号（owner mid={owner_mid}，当前 uid={cred.get('dedeuserid')}）"}
        aid = int(vdata.get("aid") or 0)
        resolved.update({
            "aid": aid,
            "bvid": vdata.get("bvid", bvid),
            "title": vdata.get("title", ""),
            "state": vdata.get("state"),
            "state_desc": vdata.get("state_desc", ""),
        })
    else:
        aid = int(aid)
        resolved["aid"] = aid
        if bvid:
            resolved["bvid"] = bvid

    if not confirm:
        return {
            "ok": True,
            "action": "delete_video",
            "will_delete": True,
            "deleted": False,
            "confirm_required": True,
            "target": resolved,
            "message": "confirm 不为 True：本次只做预览，未调用删稿接口。确认要删请传 confirm=True。",
        }

    url = f"{_ARCHIVE_DELETE_URL}?csrf={cred.get('bili_jct')}"
    # 前端 deleteArticle 的真实字段：aid + geetest_challenge + geetest_validate + geetest_seccode。
    # 这里 json / form 两种都试；只有账号命中 GAIA 风控（需先过验证码）时才需要传 geetest_*。
    geetest = {k: kwargs.get(k) for k in ("geetest_challenge", "geetest_validate", "geetest_seccode")
               if kwargs.get(k)}
    attempts: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    final_status = -1
    for form in (False, True):
        headers = _member_headers(cred)
        headers["X-Requested-With"] = "XMLHttpRequest"
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                if form:
                    body = {"aid": str(resolved["aid"]), "csrf": cred.get("bili_jct", ""),
                            "del_reason": "", **geetest}
                    async with session.post(url, data=body, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                        status = resp.status
                        data = await resp.json(content_type=None)
                else:
                    body = {"aid": resolved["aid"], "del_reason": "", **geetest}
                    async with session.post(url, json=body, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                        status = resp.status
                        data = await resp.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            attempts.append({"body_kind": "form" if form else "json", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            continue
        attempts.append({"body_kind": "form" if form else "json", "request_body": body,
                         "http_status": status, "response": data})
        final, final_status = data, status
        if isinstance(data, dict) and int(data.get("code") or 0) == 0:
            break

    code = int(final.get("code") or 0) if isinstance(final, dict) else None
    result: dict[str, Any] = {
        "ok": code == 0,
        "action": "delete_video",
        "url": url,
        "http_status": final_status,
        "code": code,
        "message": (final or {}).get("message") if isinstance(final, dict) else None,
        "deleted": code == 0,
        "target": resolved,
        "attempts": attempts,
        "response": final,
        "error": None if code == 0 else f"删稿接口返回 code={code}: {(final or {}).get('message') if isinstance(final, dict) else final}",
        "note": "删除是否生效请用 my_videos 回读确认（删除后稿件不再出现在列表中）。",
    }

    # code != 0 时追加一次风险网关预检（/x/risk/archive/del），把风控判定一并带回，
    # 便于定位是「参数错」还是「账号命中 GAIA 风控需人工过验证码」。
    if code != 0:
        # 与前端 getArchiveDelRisk 完全一致的参数：platform=web&bvid=<bvid>
        risk_target = resolved.get("bvid") or ""
        risk_qs = f"platform=web&bvid={risk_target}" if risk_target else f"platform=web&aid={resolved['aid']}"
        risk_url = f"https://member.bilibili.com/x/risk/archive/del?{risk_qs}&csrf={cred.get('bili_jct')}"
        try:
            risk_headers = _member_headers(cred)
            risk_headers["X-Requested-With"] = "XMLHttpRequest"
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(risk_url, headers=risk_headers, timeout=_HTTP_TIMEOUT) as resp:
                    risk_status = resp.status
                    risk_body = await resp.json(content_type=None)
                    gaia_vvoucher = resp.headers.get("x-bili-gaia-vvoucher")
                    gaia_param = resp.headers.get("x-bili-gaia-param")
        except Exception:  # noqa: BLE001
            risk_body = None
            risk_status = -1
            gaia_vvoucher = None
            gaia_param = None
        result["risk_precheck"] = {
            "url": risk_url,
            "http_status": risk_status,
            "response": risk_body,
            "x_bili_gaia_vvoucher": gaia_vvoucher,
            "x_bili_gaia_param": gaia_param,
            "hint": (
                "B 站删稿走 GAIA 风控网关：/x/web/archive/delete 命中风控时返回 code=340022「验证码错误」，"
                "配套的 /x/risk/archive/del 会返回 code=340052「您的账号存在风险请先验证」。该挑战需在"
                "创作中心（https://member.bilibili.com/platform/upload-manager/article）网页端人工通过"
                "滑块/验证码后才能解除；纯 HTTP 无法自行通过，也不存在可绕过的免验证码删稿接口。"
            ) if (isinstance(risk_body, dict) and int(risk_body.get("code") or 0) == 340052) else None,
        }

    return result


# ============ 工具主体 ============


class BiliTool(Tool):
    """B站能力包：只读为主 + 扫码登录 + 刷一轮报告；发送类默认关闭。"""

    # 动作都走 aiohttp / bilibili_api 的异步，天然可取消；长循环在阶段边界 yield。
    # 不声明 self_enforced_timeout：交给外层 watchdog 保底。
    # 不声明 rerun_safe：send 动作是工具级声明救不回来的对外副作用（同一句
    # 正文重放会再发一条），恢复车道交给模型按回执台账裁定。

    @property
    def name(self) -> str:
        return "bili"

    @property
    def description(self) -> str:
        return (
            "B站能力包：搜索/热榜/排行/视频详情/UP主/字幕/评论/弹幕/收藏夹/日报；"
            "评论与私信默认关闭，需 config 开启并受单日配额限制。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": _ACTIONS,
                    "description": "要执行的动作：search/hot/rank/video_info/user_info/user_videos（无需登录）；login/login_check/check_credential/status（登录与状态）；subtitle/comments/danmaku/favorite_lists/favorite_content/watch_later（需登录）；following/followers（关注/粉丝列表，需登录）；like（点赞/取消点赞，需登录）；reply/send_message（发送类，默认关闭）；config（发送开关与配额）；daily_report（刷一轮B站日报工作流）；my_videos/publish_video/delete_video（本账号稿件回读/投稿上传/删除稿件，需登录）。",
                },
                "keyword": {"type": "string", "description": "搜索关键词（search / daily_report）。"},
                "keywords": {"type": "string", "description": "日报关键词组，逗号/分号/换行分隔（daily_report）。"},
                "num": {"type": "integer", "minimum": 1, "maximum": 50, "description": "返回条数（search/comments/danmaku）。"},
                "order": {"type": "string", "enum": ["totalrank", "click", "pubdate", "dm"], "description": "搜索排序（search）。"},
                "page": {"type": "integer", "minimum": 1, "description": "页码（search/favorite_content）。"},
                "bvid": {"type": "string", "description": "视频BV号（video_info/subtitle/comments/danmaku/reply/like）。"},
                "aid": {"type": "integer", "minimum": 1, "description": "视频AV号（like，与 bvid 二选一，至少给一个）。"},
                "uid": {"type": "integer", "minimum": 1, "description": "用户UID（user_info/user_videos/favorite_lists/following/followers；0=自己）。"},
                "pn": {"type": "integer", "minimum": 1, "description": "页码（hot/user_videos/following/followers）。"},
                "ps": {"type": "integer", "minimum": 1, "maximum": 50, "description": "每页数量（hot/user_videos/following/followers，1-50）。"},
                "category": {"type": "string", "enum": sorted(_RANK_TYPE_MAP), "description": "排行榜分区（rank）。"},
                "day": {"type": "integer", "enum": [3, 7], "description": "排行时间维度（rank）。"},
                "media_id": {"type": "integer", "minimum": 1, "description": "收藏夹ID（favorite_content）。"},
                "text": {"type": "string", "description": "评论/私信正文（reply/send_message）。"},
                "rpid": {"type": "integer", "minimum": 0, "description": "目标评论ID，0=发表新评论（reply）。"},
                "root": {"type": "integer", "minimum": 0, "description": "楼层根评论ID，回复一级评论时填0（reply）。"},
                "at_name": {"type": "string", "description": "@ 提及的昵称，会将正文前缀 '@昵称 ' 后发出（reply，可选）。"},
                "receiver_uid": {"type": "integer", "minimum": 1, "description": "私信接收者UID（send_message）。"},
                "like": {"type": "boolean", "description": "点赞目标状态：true=点赞，false=取消点赞（like，幂等，默认 true）。"},
                "send_enabled": {"type": "boolean", "description": "发送类动作总开关（config）。"},
                "daily_quota": {"type": "integer", "minimum": 0, "description": "单日发送配额（config）。"},
                "days": {"type": "integer", "minimum": 1, "maximum": 30, "description": "日报时间范围天数，默认7（daily_report）。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "最多逐条读字幕的候选数，默认8（daily_report）。"},
                "picks": {"type": "integer", "minimum": 1, "maximum": 10, "description": "报告挑中条数上限，默认5（daily_report）。"},
                "out_dir": {"type": "string", "description": "报告输出目录，默认 <data>/output（daily_report）。"},
                "video_path": {"type": "string", "description": "视频文件绝对路径（publish_video，必填）。"},
                "title": {"type": "string", "description": "稿件标题，最多 80 字（publish_video，必填）。"},
                "desc": {"type": "string", "description": "稿件简介，最多 2000 字（publish_video）。"},
                "tags": {
                    "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                    "description": "标签，list 或逗号分隔字符串，1-10 个（publish_video）。",
                },
                "tid": {"type": "integer", "minimum": 1, "description": "分区 id，如 21=日常、231=计算机技术；默认 21（publish_video）。"},
                "copyright": {"type": "integer", "enum": [1, 2], "description": "1=自制（默认），2=转载（publish_video）。"},
                "cover_path": {"type": "string", "description": "封面图绝对路径，可选（publish_video）。"},
                "dry_run": {"type": "boolean", "description": "true 时只做预上传/分片/合片校验，不提交稿件（publish_video，默认 false）。"},
                "confirm": {"type": "boolean", "description": "必须显式 true 才真删；默认 false 只返回 will_delete 预览（delete_video）。"},
                "geetest_challenge": {"type": "string", "description": "可选。账号命中 GAIA 风控时，网页端过滑块后从删稿请求里取到的 geetest_challenge 透传（delete_video）。"},
                "geetest_validate": {"type": "string", "description": "可选。过滑块后取到的 geetest_validate 透传（delete_video）。"},
                "geetest_seccode": {"type": "string", "description": "可选。过滑块后取到的 geetest_seccode 透传（delete_video）。"},
                "status": {"type": "string", "description": "稿件状态过滤，逗号分隔，默认 is_pubing,pubed,not_pubed（my_videos）。"},
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        action = str(kwargs.get("action") or "").strip()
        if not action:
            return {"ok": False, "error": "action 不能为空", "actions": _ACTIONS}
        try:
            # 惰性确认依赖可导入；缺失时给出安装指引而不是让加载直接崩
            import bilibili_api  # noqa: F401
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "error": "B站 Python 依赖未安装，无法执行。请在项目 .venv 里安装："
                "bilibili-api-python aiohttp qrcode Pillow（详见 toolskills/SKILL.md）。",
                "detail": f"{type(e).__name__}: {e}",
            }
        dispatch = {
            "search": _action_search,
            "hot": _action_hot,
            "rank": _action_rank,
            "video_info": _action_video_info,
            "user_info": _action_user_info,
            "user_videos": _action_user_videos,
            "login": _action_login,
            "login_check": _action_login_check,
            "check_credential": _action_check_credential,
            "status": _action_status,
            "config": _action_config,
            "subtitle": _action_subtitle,
            "comments": _action_comments,
            "danmaku": _action_danmaku,
            "favorite_lists": _action_favorite_lists,
            "favorite_content": _action_favorite_content,
            "watch_later": _action_watch_later,
            "following": _action_following,
            "followers": _action_followers,
            "like": _action_like,
            "reply": _action_reply,
            "send_message": _action_send_message,
            "daily_report": _action_daily_report,
            "my_videos": _action_my_videos,
            "publish_video": _action_publish_video,
            "delete_video": _action_delete_video,
        }
        fn = dispatch.get(action)
        if fn is None:
            return {"ok": False, "error": f"未知 action: {action}", "actions": _ACTIONS}
        try:
            return await fn(kwargs)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "action": action, "error": f"{type(e).__name__}: {str(e)[:400]}"}


def build(runtime):  # noqa: ANN001, ANN201
    """Negi 资源装载入口：返回 Tool 实例。"""
    return BiliTool()
