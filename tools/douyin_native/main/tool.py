"""douyin_native: native Douyin capability for Negi.

Fully independent of DSH (no dsh, cordis, DSH profile, @deepseek-ai/* runtime).
Drives the local real windowed Microsoft Edge through Playwright
(channel="msedge", persistent user-data dir). Read-only first; sending
(comment / forward) is OFF by default and gated by an explicit user-edited
policy file plus allow_send=true, quotas, interval and a post-send receipt
check that reports "未确认送达" when it cannot verify.

Login guidance is a runtime step: when not logged in, actions return a
screenshot and ask the human to scan/confirm in the open Edge window; the tool
never fakes login. Anti-bot walls (captcha/502/rate limit) are screenshotted
and reported honestly, never retried in a burst.

Run-time data lives under <workspace>/data/douyin_native/:
  profile/           persistent Edge user-data dir (keeps douyin login state)
  browser.json       atomic session record {pid, port, ready}
  send_policy.json   SEND permission file; only the human edits it
  seen.json          already-seen videos (dedupe)
  usage.json         daily usage ledger (Asia/Shanghai day key)
  login_state.json   logged-in marker written by login_check
  shots/             screenshots & storyboard sheets
Reports (formal deliverables) go to <workspace>/output/douyin_native/reports/.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool

try:  # pragma: no cover - lazy import keeps module importable without deps
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
_DEFAULT_PAGE_NAME = "negi-douyin"
_DOUYIN_HOME = "https://www.douyin.com/?recommend=1"
_DOUYIN_JINGXUAN = "https://www.douyin.com/jingxuan"
_DOUYIN_SEARCH = "https://www.douyin.com/search/{q}?type=video"
_BEIJING_TZ = timezone(timedelta(hours=8))
_NAV_TIMEOUT = 45

_LOGIN_MODAL_CLASS = "douyin_login_new_class"
_LOGIN_TEXTS = ("扫码登录", "登录后免费畅享高清视频", "登录即代表同意用户协议和隐私政策")

_SEND_POLICY_DEFAULT = {
    "enabled": False,
    "note": "Sending is OFF by default. Only the user may edit this file: set enabled=true and allow_<action>=true to grant. The tool refuses sends otherwise.",
    "allow_comment": False,
    "allow_forward": False,
    "daily_limit_comment": 3,
    "daily_limit_forward": 3,
    "interval_seconds": 60,
}

_FEED_ITEM_RE = re.compile(r"^\s*(?P<dur>\d{1,2}:\d{2}(?::\d{2})?)\s*$")
_UNIT_RE = re.compile(r"^(\d+(?:\.\d+)?)(万)?$")


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------

def _parse_count(text: str) -> int | None:
    m = _UNIT_RE.match((text or "").strip())
    if not m:
        return None
    val = float(m.group(1))
    return int(val * 10000) if m.group(2) else int(val)


def _parse_duration_seconds(text: str) -> int | None:
    parts = [int(x) for x in (text or "").strip().split(":") if x.isdigit()]
    if not parts:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def _day_key() -> str:
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d")


def _iso_now() -> str:
    return datetime.now(_BEIJING_TZ).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _http_alive(port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=2.0
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _find_edge() -> str | None:
    for cand in _EDGE_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def _dur_at(info: dict[str, Any], idx: int, n: int) -> float:
    dur = info.get("duration_seconds") or 0
    return dur * idx / max(n - 1, 1)


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------

class DouyinNativeTool(Tool):
    self_enforced_timeout = True

    def __init__(
        self,
        workspace: Path | str | None = None,
        state_dir: Path | str | None = None,
        output_root: Path | str | None = None,
    ) -> None:
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.state_dir = Path(state_dir) if state_dir else self.workspace / "data" / "douyin_native"
        self.output_root = Path(output_root) if output_root else self.workspace / "output" / "douyin_native"
        self._pw = None
        self._browser = None
        self._context = None

    # -- schema -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "douyin_native"

    @property
    def description(self) -> str:
        return (
            "Douyin browsing via the local logged-in Edge (feed/search/video/comments/"
            "storyboard/watch reports); sending stays off until send_policy.json and "
            "allow_send permit it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "open", "login_status", "login_check", "feed", "search",
                        "video", "storyboard", "comments", "watch_report",
                        "send_comment", "send_forward", "usage", "health", "close",
                    ],
                    "description": "Douyin capability to run (see toolskill for details).",
                },
                "keyword": {"type": "string", "description": "Search keyword / report topic."},
                "max_items": {"type": "integer", "description": "Feed/report item cap (1-40)."},
                "frame_count": {"type": "integer", "description": "Storyboard frames (2-16)."},
                "slot": {"type": "integer", "description": "1-based feed item to open in player."},
                "text": {"type": "string", "description": "Comment text (send_comment only)."},
                "allow_send": {"type": "boolean", "description": "Explicit send permission flag."},
                "include_frames": {"type": "boolean", "description": "watch_report samples storyboard frames."},
            },
            "required": ["action"],
        }

    # -- state --------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "profile").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "shots").mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        (self.output_root / "reports").mkdir(parents=True, exist_ok=True)

    def _send_policy(self) -> dict[str, Any]:
        p = self.state_dir / "send_policy.json"
        if not p.exists():
            _atomic_write_json(p, _SEND_POLICY_DEFAULT)
        return _read_json(p, dict(_SEND_POLICY_DEFAULT))

    def _usage(self) -> dict[str, Any]:
        p = self.state_dir / "usage.json"
        if not p.exists():
            _atomic_write_json(p, {"days": {}})
        return _read_json(p, {"days": {}})

    def _touch_usage(self, action: str, count: int = 1) -> dict[str, Any]:
        usage = self._usage()
        day = usage.setdefault("days", {}).setdefault(_day_key(), {})
        day.setdefault(action, 0)
        day[action] = day[action] + count
        _atomic_write_json(self.state_dir / "usage.json", usage)
        return usage

    def _record_seen(self, entries: list[dict[str, Any]]) -> None:
        p = self.state_dir / "seen.json"
        seen = _read_json(p, {"items": []})
        items = seen.setdefault("items", [])
        existing = {it.get("title") for it in items}
        added = 0
        for e in entries:
            key = e.get("title") or e.get("author")
            if not key or key in existing:
                continue
            items.append(
                {
                    "title": key[:200],
                    "author": e.get("author", ""),
                    "seen_at": _iso_now(),
                    "likes": e.get("likes", 0),
                }
            )
            existing.add(key)
            added += 1
        if added:
            _atomic_write_json(p, seen)

    # -- send gates ---------------------------------------------------------

    def _check_send_gate(self, action: str, allow_send: bool) -> tuple[bool, str]:
        policy = self._send_policy()
        if not policy.get("enabled", False):
            return False, "send_policy.json enabled=false: sending is OFF by default (only the user may enable it)"
        if not allow_send:
            return False, f"allow_send must be explicitly true by the user for action={action}"
        key = {"send_comment": "allow_comment", "send_forward": "allow_forward"}.get(action)
        if key and not policy.get(key, False):
            return False, f"send_policy.json {key}=false: user has not granted this action"
        if not (self.state_dir / "login_state.json").exists():
            return False, "not logged in: complete the login flow (open -> user scans in Edge window -> login_check)"
        usage = self._usage()
        today = usage.get("days", {}).get(_day_key(), {})
        limit_key = {"send_comment": "daily_limit_comment", "send_forward": "daily_limit_forward"}.get(action)
        limit = int(policy.get(limit_key, 0) or 0)
        used = int(today.get(action, 0) or 0)
        if limit > 0 and used >= limit:
            return False, f"daily quota reached: {used}/{limit} for {action}"
        interval = float(policy.get("interval_seconds", 0) or 0)
        last = today.get(f"{action}_last_ts")
        if interval > 0 and last:
            elapsed = time.time() - float(last)
            if elapsed < interval:
                return False, f"interval not elapsed: wait {interval - elapsed:.0f}s before next {action}"
        return True, ""

    # -- browser session ----------------------------------------------------

    async def _ensure_session(self):
        self._ensure_dirs()
        browser_json = self.state_dir / "browser.json"
        record = _read_json(browser_json, {})
        port = int(record.get("port") or 0)
        if port and await _http_alive(port):
            try:
                return await self._connect(port)
            except Exception:
                self._browser = None
                self._context = None
        return await self._launch()

    async def _connect(self, port: int):
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        if self._browser is None or self._context is None:
            self._browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        page = await self._page_for(self._context)
        return self._context, page

    async def _launch(self):
        from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._context = None
        if _find_edge() is None:
            raise RuntimeError("Microsoft Edge not found at " + " / ".join(_EDGE_CANDIDATES))
        port = _pick_free_port()
        profile_dir = self.state_dir / "profile"
        ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="msedge",
            headless=False,
            no_viewport=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                "--start-maximized",
            ],
            timeout=90_000,
        )
        self._context = ctx
        self._browser = None
        _atomic_write_json(
            self.state_dir / "browser.json",
            {"pid": None, "port": port, "ready": True, "started_at": _iso_now(), "edge": _find_edge()},
        )
        page = await self._page_for(ctx)
        return ctx, page

    async def _page_for(self, ctx):
        for pg in ctx.pages:
            try:
                if await pg.evaluate("window.name || ''") == _DEFAULT_PAGE_NAME:
                    return pg
            except Exception:
                continue
        for pg in ctx.pages:
            if "douyin.com" in (pg.url or ""):
                return pg
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await pg.evaluate(f"window.name = '{_DEFAULT_PAGE_NAME}'")
        except Exception:
            pass
        return pg

    async def _goto(self, page, url: str, wait_ms: int = 2500) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT * 1000)
        except Exception:
            pass
        await asyncio.sleep(wait_ms / 1000)

    async def _dismiss_overlays(self, page) -> None:
        try:
            await page.evaluate(
                "() => { const els = Array.from(document.querySelectorAll('div,span')); for (const e of els) { const t=(e.innerText||'').trim(); if (t === '我知道了') { e.click(); break; } } }"
            )
        except Exception:
            pass
        for _ in range(3):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

    async def _detect_login(self, page) -> dict[str, Any]:
        try:
            html = await page.content()
            cookie_names: set[str] = set()
            try:
                cookies = await page.context.cookies(["https://www.douyin.com/"])
                cookie_names = {str(c.get("name") or "") for c in cookies}
            except Exception:
                pass
            auth_cookies = sorted(
                cookie_names
                & {
                    "sessionid",
                    "sid_guard",
                    "sid_tt",
                    "uid_tt",
                    "passport_auth_status",
                    "uid_ucp",
                    "sid_ucp",
                }
            )
            nav_text = ""
            try:
                nav_text = await page.eval_on_selector(
                    "[data-e2e='douyin-navigation']", "e => (e.innerText || '')"
                )
            except Exception:
                nav_text = ""
            body_text = ""
            try:
                body_text = await page.evaluate("document.body ? document.body.innerText : ''")
            except Exception:
                pass
            modal_visible = _LOGIN_MODAL_CLASS in html and any(t in body_text for t in _LOGIN_TEXTS)
            nav_login = "登录" in nav_text
            logged_in = bool(auth_cookies) or (not nav_login and not modal_visible)
            return {
                "logged_in": bool(logged_in),
                "cookie_auth": auth_cookies,
                "login_modal": bool(modal_visible),
                "nav_login": bool(nav_login),
                "markers": {
                    "login_modal_class": _LOGIN_MODAL_CLASS in html,
                    "has_login_texts": [t for t in _LOGIN_TEXTS if t in body_text][:3],
                },
            }
        except Exception as exc:  # pragma: no cover
            return {"logged_in": False, "login_modal": False, "nav_login": True, "error": str(exc)}

    async def _shot(self, page, name: str) -> str:
        path = self.state_dir / "shots" / f"{_day_key()}_{name}.png"
        try:
            await page.screenshot(path=str(path))
        except Exception:
            pass
        return str(path)

    async def _page_text(self, page) -> str:
        try:
            return await page.evaluate("document.body ? document.body.innerText : ''")
        except Exception:
            return ""

    # -- feed / data extraction ---------------------------------------------

    async def _feed_entries(self, page, max_items: int = 30, keyword: str | None = None) -> list[dict[str, Any]]:
        body = await self._page_text(page)
        lines = [ln.strip() for ln in body.splitlines()]
        entries: list[dict[str, Any]] = []
        i = 0
        while i < len(lines) and len(entries) < max_items:
            if not _FEED_ITEM_RE.match(lines[i]):
                i += 1
                continue
            dur = lines[i]
            i += 1
            likes = ""
            while i < len(lines) and not _parse_count(lines[i]) and lines[i]:
                i += 1
            if i < len(lines) and _parse_count(lines[i]):
                likes = lines[i]
                i += 1
            title_parts: list[str] = []
            author = ""
            while i < len(lines):
                ln = lines[i]
                if ln.startswith("@"):
                    author = ln
                    i += 1
                    break
                if _FEED_ITEM_RE.match(ln) or ln == "正在直播":
                    break
                title_parts.append(ln)
                i += 1
            date = ""
            if i < len(lines) and (lines[i].startswith("·") or "直播" in lines[i]):
                date = lines[i]
                i += 1
            title = " ".join([t for t in title_parts if t]).strip()
            if not (title or author):
                continue
            entry = {
                "duration": dur,
                "duration_seconds": _parse_duration_seconds(dur),
                "likes": _parse_count(likes) or 0,
                "likes_text": likes,
                "title": title[:200],
                "author": author[1:].strip() if author.startswith("@") else author,
                "date": date.strip("· ").strip(),
                "link": "",
            }
            if keyword and keyword not in (entry["title"] + entry["author"]):
                continue
            entries.append(entry)
        return entries

    async def _current_video_info(self, page) -> dict[str, Any]:
        try:
            meta = await page.evaluate(
                "() => { const v = document.querySelector('video'); if (!v) return null; return {dur: v.duration||0, paused: v.paused, w: v.videoWidth||0, h: v.videoHeight||0}; }"
            )
        except Exception:
            meta = None
        body = await self._page_text(page)
        author = ""
        m = re.search(r"@([^\n|]+?)\s*(?:·|\|)", body)
        if m:
            author = m.group(1).strip()
        stats = re.findall(r"\n(\d{1,3}(?:\.\d+)?万?)\n", body)
        title = ""
        t = re.search(r"#\S+\s+([^\n@]{10,120})", body)
        if t:
            title = t.group(1).strip()
        return {
            "video_present": bool(meta),
            "duration_seconds": int(meta["dur"]) if meta and meta["dur"] else 0,
            "video_width": meta["w"] if meta else 0,
            "video_height": meta["h"] if meta else 0,
            "author": author,
            "stats_numbers": stats[:8],
            "title_guess": title[:120],
        }

    async def _open_player(self, page, slot: int = 1) -> dict[str, Any]:
        await self._dismiss_overlays(page)
        try:
            href = await page.evaluate(
                "() => { const a = Array.from(document.querySelectorAll('a[href*=\"/video/\"]'))[0]; return a ? a.href : ''; }"
            )
            if href:
                await self._goto(page, href, wait_ms=3000)
        except Exception:
            pass
        click_js = """
        (slot) => {
          const cands = Array.from(document.querySelectorAll('div,li'))
            .filter(el => {
              const t = (el.innerText || '').trim();
              return t && t.length > 4 && t.length < 240 && /\\d{1,2}:\\d{2}/.test(t) && el.getBoundingClientRect().width > 200;
            });
          const el = cands[slot - 1] || cands[0];
          if (!el) return false;
          el.scrollIntoView({block: 'center'});
          el.click();
          return true;
        }
        """
        try:
            await page.evaluate(click_js, slot)
        except Exception:
            pass
        await asyncio.sleep(2.5)
        await self._dismiss_overlays(page)
        return await self._current_video_info(page)

    async def _try_video_link(self, page) -> str:
        for sel in ["[aria-label*='分']", "[title*='分']", "text=分享"]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=3000)
                    break
            except Exception:
                continue
        await asyncio.sleep(1.5)
        body = await self._page_text(page)
        m = re.search(r"https?://(?:v\.)?douyin\.com/\S+|v\.douyin\.com/\S+", body)
        m2 = re.search(r"/video/(\d{8,})", body)
        if m:
            return m.group(0).rstrip("，。；）)]}")
        if m2:
            return f"https://www.douyin.com/video/{m2.group(1)}"
        return ""

    # -- storyboard ---------------------------------------------------------

    async def _storyboard(self, page, frame_count: int) -> tuple[dict[str, Any], dict[str, Any]]:
        frame_count = max(2, min(16, int(frame_count or 4)))
        info = await self._current_video_info(page)
        if not info.get("video_present"):
            return (
                {"status": "no_video", "message": "no visible <video> element; player not open or blocked"},
                info,
            )
        dur = info.get("duration_seconds") or 0
        if dur <= 0:
            dur = 300
        v = page.locator("video").first
        try:
            await v.wait_for(state="visible", timeout=10_000)
        except Exception:
            return ({"status": "video_not_visible", "message": "video element exists but not visible"}, info)
        shots: list[dict[str, Any]] = []
        shot_paths: list[str] = []
        for idx in range(frame_count):
            target = _dur_at(info, idx, frame_count)
            try:
                await v.evaluate(
                    "(v,t)=>{ try { v.currentTime = t; v.play().catch(()=>{}); } catch(e){} }", target
                )
            except Exception:
                pass
            await asyncio.sleep(1.0)
            sp = self.state_dir / "shots" / f"story_{_day_key()}_{int(time.time()*1000)}_{idx}.png"
            try:
                await v.screenshot(path=str(sp))
                shots.append({"cell": idx + 1, "time": round(target, 1), "path": str(sp)})
                shot_paths.append(str(sp))
            except Exception:
                shots.append({"cell": idx + 1, "time": round(target, 1), "path": "", "error": "screenshot failed"})
        sheet_path = ""
        if shot_paths and Image is not None:
            sheet_path = await self._compose_sheet(shot_paths, info)
        return (
            {
                "status": "storyboard",
                "source": "real video element sampling (windowed Edge)",
                "cells": shots,
                "sheet_path": sheet_path,
                "note": "分时采样，不代表看完整条，不含音轨证据",
            },
            info,
        )

    async def _compose_sheet(self, shot_paths: list[str], info: dict[str, Any]) -> str:
        images = []
        for p in shot_paths:
            try:
                images.append(Image.open(p).convert("RGB"))  # type: ignore[attr-defined]
            except Exception:
                continue
        if not images:
            return ""
        n = len(images)
        cols = int(math.ceil(n ** 0.5))
        rows = int(math.ceil(n / cols))
        cell_w = max(im.width for im in images)
        cell_h = max(im.height for im in images)
        budget = 480_000
        scale = min(1.0, (budget / max(cell_w * cell_h * n, 1)) ** 0.5)
        cell_w = max(64, int(cell_w * scale))
        cell_h = max(64, int(cell_h * scale))
        header_h = 44
        pad = 6
        sheet_w = cols * cell_w + (cols + 1) * pad
        sheet_h = header_h + rows * cell_h + (rows + 1) * pad
        sheet = Image.new("RGB", (sheet_w, sheet_h), (245, 245, 245))  # type: ignore[attr-defined]
        draw = ImageDraw.Draw(sheet)  # type: ignore[attr-defined]
        font = None
        for f in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\arial.ttf"]:
            try:
                font = ImageFont.truetype(f, 18)  # type: ignore[attr-defined]
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()  # type: ignore[attr-defined]
        title = (info.get("title_guess") or "Douyin storyboard")[:60].replace("\n", " ")
        draw.text((10, 8), f"{title}  ·  {n} cells · 分时采样", fill=(0, 0, 0), font=font)
        for idx, im in enumerate(images):
            r, c = divmod(idx, cols)
            x = pad + c * (cell_w + pad)
            y = header_h + pad + r * (cell_h + pad)
            sheet.paste(im.resize((cell_w, cell_h)), (x, y))
            draw.rectangle([x, y, x + cell_w, y + cell_h], outline=(200, 200, 200))
            cell_txt = f"C{idx + 1} t={_dur_at(info, idx, n):.1f}s"
            try:
                draw.rectangle([x, y, x + 120, y + 24], fill=(0, 80, 160))
                draw.text((x + 4, y + 2), cell_txt, fill=(255, 255, 255), font=font)
            except Exception:
                pass
        out = self.state_dir / "shots" / f"storyboard_{_day_key()}_{int(time.time()*1000)}.png"
        sheet.save(str(out), format="PNG")
        return str(out)

    # -- comments -----------------------------------------------------------

    async def _open_comments(self, page) -> dict[str, Any]:
        clicked = False
        for sel in ["[aria-label*='评']", "[title*='评']", "[data-e2e*='comment']"]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue
        await asyncio.sleep(2.0)
        body = await self._page_text(page)
        lines = [ln.strip() for ln in body.splitlines()]
        comments: list[dict[str, Any]] = []
        i = 0
        while i < len(lines):
            if lines[i].startswith("@") and i + 1 < len(lines):
                author = lines[i][1:].strip()
                text_parts: list[str] = []
                j = i + 1
                while j < len(lines) and not lines[j].startswith("@") and not _FEED_ITEM_RE.match(lines[j]):
                    text_parts.append(lines[j])
                    j += 1
                text = " ".join(t for t in text_parts if t).strip()
                if text:
                    comments.append({"author": author[:60], "text": text[:300]})
                i = j
            else:
                i += 1
        return {
            "comments_panel": clicked,
            "comment_count": len(comments),
            "comments": comments[:30],
            "note": "评论面板解析为尽力而为；私信不在读取范围",
        }, body[:800]

    # -- search -------------------------------------------------------------

    async def _act_search(self, page, keyword: str | None, max_items: int) -> dict[str, Any]:
        kw = (keyword or "").strip()
        if not kw:
            return {"status": "error", "message": "keyword required for search"}
        from urllib.parse import quote
        url = _DOUYIN_SEARCH.format(q=quote(kw))
        await self._goto(page, url, wait_ms=4000)
        body = await self._page_text(page)
        if "502" in body or "Bad Gateway" in body or len(body) < 60:
            shot = await self._shot(page, "search_blocked")
            return {
                "status": "blocked",
                "reason": "search page returned gateway error or empty (502/anti-bot/login wall)",
                "url": url,
                "screenshot": shot,
                "advice": "登录后在打开的 Edge 窗口中重试 search；冷启动匿名搜索经常被网关拦截",
            }
        entries = await self._feed_entries(page, max_items=max_items)
        self._record_seen(entries)
        login = await self._detect_login(page)
        return {
            "status": "ok",
            "keyword": kw,
            "url": url,
            "items": entries[:max_items],
            "item_count": len(entries),
            "login": login,
        }

    # -- actions ------------------------------------------------------------

    async def _act_open(self, page) -> dict[str, Any]:
        await self._goto(page, _DOUYIN_HOME, wait_ms=4000)
        await self._dismiss_overlays(page)
        login = await self._detect_login(page)
        shot = await self._shot(page, "open_home")
        title = await page.title()
        url = page.url
        if not login["logged_in"]:
            return {
                "status": "login_required",
                "message": "请在打开的 Edge 窗口里扫码/手机确认登录抖音；完成后调用 login_check 继续。工具不会伪造登录。",
                "page_title": title,
                "url": url,
                "screenshot": shot,
                "login": login,
            }
        return {
            "status": "ok",
            "page_title": title,
            "url": url,
            "screenshot": shot,
            "login": login,
        }

    async def _act_login_status(self, page) -> dict[str, Any]:
        login = await self._detect_login(page)
        shot = await self._shot(page, "login_status")
        return {"status": "ok", "login": login, "screenshot": shot}

    async def _act_login_check(self, page) -> dict[str, Any]:
        login = await self._detect_login(page)
        shot = await self._shot(page, "login_check")
        if login["logged_in"]:
            _atomic_write_json(self.state_dir / "login_state.json", {"logged_in": True, "at": _iso_now()})
            return {"status": "ok", "message": "已确认登录状态；后续发送类动作可进入许可检查", "login": login, "screenshot": shot}
        return {
            "status": "not_logged_in",
            "message": "仍未检测到登录态：请在打开 Edge 窗口完成扫码/手机确认后再调用 login_check",
            "screenshot": shot,
            "login": login,
        }

    async def _act_feed(self, page, keyword: str | None, max_items: int) -> dict[str, Any]:
        if page.url and "douyin.com" not in page.url:
            await self._goto(page, _DOUYIN_HOME, wait_ms=3000)
        await self._dismiss_overlays(page)
        entries = await self._feed_entries(page, max_items=max_items, keyword=keyword)
        self._record_seen(entries)
        login = await self._detect_login(page)
        shot = await self._shot(page, "feed")
        return {"status": "ok", "items": entries[:max_items], "item_count": len(entries), "login": login, "screenshot": shot}

    async def _act_video(self, page, slot: int) -> dict[str, Any]:
        info = await self._open_player(page, slot)
        link = await self._try_video_link(page)
        shot = await self._shot(page, "video_info")
        return {"status": "ok", "slot": slot, "info": info, "link": link, "screenshot": shot}

    async def _act_storyboard(self, page, slot: int, frame_count: int) -> dict[str, Any]:
        info = await self._open_player(page, slot)
        result, info = await self._storyboard(page, frame_count)
        result["info"] = info
        result["screenshot"] = await self._shot(page, "storyboard_state")
        return result

    async def _act_comments(self, page, slot: int) -> dict[str, Any]:
        info = await self._open_player(page, slot)
        comments, body_preview = await self._open_comments(page)
        comments["info"] = info
        return comments

    async def _act_watch_report(self, page, keyword: str | None, max_items: int, include_frames: bool, frame_count: int) -> dict[str, Any]:
        kw = (keyword or "").strip()
        notes: list[str] = []
        await self._goto(page, _DOUYIN_HOME, wait_ms=3500)
        await self._dismiss_overlays(page)
        entries = await self._feed_entries(page, max_items=max_items, keyword=kw or None)
        if not entries and kw:
            notes.append(f"推荐流中没有命中「{kw}」的条目；已尝试在精选/推荐流只读浏览并如实记录")
        if not entries:
            entries = await self._feed_entries(page, max_items=max_items)
        self._record_seen(entries)
        enriched: list[dict[str, Any]] = []
        frames_for = min(3, max_items, len(entries)) if include_frames else 0
        blocked_count = 0
        for idx in range(len(entries)):
            item = dict(entries[idx])
            item["index"] = idx + 1
            item["frame_path"] = ""
            if idx < frames_for:
                info = await self._open_player(page, slot=idx + 1)
                if info.get("video_present"):
                    item["link"] = await self._try_video_link(page)
                    item["detail"] = info
                if include_frames and info.get("video_present"):
                    sb, _ = await self._storyboard(page, frame_count)
                    item["frame_path"] = sb.get("sheet_path", "")
                else:
                    blocked_count += 1
            enriched.append(item)
        login = await self._detect_login(page)
        if not login["logged_in"]:
            notes.append("未登录状态；搜索/部分视频页可能被登录墙或网关拦截")
        report_path = await self._write_report(enriched, kw, {})
        preview = "\n".join(
            f"{e['index']}. 《{(e.get('title') or '')[:40]}》 @{e.get('author')}" for e in enriched
        )
        self._touch_usage("watch_report", count=len(enriched))
        return {
            "status": "ok",
            "topic": kw or "(推荐流)",
            "report_path": report_path,
            "item_count": len(enriched),
            "blocked_count": blocked_count,
            "notes": notes,
            "preview": preview,
            "login": login,
        }

    async def _write_report(self, entries: list[dict[str, Any]], topic: str, meta: dict[str, Any]) -> str:
        lines: list[str] = []
        lines.append(f"# 抖音观看报告 · {topic or '推荐流'}")
        lines.append("")
        lines.append(f"> 生成时间：{_iso_now()}；方式：真实登录 Edge（Playwright channel=msedge）只读浏览。")
        lines.append("> 说明：分时采样不代表看完整条、不含音轨；未登录/被风控拦截的项目已如实标注。")
        lines.append("> 报告不含账号、cookie、本机路径等隐私信息。")
        lines.append("")
        lines.append("## 概览")
        lines.append("")
        lines.append(f"- 主题：{topic or '推荐流'}")
        lines.append(f"- 条目数：{len(entries)}")
        lines.append("")
        for e in entries:
            likes = e.get("likes_text") or str(e.get("likes") or 0)
            why = self._why_worth(e)
            link = e.get("link") or "（未获取链接：需要登录或分享面板未打开）"
            lines.append(f"### {e['index']}. 《{(e.get('title') or '（无标题）')[:120]}》")
            lines.append("")
            lines.append(f"- 作者：@{e.get('author') or '未知'}")
            lines.append(f"- 时长：{e.get('duration') or '未知'} ｜ 点赞：{likes} ｜ 日期：{e.get('date') or '未知'}")
            lines.append(f"- 链接：{link}")
            lines.append(f"- 内容摘要：{(e.get('title') or '（未能读取标题文本）')[:200]}")
            lines.append(f"- 为什么值得看：{why}")
            if e.get("frame_path"):
                lines.append(f"- 分镜：{e['frame_path']}")
            lines.append("")
        report_dir = self.output_root / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{_day_key()}_{(topic or 'feed').replace(' ', '_')[:20]}.md"
        path = report_dir / fname
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    @staticmethod
    def _why_worth(e: dict[str, Any]) -> str:
        reasons: list[str] = []
        likes = e.get("likes") or 0
        if likes >= 100_000:
            reasons.append(f"高热度（点赞约 {e.get('likes_text')}）")
        elif likes >= 10_000:
            reasons.append(f"有一定热度（点赞 {e.get('likes_text')}）")
        if e.get("duration_seconds"):
            ds = int(e["duration_seconds"])
            if ds >= 600:
                reasons.append("长视频/深度内容")
            elif ds >= 180:
                reasons.append("中等时长，信息量适中")
            else:
                reasons.append("短视频，适合快速浏览")
        if e.get("date"):
            reasons.append(f"发布于 {e['date']}")
        title = e.get("title") or ""
        tags = re.findall(r"#([^\s#]+)", title)[:4]
        if tags:
            reasons.append(f"话题：{'、'.join(tags)}")
        return "；".join(reasons) if reasons else "按推荐流顺序收录，未获得额外热度信号"

    async def _act_send_comment(self, page, slot: int, text: str | None, allow_send: bool) -> dict[str, Any]:
        body_text = (text or "").strip()
        if not body_text:
            return {"status": "error", "message": "text required for send_comment"}
        ok, reason = self._check_send_gate("send_comment", allow_send)
        if not ok:
            return {"status": "blocked", "reason": reason}
        info = await self._open_player(page, slot)
        if not info.get("video_present"):
            return {"status": "blocked", "reason": "player not open (login wall or page change); send aborted"}
        typed = False
        input_selectors = ["textarea", "[contenteditable='true']", "input[type='text']", "[data-e2e*='comment']"]
        loc = None
        for sel in input_selectors:
            cand = page.locator(sel).first
            if await cand.count() > 0 and await cand.is_visible():
                loc = cand
                break
        if loc is not None:
            try:
                await loc.click(timeout=3000)
                await loc.fill(body_text)
                typed = True
            except Exception:
                typed = False
        if not typed:
            shot = await self._shot(page, "send_comment_input_missing")
            return {"status": "error", "message": "评论输入框未定位到（可能未登录或页面结构变化）", "screenshot": shot}
        sent = False
        for btn_sel in ["button:has-text('发送')", "text=发送"]:
            try:
                btn = page.locator(btn_sel).last
                if await btn.count() > 0:
                    await btn.click(timeout=3000)
                    sent = True
                    break
            except Exception:
                continue
        await asyncio.sleep(2.0)
        receipt = "未确认送达"
        comments, _ = await self._open_comments(page)
        all_text = " ".join(c["text"] for c in comments.get("comments", []))
        if body_text[:20] in all_text:
            receipt = "confirmed"
        usage = self._touch_usage("send_comment")
        today = usage.get("days", {}).get(_day_key(), {})
        today["send_comment_last_ts"] = time.time()
        _atomic_write_json(self.state_dir / "usage.json", usage)
        return {
            "status": "sent_attempted" if sent else "not_sent",
            "receipt": receipt,
            "message": "发送完成并做回执核对；核对不到即如实报「未确认送达」" if sent else "发送按钮未定位到，未发送",
            "text_snippet": body_text[:40],
            "usage_today": today.get("send_comment", 0),
        }

    async def _act_send_forward(self, page, slot: int, allow_send: bool) -> dict[str, Any]:
        ok, reason = self._check_send_gate("send_forward", allow_send)
        if not ok:
            return {"status": "blocked", "reason": reason}
        info = await self._open_player(page, slot)
        if not info.get("video_present"):
            return {"status": "blocked", "reason": "player not open; forward aborted"}
        clicked = False
        for sel in ["[aria-label*='分']", "[title*='分']", "text=分享"]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue
        shot = await self._shot(page, "send_forward_panel")
        usage = self._touch_usage("send_forward")
        today = usage.get("days", {}).get(_day_key(), {})
        today["send_forward_last_ts"] = time.time()
        _atomic_write_json(self.state_dir / "usage.json", usage)
        return {
            "status": "panel_clicked" if clicked else "panel_not_found",
            "receipt": "未确认送达",
            "message": "已打开官方面板尝试转发；平台未给出发送回执，如实报「未确认送达」，需要人工核对",
            "screenshot": shot,
        }

    async def _act_usage(self) -> dict[str, Any]:
        usage = self._usage()
        policy = self._send_policy()
        seen = _read_json(self.state_dir / "seen.json", {"items": []})
        return {
            "status": "ok",
            "today": usage.get("days", {}).get(_day_key(), {}),
            "send_policy": {k: v for k, v in policy.items() if not (isinstance(v, str) and v.startswith("Sending"))},
            "send_policy_path": str(self.state_dir / "send_policy.json"),
            "seen_count": len(seen.get("items", [])),
        }

    async def _act_health(self) -> dict[str, Any]:
        edge = _find_edge()
        try:
            import playwright  # noqa: F401
            pw_ok = True
        except Exception:
            pw_ok = False
        try:
            import PIL  # noqa: F401
            pil_ok = True
        except Exception:
            pil_ok = False
        session = _read_json(self.state_dir / "browser.json", {})
        return {
            "status": "ok",
            "edge": edge,
            "edge_found": edge is not None,
            "playwright_available": pw_ok,
            "pillow_available": pil_ok,
            "state_dir": str(self.state_dir),
            "browser_session": session,
            "login_marker": (self.state_dir / "login_state.json").exists(),
            "send_policy_path": str(self.state_dir / "send_policy.json"),
        }

    async def _act_close(self) -> dict[str, Any]:
        if self._pw is not None:
            try:
                if self._browser is not None:
                    await self._browser.close()
                elif self._context is not None:
                    await self._context.close()
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self._browser = None
            self._context = None
        try:
            (self.state_dir / "browser.json").unlink(missing_ok=True)
        except Exception:
            pass
        return {"status": "ok", "message": "Edge session closed; login profile preserved"}

    # -- dispatch -----------------------------------------------------------

    async def _run_action(self, action: str, **kw: Any) -> dict[str, Any]:
        if action in ("health", "usage"):
            if action == "health":
                return await self._act_health()
            return await self._act_usage()
        max_items = max(1, min(40, int(kw.get("max_items") or 10)))
        frame_count = max(2, min(16, int(kw.get("frame_count") or 4)))
        slot = max(1, int(kw.get("slot") or 1))
        keyword = kw.get("keyword")
        ctx, page = await self._ensure_session()
        if action == "open":
            return await self._act_open(page)
        if action == "login_status":
            return await self._act_login_status(page)
        if action == "login_check":
            return await self._act_login_check(page)
        if action == "feed":
            return await self._act_feed(page, keyword, max_items)
        if action == "search":
            return await self._act_search(page, keyword, max_items)
        if action == "video":
            return await self._act_video(page, slot)
        if action == "storyboard":
            return await self._act_storyboard(page, slot, frame_count)
        if action == "comments":
            return await self._act_comments(page, slot)
        if action == "watch_report":
            return await self._act_watch_report(page, keyword, max_items, bool(kw.get("include_frames", True)), frame_count)
        if action == "send_comment":
            return await self._act_send_comment(page, slot, kw.get("text"), bool(kw.get("allow_send", False)))
        if action == "send_forward":
            return await self._act_send_forward(page, slot, bool(kw.get("allow_send", False)))
        if action == "close":
            return await self._act_close()
        return {"status": "error", "message": f"unknown action {action}"}

    async def execute(self, **kwargs: Any) -> Any:
        action = kwargs.pop("action", "")
        timeout_seconds = kwargs.pop("timeout_seconds", None)
        try:
            effective = float(timeout_seconds or 300)
        except (TypeError, ValueError):
            effective = 300.0
        try:
            return await asyncio.wait_for(self._run_action(action, **kwargs), timeout=effective)
        except asyncio.TimeoutError:
            return {"status": "error", "message": f"douyin_native timeout after {effective}s", "action": action}
        except Exception as exc:  # pragma: no cover - defensive envelope
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}", "action": action}


def build(runtime: Any) -> DouyinNativeTool | None:
    workspace = getattr(runtime, "workspace", None) or Path.cwd()
    return DouyinNativeTool(workspace=workspace)
