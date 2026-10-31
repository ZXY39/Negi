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
    "reply", "send_message", "config", "daily_report",
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
                    "description": "要执行的动作：search/hot/rank/video_info/user_info/user_videos（无需登录）；login/login_check/check_credential/status（登录与状态）；subtitle/comments/danmaku/favorite_lists/favorite_content/watch_later（需登录）；reply/send_message（发送类，默认关闭）；config（发送开关与配额）；daily_report（刷一轮B站日报工作流）。",
                },
                "keyword": {"type": "string", "description": "搜索关键词（search / daily_report）。"},
                "keywords": {"type": "string", "description": "日报关键词组，逗号/分号/换行分隔（daily_report）。"},
                "num": {"type": "integer", "minimum": 1, "maximum": 50, "description": "返回条数（search/comments/danmaku）。"},
                "order": {"type": "string", "enum": ["totalrank", "click", "pubdate", "dm"], "description": "搜索排序（search）。"},
                "page": {"type": "integer", "minimum": 1, "description": "页码（search/favorite_content）。"},
                "bvid": {"type": "string", "description": "视频BV号（video_info/subtitle/comments/danmaku/reply）。"},
                "uid": {"type": "integer", "minimum": 1, "description": "用户UID（user_info/user_videos/favorite_lists；0=自己）。"},
                "pn": {"type": "integer", "minimum": 1, "description": "页码（hot/user_videos）。"},
                "ps": {"type": "integer", "minimum": 1, "maximum": 50, "description": "每页数量（hot/user_videos）。"},
                "category": {"type": "string", "enum": sorted(_RANK_TYPE_MAP), "description": "排行榜分区（rank）。"},
                "day": {"type": "integer", "enum": [3, 7], "description": "排行时间维度（rank）。"},
                "media_id": {"type": "integer", "minimum": 1, "description": "收藏夹ID（favorite_content）。"},
                "text": {"type": "string", "description": "评论/私信正文（reply/send_message）。"},
                "rpid": {"type": "integer", "minimum": 0, "description": "目标评论ID，0=发表新评论（reply）。"},
                "root": {"type": "integer", "minimum": 0, "description": "楼层根评论ID，回复一级评论时填0（reply）。"},
                "at_name": {"type": "string", "description": "@ 提及的昵称，会将正文前缀 '@昵称 ' 后发出（reply，可选）。"},
                "receiver_uid": {"type": "integer", "minimum": 1, "description": "私信接收者UID（send_message）。"},
                "send_enabled": {"type": "boolean", "description": "发送类动作总开关（config）。"},
                "daily_quota": {"type": "integer", "minimum": 0, "description": "单日发送配额（config）。"},
                "days": {"type": "integer", "minimum": 1, "maximum": 30, "description": "日报时间范围天数，默认7（daily_report）。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "最多逐条读字幕的候选数，默认8（daily_report）。"},
                "picks": {"type": "integer", "minimum": 1, "maximum": 10, "description": "报告挑中条数上限，默认5（daily_report）。"},
                "out_dir": {"type": "string", "description": "报告输出目录，默认 <data>/output（daily_report）。"},
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
            "reply": _action_reply,
            "send_message": _action_send_message,
            "daily_report": _action_daily_report,
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
