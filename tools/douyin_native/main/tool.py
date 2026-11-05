"""douyin_native: native Douyin capability for Negi.

Fully independent of DSH (no dsh, cordis, DSH profile, @deepseek-ai/* runtime).
Drives the local real windowed Microsoft Edge through Playwright over CDP
(channel="msedge", persistent user-data dir). Read-only first; sending
(comment / reply / forward) is OFF by default and gated by an explicit
user-edited policy file plus allow_send=true, quotas, interval and a
post-send receipt check that reports "未确认送达" when it cannot verify.

Login guidance is a runtime step: when not logged in, actions return a
screenshot and ask the human to scan/confirm in the open Edge window; the tool
never fakes login. Anti-bot walls (captcha / login wall / rate limit) are
screenshotted and reported honestly, never retried in a burst.

Login state durability (see ``login_state.json``):
  Douyin's ``sessionid`` is a session cookie. Chromium does not persist those
  to disk on an abnormal shutdown, and the old code relaunched a brand new
  browser session for every action, so a scan-to-login could disappear between
  calls. We therefore (a) keep one long-lived Edge process addressed over CDP
  and recorded in ``browser.json`` ({pid, port}), (b) export the douyin cookies
  to ``login_state.json`` right after a *verified* login and lock the file down
  with ``icacls`` to the current user only, and (c) re-inject them with
  ``context.add_cookies`` whenever the live context has no auth cookie.

Login detection is multi-signal, never cookie-name-only:
  UI (header 登录 button / avatar / login modal) + server API
  (``/passport/web/account/info/``) + cookie presence. The server verdict and
  the UI win over cookie names, and every result carries ``logged_in``,
  ``cookie_valid`` and ``basis`` so "UI shows 未登录 but reports true" cannot
  happen again.

Run-time data lives under <workspace>/data/douyin_native/:
  profile/           persistent Edge user-data dir (keeps douyin login state)
  browser.json       session record {pid, port, ready, started_at}
  login_state.json   exported douyin cookies (ACL: current user only)
  send_policy.json   SEND permission file; only the human edits it
  seen.json          already-seen videos (dedupe)
  feed_snapshot.json slot -> aweme_id snapshot written by feed/watch_report; every
                     slot-based action resolves its target from it and verifies the
                     opened video really is that aweme (no silent video swapping)
  usage.json         daily usage ledger (Asia/Shanghai day key)
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
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from g3ku.agent.tools.base import Tool

_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))

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
_DOUYIN_SEARCH_GENERAL = "https://www.douyin.com/search/{q}"
_DOUYIN_AISEARCH = "https://www.douyin.com/aisearch?keyword={q}"
_DOUYIN_VIDEO = "https://www.douyin.com/video/{vid}"
_DOUYIN_USER_SELF = "https://www.douyin.com/user/self"
_DOUYIN_ACCOUNT_INFO_PAGE = "https://www.douyin.com/user/self"
_DOUYIN_CREATOR_UPLOAD = "https://creator.douyin.com/creator-micro/content/upload"
_DOUYIN_CREATOR_POST_VIDEO = "https://creator.douyin.com/creator-micro/content/post/video"
_DOUYIN_CREATOR_MANAGE = "https://creator.douyin.com/creator-micro/content/manage"
_SECOND_VERIFY_SELECTORS = ("#uc-second-verify", ".uc-second-verify", "[class*=\"second-verify\"]")
_TITLE_INPUT_SELECTOR = 'input[placeholder*="作品标题"]'
_DESC_EDITOR_SELECTOR = 'div.zone-container[contenteditable="true"]'
_ACCOUNT_INFO_URL = "https://www.douyin.com/passport/web/account/info/"
_BEIJING_TZ = timezone(timedelta(hours=8))
_NAV_TIMEOUT = 45
_CDP_READY_TIMEOUT = 40.0

_LOGIN_MODAL_CLASS = "douyin_login_new_class"
_LOGIN_TEXTS = ("扫码登录", "登录后免费畅享高清视频", "登录即代表同意用户协议和隐私政策")

# Cookies that actually carry the douyin login session.
_LOGIN_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
    "sid_ucp_v1",
    "ssid_ucp_v1",
    "ttwid",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "passport_auth_mix_state",
    "passport_mfa_token",
    "login_time",
    "csrf_session_id",
    "d_ticket",
    "n_mh",
    "odin_tt",
    "s_v_web_id",
    "session_tlb_tag",
    "web_sign_token",
    "x_tt_token",
    "__ac_nonce",
    "__ac_signature",
}

_SEND_POLICY_DEFAULT = {
    "enabled": False,
    "note": (
        "Sending is OFF by default. Only the user may edit this file: set enabled=true and "
        "allow_<action>=true to grant. The tool refuses sends otherwise. allow_publish only "
        "covers mode=publish (a REAL public post); mode=draft (default) needs enabled + "
        "allow_send but never allow_publish."
    ),
    "allow_comment": False,
    "allow_reply": False,
    "allow_forward": False,
    "allow_publish": False,
    "daily_limit_comment": 3,
    "daily_limit_reply": 3,
    "daily_limit_forward": 3,
    "daily_limit_publish": 3,
    "interval_seconds": 60,
}

_POLICY_KEY = {
    "send_comment": "allow_comment",
    "reply_to_comment": "allow_reply",
    "send_forward": "allow_forward",
    "publish": "allow_publish",
}
_LIMIT_KEY = {
    "send_comment": "daily_limit_comment",
    "reply_to_comment": "daily_limit_reply",
    "send_forward": "daily_limit_forward",
    "publish": "daily_limit_publish",
}

_FEED_ITEM_RE = re.compile(r"^\s*(?P<dur>\d{1,2}:\d{2}(?::\d{2})?)\s*$")
_UNIT_RE = re.compile(r"^(\d+(?:\.\d+)?)(万)?$")
_TIME_HINT_RE = re.compile(
    r"(刚刚|\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前|\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日|昨天|前天)"
)
_ACTION_WORDS = {"分享", "回复", "举报", "删除", "置顶", "作者赞过", "...", "翻译", "收起翻译", "更多", "收起"}
_LOGIN_WALL_TEXTS = (
    "登录后即可搜索",
    "扫码登录",
    "验证码登录",
    "密码登录",
    "登录即代表同意",
    "登录后免费畅享",
)

# -- in-page JS -------------------------------------------------------------

_JS_LOGIN_UI = r"""
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const out = {login_btn: null, avatar: null, avatar_src: '', nickname: '', avatar_probe: [], modal: false,
               title: document.title, url: location.href, innerWidth: window.innerWidth || 0};
  const vw = window.innerWidth || 1280;
  const rightEdge = vw * 0.45;
  for (const el of Array.from(document.querySelectorAll('div,span,button,a,li,p'))) {
    if (el.children.length > 0) continue;
    const t = (el.innerText || '').trim();
    if (t !== '登录' && t !== '登录/注册') continue;
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.top > 140 || r.left < rightEdge) continue;
    out.login_btn = {text: t, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
    break;
  }
  const avatarSels = ['img[class*="avatar"]', '[data-e2e="user-avatar"] img', 'header a[href*="/user/"] img', '[class*="avatar"] img'];
  for (const sel of avatarSels) {
    for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 8)) {
      if (!vis(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.top > 160 || r.left < rightEdge) continue;
      out.avatar = {sel, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
      out.avatar_src = (el.getAttribute('src') || '').slice(0, 240);
      // nickname candidate must come from THIS header avatar (top-right), never from a
      // video author link: measured 2026-11-01 a loose `header a[href*="/user/"]` lookup
      // returned the video author's name ("小星科普宇宙") on a /video/<id> page.
      {
        const ael = el.closest('a');
        const cand = ((ael && (ael.innerText || ael.getAttribute('title'))) || el.getAttribute('alt') || '').trim();
        if (cand && cand !== '登录' && cand !== '登录/注册' && !/^[0-9.,万wW]+$/.test(cand)) out.nickname = cand;
      }
      break;
    }
    if (out.avatar) break;
  }
  const bodyText = document.body ? document.body.innerText : '';
  const html = document.documentElement.outerHTML || '';
  out.modal = html.includes('douyin_login_new_class') &&
    /扫码登录|验证码登录|密码登录|登录即代表同意/.test(bodyText.slice(0, 4000));
  out.login_wall_text = /登录后即可搜索|登录查看更多|登录后观看/.test(bodyText.slice(0, 4000));
  return out;
}
"""

_JS_ACCOUNT_INFO = r"""
async () => {
  const out = {endpoint: 'https://www.douyin.com/passport/web/account/info/'};
  try {
    const r = await fetch(out.endpoint, {credentials: 'include'});
    out.status = r.status;
    const txt = await r.text();
    let j = null;
    try { j = JSON.parse(txt); } catch (e) { j = null; }
    if (j && j.data) {
      out.error_code = j.data.error_code === undefined ? null : Number(j.data.error_code);
      out.description = String(j.data.description || '');
      out.has_uid = Boolean(j.data.user_id || j.data.uid || j.data.user_uid || j.data.username);
      out.ok = out.error_code === 0;
    } else {
      out.ok = false;
      out.error_code = -1;
      out.description = (j && (j.message || '')) || txt.slice(0, 200);
    }
  } catch (e) {
    out.error = String(e);
  }
  return out;
}
"""

_JS_CARDS = r"""
() => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('[data-aweme-id]')).map((el, i) => {
    const vid = el.getAttribute('data-aweme-id') || '';
    const a = el.querySelector('a[href*="/video/"]');
    let href = a ? a.getAttribute('href') || '' : '';
    if (!href && vid) href = '/video/' + vid;
    if (href.startsWith('//')) href = 'https:' + href;
    else if (href.startsWith('/')) href = 'https://www.douyin.com' + href;
    const img = el.querySelector('img');
    const lines = (el.innerText || '').split('\n').map(clean).filter(Boolean);
    const dur = lines.find((t) => /^\d{1,2}:\d{2}(?::\d{2})?$/.test(t)) || '';
    const likesIdx = lines.findIndex((t) => /^\d+(\.\d+)?\s*(万|w|W)?$/.test(t) && t !== dur);
    const authorLine = lines.find((t) => t.startsWith('@')) || '';
    const dateLine = lines.find((t) => t.startsWith('·') || /^·/.test(t)) || '';
    return {
      index: i + 1,
      aweme_id: vid,
      link: href,
      duration: dur,
      likes_text: likesIdx >= 0 ? lines[likesIdx] : '',
      author: authorLine.replace(/^@/, '').trim(),
      title: clean(img ? (img.getAttribute('alt') || '') : ''),
      date: dateLine.replace(/^·/, '').trim(),
      lines: lines.slice(0, 12)
    };
  });
}
"""

# Video detail reader. The douyin /video/<id> page is a client-rendered shell at
# DOMContentLoaded: a single immediate read legitimately sees NO detail panel and
# NO <video> element, which is how every field used to come back empty
# ("video_present=true but author/title/duration/likes/publish_time all blank").
# So this reader (a) pulls every field from where it actually lives, (b) reports
# the source of each field, and (c) lets the Python side poll it until ready.
# Field sources (measured 2026-11-01 on www.douyin.com/video/<id>):
#   author        [data-e2e="user-info"] first line, minus 粉丝/获赞/关注 stats
#   title         document.title minus " - 抖音" (fallback: detail panel desc)
#   detail_text   [data-e2e="detail-video-info"] innerText (also carries
#                 data-e2e-aweme-id, the identity anchor for the open video)
#   likes         [data-e2e="video-player-digg"] text ("3.7万"), fallback = the
#                 first numeric stat line of the detail panel
#   publish_time  [data-e2e="detail-video-publish-time"] ("发布时间：2026-10-31 18:51")
#   duration      video.duration when metadata is loaded, else the player clock
#                 text "00:03 / 44:57" from [data-e2e="player-container"] — this is
#                 the path that keeps working while paused / not yet played
_JS_VIDEO_INFO = r"""
() => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  const txt = (sel) => { const el = document.querySelector(sel); return el ? clean(el.innerText) : ''; };
  const num = (t) => {
    const m = (t || '').match(/(\d+(?:\.\d+)?)\s*(万|w|W)?/);
    if (!m) return 0;
    return Math.round(parseFloat(m[1]) * (m[2] ? 10000 : 1));
  };
  const v = document.querySelector('video');

  // identity of the video actually mounted on this page
  const detailEl = document.querySelector('[data-e2e="detail-video-info"]');
  const panelAweme = detailEl ? (detailEl.getAttribute('data-e2e-aweme-id') || '') : '';
  const playerEl = document.querySelector('[data-e2e="player-container"]');
  const playerCls = (playerEl && typeof playerEl.className === 'string') ? playerEl.className : '';
  const playerAweme = (playerCls.match(/video_(\d{8,})/) || [])[1] || '';
  const urlAweme = (location.pathname.match(/\/video\/(\d{8,})/) || [])[1] || '';
  const aweme_id = panelAweme || playerAweme || urlAweme;

  // detail panel: split description lines away from stat/action lines
  const rawDetail = detailEl ? clean(detailEl.innerText) : '';
  const rawLines = detailEl ? (detailEl.innerText || '').split('\n').map(clean).filter(Boolean) : [];
  const COUNT_RE = /^\d+(?:\.\d+)?\s*(?:万|w|W)?$/;
  const descLines = [];
  const counts = [];
  for (const t of rawLines) {
    if (/^展开$|^收起$|^举报$|^发布时间[：:]/.test(t)) continue;
    if (COUNT_RE.test(t)) { counts.push(t); continue; }
    descLines.push(t);
  }
  const desc = descLines.join(' ').trim();

  let title = (document.title || '').replace(/\s*-\s*抖音\s*$/, '').trim();
  let title_source = 'document.title';
  if (!title || title === '抖音') {
    title = desc.slice(0, 200);
    title_source = 'detail-video-info desc';
  }
  if (!title) title_source = '';

  const userTxt = txt('[data-e2e="user-info"]');
  let author = (userTxt.split('\n')[0] || '').split(/粉丝|获赞|关注/)[0].trim().slice(0, 60);
  let author_source = author ? 'data-e2e=user-info' : '';
  if (!author) {
    const a = document.querySelector('[data-e2e="video-detail"] a[href*="/user/"]');
    author = a ? clean(a.innerText).split('\n')[0].slice(0, 60) : '';
    author_source = author ? 'video-detail a[href*=user]' : '';
  }

  const diggTxt = txt('[data-e2e="video-player-digg"]');
  let likes = num(diggTxt);
  let likes_source = likes ? 'data-e2e=video-player-digg' : '';
  if (!likes && counts.length) {
    likes = num(counts[0]);
    likes_source = likes ? 'detail panel stat #1' : '';
  }

  let publish_time = txt('[data-e2e="detail-video-publish-time"]')
    .replace(/^发布时间[：:]\s*/, '').trim();
  let publish_time_source = publish_time ? 'data-e2e=detail-video-publish-time' : '';
  if (!publish_time && detailEl) {
    const mm = (detailEl.innerText || '').match(/发布时间[：:]\s*([0-9\-: ]+)/);
    if (mm) { publish_time = clean(mm[1]); publish_time_source = 'detail panel text'; }
  }

  let duration_seconds = 0;
  let duration_source = '';
  if (v && isFinite(v.duration) && v.duration > 0) {
    duration_seconds = Math.round(v.duration);
    duration_source = 'video.duration';
  } else {
    const ptxt = txt('[data-e2e="player-container"]') || txt('.xgplayer');
    const dm = ptxt.match(/\d{1,2}:\d{2}(?::\d{2})?\s*\/\s*(\d{1,2}:\d{2}(?::\d{2})?)/) ||
               ptxt.match(/\/\s*(\d{1,2}:\d{2}(?::\d{2})?)/);
    if (dm) {
      let secs = 0;
      for (const p of dm[1].split(':')) {
        const n = parseInt(p, 10);
        secs = secs * 60 + (isNaN(n) ? 0 : n);
      }
      if (secs > 0) { duration_seconds = secs; duration_source = 'player clock text'; }
    }
  }

  return {
    video_present: !!v,
    duration_seconds,
    duration_source,
    video_width: v ? v.videoWidth || 0 : 0,
    video_height: v ? v.videoHeight || 0 : 0,
    paused: v ? !!v.paused : null,
    ready_state: v ? v.readyState : -1,
    author,
    author_source,
    likes,
    likes_text: diggTxt || (counts[0] || ''),
    likes_source,
    publish_time,
    publish_time_source,
    detail_text: rawDetail.slice(0, 400),
    desc: desc.slice(0, 400),
    detail_panel_present: !!detailEl,
    detail_panel_chars: rawDetail.length,
    title,
    title_source,
    aweme_id,
    aweme_identity: {panel: panelAweme, player_class: playerAweme, url: urlAweme},
    url: location.href
  };
}
"""

# Cheap identity read used for slot -> aweme verification loops.
_JS_AWEME_ID = r"""
() => {
  const detailEl = document.querySelector('[data-e2e="detail-video-info"]');
  const playerEl = document.querySelector('[data-e2e="player-container"]');
  const playerCls = (playerEl && typeof playerEl.className === 'string') ? playerEl.className : '';
  const panel = detailEl ? (detailEl.getAttribute('data-e2e-aweme-id') || '') : '';
  const player = (playerCls.match(/video_(\d{8,})/) || [])[1] || '';
  const url = (location.pathname.match(/\/video\/(\d{8,})/) || [])[1] || '';
  // Only DOM-mounted identity (detail panel attr / player-container class) proves
  // which video is really open. The URL alone is NOT proof: douyin leaves the
  // /video/<id> path in place on its error / not-found pages, so a url-only hit is
  // flagged for the Python side to treat as unverified instead of silently matching.
  const vals = [panel, player].filter(Boolean);
  return {
    panel,
    player_class: player,
    url_id: url,
    actual: panel || player || '',
    url_only: !(panel || player) && !!url,
    candidates: Array.from(new Set(vals)),
    conflict: new Set(vals).size > 1
  };
}
"""

# Like (digg) button state: douyin marks the button with
# data-e2e-state="video-player-digged" when the current user liked it and
# "video-player-no-digged" otherwise; innerText carries the raw like count.
_JS_LIKE_STATE = r"""
() => {
  const sel = '[data-e2e="video-player-digg"]';
  const el = document.querySelector(sel);
  if (!el) return {present: false, selector: sel};
  const st = el.getAttribute('data-e2e-state') || '';
  const liked = st === 'video-player-digged' || (/digged/i.test(st) && !/no-digged/i.test(st));
  const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
  const m = txt.match(/(\d+(?:\.\d+)?)\s*(万|w|W)?/);
  let count = 0;
  if (m) count = Math.round(parseFloat(m[1]) * (m[2] ? 10000 : 1));
  return {
    present: true,
    selector: sel,
    liked,
    state: st || '',
    count,
    count_text: txt,
    aria: el.getAttribute('aria-label') || '',
    cls: typeof el.className === 'string' ? el.className : ''
  };
}
"""

_JS_LIKE_CLICK = r"""
() => {
  const el = document.querySelector('[data-e2e="video-player-digg"]');
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const opts = {
    bubbles: true, cancelable: true, view: window, button: 0,
    clientX: r.left + r.width / 2, clientY: r.top + r.height / 2
  };
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.dispatchEvent(new MouseEvent('click', opts));
  return true;
}
"""

_JS_PANEL_STATE = r"""
() => {
  const list = document.querySelector('[data-e2e="comment-list"]');
  const items = document.querySelectorAll('[data-e2e="comment-item"]');
  const editors = document.querySelectorAll('.comment-input-container [contenteditable="true"], [data-e2e="comment-input"]');
  const body = document.body ? document.body.innerText : '';
  return {
    comment_list_present: !!list,
    comment_item_count: items.length,
    editor_present: editors.length > 0,
    login_gate_input: /登录后|扫码登录|立即登录/.test((list ? list.innerText : '').slice(0, 800)) ||
                      document.querySelectorAll('.comment-input-un-login-container').length > 0,
    title_text: list ? (list.innerText || '').split('\n')[0].slice(0, 40) : '',
    url: location.href
  };
}
"""

_JS_COMMENTS = r"""
() => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  const TIME_RE = /(刚刚|\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前|\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日|昨天|前天)/;
  const ACTION = new Set(['分享', '回复', '举报', '删除', '置顶', '作者赞过', '...', '翻译', '收起翻译', '更多', '收起', '展开']);
  const inside = (el, root) => { let c = el; while (c) { if (c === root) return true; c = c.parentElement; } return false; };
  const items = Array.from(document.querySelectorAll('[data-e2e="comment-item"]'));
  const out = items.map((it, idx) => {
    const info = it.querySelector('.comment-item-info-wrap');
    const stats = it.querySelector('.comment-item-stats-container');
    const more = it.querySelector('[data-e2e="video-comment-more"]');
    const leaves = [];
    for (const el of Array.from(it.querySelectorAll('span,div,p'))) {
      if (el.children.length > 0) continue;
      const t = clean(el.innerText);
      if (!t) continue;
      leaves.push({t, el});
    }
    const author = info ? clean(info.innerText).split('\n')[0] : '';
    const content = [];
    let time = '';
    for (const {t, el} of leaves) {
      if (info && inside(el, info)) continue;
      if (stats && inside(el, stats)) continue;
      if (more && inside(el, more)) continue;
      if (ACTION.has(t)) continue;
      if (/^\d+(\.\d+)?\s*(万|w|W)?$/.test(t)) continue;
      if (TIME_RE.test(t) && t.length <= 18) { if (!time) time = t; continue; }
      if (/^展开\d+条回复$/.test(t)) continue;
      content.push(t);
    }
    let likes = 0;
    if (stats) {
      const m = clean(stats.innerText).match(/(\d+(?:\.\d+)?)\s*(万)?/);
      if (m) likes = Math.round(parseFloat(m[1]) * (m[2] ? 10000 : 1));
    }
    let commentId = '';
    if (more) {
      const idEl = more.querySelector('[id]');
      const idAttr = idEl ? idEl.getAttribute('id') || '' : '';
      const m = idAttr.match(/tooltip_(\d+)/) || (more.innerText || '').match(/(\d{15,})/);
      if (m) commentId = m[1];
    }
    const replyCountMatch = clean(it.innerText).match(/展开\s*(\d+)\s*条回复/);
    return {
      index: idx + 1,
      raw_index: idx + 1,
      author: author.slice(0, 60),
      text: content.join(' ').slice(0, 400),
      likes,
      time,
      comment_id: commentId,
      reply_count: replyCountMatch ? parseInt(replyCountMatch[1], 10) : 0
    };
  });
  const list = document.querySelector('[data-e2e="comment-list"]');
  return {
    items: out.filter((x) => x.text && !TIME_RE.test(x.text.slice(0, 18))),
    raw_count: out.length,
    comment_list_present: !!list,
    title_text: list ? clean((list.innerText || '').split('\n')[0]).slice(0, 40) : ''
  };
}
"""

_JS_SCROLL_COMMENTS = r"""
() => {
  const items = document.querySelectorAll('[data-e2e="comment-item"]');
  const last = items[items.length - 1];
  if (last) { last.scrollIntoView({block: 'end'}); }
  const list = document.querySelector('[data-e2e="comment-list"]');
  if (list) {
    let sc = list;
    while (sc && sc.scrollHeight <= sc.clientHeight + 4 && sc.parentElement) sc = sc.parentElement;
    if (sc) sc.scrollTop = sc.scrollHeight;
  }
  return items.length;
}
"""

_JS_SCROLL_FEED = r"""
() => {
  window.scrollBy(0, Math.round(window.innerHeight * 0.9));
  return document.querySelectorAll('[data-aweme-id]').length;
}
"""

_JS_CLICK_REPLY = r"""
(idx) => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').trim();
  const items = Array.from(document.querySelectorAll('[data-e2e="comment-item"]'));
  const it = items[idx];
  if (!it) return {ok: false, reason: 'comment item #' + (idx + 1) + ' not found'};
  it.scrollIntoView({block: 'center'});
  const cands = Array.from(it.querySelectorAll('span,div,a,button'))
    .filter((el) => el.children.length === 0 && clean(el.innerText) === '回复');
  if (!cands.length) return {ok: false, reason: 'reply trigger not found inside comment item'};
  cands[0].click();
  return {ok: true};
}
"""

_JS_TEXT_PRESENT = r"""
async (needle) => {
  const body = document.body ? document.body.innerText : '';
  return body.includes(needle);
}
"""

# -- notification ("互动消息") dropdown, measured 2026-11-02 on www.douyin.com/user/self --
#
# Where it lives (page-level facts, not guesses):
#   * /user/self renders the *rich* www header: `#douyin-header` contains
#     `#douyin-header-menuCt` whose icon row reads 「搜索 充钻石 客户端 壁纸 通知 消息 投稿」。
#     (The bare feed pages /?recommend=1 and /jingxuan render the slim nav-only header,
#     which has NO 通知 icon — so interactions must land on /user/self first.)
#   * The 通知 entry is `[data-e2e="something-button"]` whose visible text is 通知; clicking it
#     renders a dropdown INSIDE the same `#douyin-header-menuCt` container.
#   * The dropdown's first line is 「互动消息」+「全部消息」; expanding shows a tab row
#     「粉丝 | @我的 | 评论 | 赞 | 推荐」and then one row per notification. A row is
#     avatar(s) + nickname + action text (「赞了你的作品」「赞了你的评论」「回复了你的评论」…) + time
#     (「刚刚」/「N分钟前」/「N小时前」/「昨天」/「N天前」/「M-D」).
#   * Row / list class names are build hashes (e.g. `yBIrNfj_`, `Vdu9fhYD`) and MUST NOT be
#     used as anchors. This reader therefore anchors on: the stable id `#douyin-header-menuCt`,
#     visible text (通知 / 互动消息 / tab labels / action wording / time wording) and structure
#     (a scroll list whose children repeat with a similar height).
_NOTIFY_ACTION_RE_SRC = (
    r"(?:赞了你(?:的作品|的评论|的视频)?|回复了你的评论|回复了你|评论了你的作品|评论了你|"
    r"@了你|提到了你|关注了你|收藏了你的作品|转发了你的作品|分享了你)"
)
_JS_NOTIFY_PANEL = r"""
() => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  const ACT = new RegExp('(?:赞了你(?:的作品|的评论|的视频)?|回复了你的评论|回复了你|评论了你的作品|评论了你|@了你|提到了你|关注了你|收藏了你的作品|转发了你的作品|分享了你)');
  const rect = (el) => { const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
  const menu = document.querySelector('#douyin-header-menuCt');
  const scope = menu || document.body;
  if (!scope) return {found: false, reason: 'no_scope'};
  // panel container = smallest element that mentions 互动消息 AND carries at least one row action
  let root = null, best = Infinity;
  for (const el of Array.from(scope.querySelectorAll('*'))) {
    const tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'SVG' || tag === 'PATH') continue;
    const t = clean(el.innerText);
    if (!t.includes('互动消息')) continue;
    if (!ACT.test(t)) continue;
    const n = el.querySelectorAll('*').length;
    if (n > 2 && n < best) { best = n; root = el; }
  }
  if (!root) return {found: false, reason: 'panel_not_rendered'};
  // tab row (text anchors only)
  const TABS = ['全部消息', '粉丝', '@我的', '评论', '赞', '推荐', '更多'];
  const tabs = [];
  for (const el of Array.from(root.querySelectorAll('div,span,a,li'))) {
    const t = clean(el.innerText);
    if (!TABS.includes(t)) continue;
    if (el.querySelectorAll('*').length > 2) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    tabs.push({text: t, ...rect(el)});
  }
  // scroll list = container whose children repeat with a similar height and hold row actions
  let listEl = null, rows = [], bestScore = -1;
  for (const cand of Array.from(root.querySelectorAll('div,ul'))) {
    const kids = Array.from(cand.children).filter(k => k.getBoundingClientRect().height >= 28);
    if (kids.length < 2) continue;
    const hs = kids.map(k => Math.round(k.getBoundingClientRect().height));
    const avg = hs.reduce((a, b) => a + b, 0) / hs.length;
    const similar = hs.filter(h => Math.abs(h - avg) <= 16).length;
    if (similar < Math.max(2, Math.floor(kids.length * 0.6))) continue;
    const score = similar * 10 + kids.length + (ACT.test(clean(cand.innerText)) ? 30 : 0);
    if (score > bestScore) { bestScore = score; listEl = cand; rows = kids; }
  }
  const rowOut = rows.map((k, i) => {
    const lines = [];
    for (const lf of Array.from(k.querySelectorAll('*'))) {
      if (lf.children.length > 0) continue;
      const t = clean(lf.innerText);
      if (!t) continue;
      if (lines[lines.length - 1] === t) continue;
      lines.push(t.slice(0, 120));
    }
    const hrefs = Array.from(k.querySelectorAll('a[href]'))
      .map(a => a.getAttribute('href') || '').filter(Boolean).slice(0, 6);
    const imgs = Array.from(k.querySelectorAll('img'))
      .map(im => (im.getAttribute('src') || '')).filter(Boolean).slice(0, 6);
    return {index: i + 1, lines: lines.slice(0, 14), hrefs, imgs, rect: rect(k)};
  });
  return {
    found: true,
    reason: '',
    panel: rect(root),
    url: location.href,
    panel_text: clean(root.innerText).slice(0, 1500),
    tabs: tabs.slice(0, 12),
    list_present: !!listEl,
    list_class_prefix: (listEl && typeof listEl.className === 'string') ? (listEl.className.split(' ')[0] || '') : '',
    row_count: rowOut.length,
    rows: rowOut.slice(0, 40),
    row_html_sample: rows.length ? rows[0].outerHTML.slice(0, 1200) : '',
  };
}
"""

_JS_NOTIFY_TAB = r"""
(tabText) => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  const menu = document.querySelector('#douyin-header-menuCt') || document.body;
  if (!menu) return {ok: false, reason: 'no_scope'};
  for (const el of Array.from(menu.querySelectorAll('div,span,a,li'))) {
    if (clean(el.innerText) !== tabText) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    // climb to the outermost node whose whole text is exactly this label (the real click target)
    let target = el;
    for (let i = 0; i < 3; i++) {
      const p = target.parentElement;
      if (!p || clean(p.innerText) !== tabText) break;
      target = p;
    }
    try { target.scrollIntoView({block: 'center'}); } catch (e) {}
    target.click();
    return {ok: true, text: tabText, tag: target.tagName.toLowerCase()};
  }
  return {ok: false, reason: 'tab_not_found:' + tabText};
}
"""

_JS_NOTIFY_ROW_POINT = r"""
(payload) => {
  const index = (payload && payload.index) || 0;
  const preferVideo = !!(payload && payload.preferVideo);
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  const ACT = new RegExp('(?:赞了你(?:的作品|的评论|的视频)?|回复了你的评论|回复了你|评论了你的作品|评论了你|@了你|提到了你|关注了你|收藏了你的作品|转发了你的作品|分享了你)');
  const menu = document.querySelector('#douyin-header-menuCt') || document.body;
  if (!menu) return {ok: false, reason: 'no_scope'};
  let root = null, best = Infinity;
  for (const el of Array.from(menu.querySelectorAll('*'))) {
    const tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'SVG' || tag === 'PATH') continue;
    const t = clean(el.innerText);
    if (!t.includes('互动消息') || !ACT.test(t)) continue;
    const n = el.querySelectorAll('*').length;
    if (n > 2 && n < best) { best = n; root = el; }
  }
  if (!root) return {ok: false, reason: 'panel_not_rendered'};
  let listEl = null, rows = [], bestScore = -1;
  for (const cand of Array.from(root.querySelectorAll('div,ul'))) {
    const kids = Array.from(cand.children).filter(k => k.getBoundingClientRect().height >= 28);
    if (kids.length < 2) continue;
    const hs = kids.map(k => Math.round(k.getBoundingClientRect().height));
    const avg = hs.reduce((a, b) => a + b, 0) / hs.length;
    const similar = hs.filter(h => Math.abs(h - avg) <= 16).length;
    if (similar < Math.max(2, Math.floor(kids.length * 0.6))) continue;
    const score = similar * 10 + kids.length + (ACT.test(clean(cand.innerText)) ? 30 : 0);
    if (score > bestScore) { bestScore = score; listEl = cand; rows = kids; }
  }
  const row = rows[index];
  if (!row) return {ok: false, reason: 'row_out_of_range', row_count: rows.length};
  try { row.scrollIntoView({block: 'center'}); } catch (e) {}
  // A notification row's left part (avatar / nickname) links to the *user* profile, while the
  // thumbnail side opens the *video*. So the click point must be deliberately chosen:
  // prefer an explicit /video/ link inside the row, else the thumbnail end of the row.
  let vlink = null;
  if (preferVideo) {
    const cands = Array.from(row.querySelectorAll('a[href*="/video/"]'));
    if (cands.length) vlink = cands[0];
  }
  const r0 = row.getBoundingClientRect();
  const b = vlink ? vlink.getBoundingClientRect() : r0;
  const x = vlink
    ? Math.round(b.x + b.width / 2)
    : (preferVideo ? Math.round(r0.x + r0.width - Math.min(45, Math.max(20, r0.width * 0.12)))
                   : Math.round(r0.x + Math.min(60, r0.width / 2)));
  return {ok: true, x, y: Math.round(b.y + b.height / 2),
          via: vlink ? 'video_link' : (preferVideo ? 'row_thumbnail_end' : 'row_left'),
          row_text: clean(row.innerText).slice(0, 200)};
}
"""

_JS_CLICK_NOTIFY_ICON = r"""
() => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  const menu = document.querySelector('#douyin-header-menuCt') || document.body;
  if (!menu) return {ok: false, reason: 'no_header_menu'};
  for (const el of Array.from(menu.querySelectorAll('*'))) {
    const t = clean(el.innerText);
    if (t !== '通知') continue;
    if (el.querySelectorAll('*').length > 0 && t.length === 2) continue; // prefer leaf
    let target = el;
    for (let i = 0; i < 5; i++) {
      const p = target.parentElement;
      if (!p || clean(p.innerText) !== '通知') break;
      target = p;
    }
    target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
    return {ok: true, tag: target.tagName.toLowerCase()};
  }
  return {ok: false, reason: 'notice_icon_not_found'};
}
"""

# The dropdown's first state only shows the compact 赞/评论 feed and a 「全部消息」 entry.
# The tabbed inbox (粉丝 | @我的 | 评论 | 赞 | 推荐) — the one that actually holds comment replies
# and @ mentions — appears only after 「全部消息」 is clicked. Measured 2026-11-02.
_JS_NOTIFY_EXPAND = r"""
() => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  const menu = document.querySelector('#douyin-header-menuCt') || document.body;
  if (!menu) return {ok: false, reason: 'no_header_menu'};
  for (const el of Array.from(menu.querySelectorAll('a,div,span,button'))) {
    const t = clean(el.innerText);
    if (t !== '全部消息') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    let target = el;
    for (let i = 0; i < 3; i++) {
      const p = target.parentElement;
      if (!p || clean(p.innerText) !== '全部消息') break;
      target = p;
    }
    target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
    return {ok: true, tag: target.tagName.toLowerCase(), href: el.getAttribute('href') || ''};
  }
  return {ok: false, reason: 'all_messages_entry_not_found'};
}
"""

_JS_NOTIFY_TAB_LABELS = r"""
() => {
  const clean = (s) => (s || '').replace(/[\u200B-\u200F\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  const MENU = document.querySelector('#douyin-header-menuCt') || document.body;
  const TABS = ['全部消息', '粉丝', '@我的', '评论', '赞', '推荐'];
  const out = [];
  for (const el of Array.from(MENU.querySelectorAll('div,span,a,li'))) {
    const t = clean(el.innerText);
    if (!TABS.includes(t)) continue;
    if (el.querySelectorAll('*').length > 2) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    out.push(t);
  }
  return Array.from(new Set(out));
}
"""

# A notification row's thumbnail opens the video as an in-page modal on /user/self:
# the URL becomes /user/self?modal_id=<aweme_id>. That query param is the only reliable
# id the row exposes (rows carry no /video/ link for like/comment notifications), so the
# modal_id is what the locate step turns into a plain /video/<id> verification.
_JS_CURRENT_VIDEO_ID = r"""
() => {
  const q = new URLSearchParams(location.search || '');
  const m = (location.pathname || '').match(/\/video\/(\d{8,})/);
  return {url: location.href, modal_id: q.get('modal_id') || '', path_id: m ? m[1] : ''};
}
"""


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


async def _cdp_version(port: int) -> dict[str, Any] | None:
    """Minimal HTTP GET /json/version on the CDP endpoint (no extra deps)."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=3.0
        )
        try:
            writer.write(
                b"GET /json/version HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(8192), timeout=3.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        text = raw.decode("utf-8", "replace")
        if " 200 " not in text.split("\r\n", 1)[0]:
            return None
        body = text.split("\r\n\r\n", 1)[-1]
        start = body.find("{")
        return json.loads(body[start:]) if start >= 0 else None
    except Exception:
        return None


def _find_edge() -> str | None:
    for cand in _EDGE_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def _decode_console(data: bytes | None) -> str:
    """Decode subprocess console output (icacls / taskkill) robustly.

    Windows console tools emit the OEM/ANSI code page (here cp936 = GBK); try the
    locale's preferred encoding first, then GBK, then UTF-8, picking the
    candidate with the most CJK chars and fewest replacement chars.
    """
    if not data:
        return ""
    import locale

    def score(s: str) -> int:
        return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff") - s.count("\ufffd") * 50

    candidates: list[tuple[int, str]] = []
    tried: set[str] = set()
    for enc in (locale.getpreferredencoding(False), "gbk", "utf-8", "mbcs"):
        if not enc or enc in tried:
            continue
        tried.add(enc)
        try:
            s = data.decode(enc)
        except Exception:
            continue
        candidates.append((score(s), s))
    if not candidates:
        return data.decode("utf-8", "replace")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _dur_at(info: dict[str, Any], idx: int, n: int) -> float:
    dur = info.get("duration_seconds") or 0
    return dur * idx / max(n - 1, 1)


def _is_login_wall(body_text: str) -> bool:
    head = (body_text or "")[:4000]
    return any(t in head for t in _LOGIN_WALL_TEXTS)


# -- notification row parsing (pure functions, unit-checkable) ----------------

_NOTIFY_ACTION_FULL_RE = re.compile(
    r"(?P<action>"
    r"赞了你的作品|赞了你的评论|赞了你的视频|回复了你的评论|回复了你|评论了你的作品|评论了你|"
    r"@了你|提到了你|关注了你|收藏了你的作品|转发了你的作品|分享了你"
    r")"
)
_NOTIFY_ACTION_WORD_RE = re.compile(_NOTIFY_ACTION_RE_SRC)
_NOTIFY_TIME_RE = re.compile(
    r"^(?:刚刚|刚刚好|\d+\s*秒前|\d+\s*分钟前|\d+\s*小时前|昨天|前天|"
    r"\d+\s*天前|\d+\s*周前|\d+\s*(?:个月|月)前|\d+\s*年前|"
    r"\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2})$"
)
_NOTIFY_TABS = {"all": None, "comment": "评论", "mention": "@我的", "like": "赞", "fan": "粉丝", "recommend": "推荐"}


def _notify_age_hours(text: str, now: datetime | None = None) -> tuple[float | None, str]:
    """Estimate how old a douyin notification is, from its own time label.

    Returns ``(age_hours, precision)`` where precision is ``exact`` (site gave a
    relative label resolvable to the hour or better), ``coarse`` (昨天/前天/N天前/
    月-日 — the site only gives a calendar-ish label, so the value is a lower bound
    style estimate) or ``unknown``. Nothing is invented: unparsable labels return
    ``(None, 'unknown')`` and the caller keeps the raw text.
    """
    t = (text or "").strip()
    if not t:
        return None, "unknown"
    now = now or datetime.now(_BEIJING_TZ)
    if t == "刚刚":
        return 0.0, "exact"
    m = re.fullmatch(r"(\d+)\s*秒前", t)
    if m:
        return int(m.group(1)) / 3600.0, "exact"
    m = re.fullmatch(r"(\d+)\s*分钟前", t)
    if m:
        return int(m.group(1)) / 60.0, "exact"
    m = re.fullmatch(r"(\d+)\s*小时前", t)
    if m:
        return float(int(m.group(1))), "exact"
    if t == "昨天":
        return 24.0, "coarse"   # 12~48h ago
    if t == "前天":
        return 48.0, "coarse"
    m = re.fullmatch(r"(\d+)\s*天前", t)
    if m:
        return float(int(m.group(1)) * 24), "coarse"
    m = re.fullmatch(r"(\d+)\s*周前", t)
    if m:
        return float(int(m.group(1)) * 24 * 7), "coarse"
    m = re.fullmatch(r"(\d+)\s*(?:个月|月)前", t)
    if m:
        return float(int(m.group(1)) * 24 * 30), "coarse"
    m = re.fullmatch(r"(\d+)\s*年前", t)
    if m:
        return float(int(m.group(1)) * 24 * 365), "coarse"
    for pat in (r"(\d{4})-(\d{1,2})-(\d{1,2})", r"(\d{1,2})月(\d{1,2})日", r"(\d{1,2})-(\d{1,2})"):
        m = re.fullmatch(pat, t)
        if not m:
            continue
        try:
            if len(m.groups()) == 3:
                yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                mm, dd = int(m.group(1)), int(m.group(2))
                yy = now.year
            when = datetime(yy, mm, dd, tzinfo=_BEIJING_TZ)
            delta = (now - when).total_seconds() / 3600.0
            return max(0.0, delta), "coarse"
        except Exception:
            return None, "unknown"
    return None, "unknown"


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
        self._session_info: dict[str, Any] = {}
        self._launched_pid: int | None = None
        self._verify_aweme_id = True

    async def close(self) -> None:
        """Release the Playwright driver; the detached Edge keeps running."""
        pw = self._pw
        self._pw = None
        self._browser = None
        self._context = None
        if pw is None:
            return
        try:
            await pw.stop()
        except Exception:
            pass

    # -- schema -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "douyin_native"

    @property
    def description(self) -> str:
        # 模型面那句的唯一可写处是 resource.yaml：装载侧按清单判定 160 字符预算、也按清单
        # 发进 tools[]。类里再自撰一句不会改变模型看到的东西，只会让人改错文件。
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "open", "login_status", "login_check", "login_export", "login_restore",
                        "feed", "search", "video", "like", "storyboard", "comments", "watch_report",
                        "my_works", "interactions",
                        "send_comment", "reply_to_comment", "send_forward", "publish",
                        "delete_work",
                        "usage", "health", "close",
                    ],
                    "description": "Douyin capability to run (see toolskill for details).",
                },
                "keyword": {"type": "string", "description": "Search keyword / report topic."},
                "max_items": {"type": "integer", "description": "Feed/report/comment item cap (1-40)."},
                "frame_count": {"type": "integer", "description": "Storyboard frames (2-16)."},
                "slot": {"type": "integer", "description": "1-based feed item to open in player."},
                "like": {
                    "type": "boolean",
                    "description": (
                        "like only: true = 点赞, false = 取消点赞（幂等；已是目标状态时不点击）。"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Comment/reply body (send_comment / reply_to_comment only).",
                },
                "comment_index": {
                    "type": "integer",
                    "description": (
                        "1-based index of the target comment in the comments list "
                        "(reply_to_comment; use together with or instead of comment_text_match)."
                    ),
                },
                "comment_text_match": {
                    "type": "string",
                    "description": (
                        "Substring used to locate the target comment by its body text "
                        "or author name (reply_to_comment)."
                    ),
                },
                "hours": {
                    "type": "number",
                    "description": (
                        "interactions only. Time window in hours (default 24). Rows whose own time "
                        "label (刚刚/N分钟前/N小时前/昨天/N天前/M-D) resolves to an age above this "
                        "are dropped and counted in filter.dropped_by_time."
                    ),
                },
                "tab": {
                    "type": "string",
                    "enum": ["comment", "mention", "like", "fan", "recommend", "all"],
                    "description": (
                        "interactions only. Which tab of the 互动消息 panel to read: comment = 评论 "
                        "(replies to my comments, default), mention = @我的, like = 赞, fan = 粉丝, "
                        "recommend = 推荐, all = whatever the panel opens on."
                    ),
                },
                "locate_item": {
                    "type": "integer",
                    "description": (
                        "interactions only. 1-based index of an item in `items`; the tool follows that "
                        "notification into its source video, verifies the mounted aweme_id, and (with "
                        "comment_text_match) reports the matched comment + comment_id, i.e. exactly what "
                        "reply_to_comment(aweme_id=...) needs. Read-only; sends nothing."
                    ),
                },
                "aweme_id": {
                    "type": "string",
                    "description": (
                        "reply_to_comment only. Target video id taken from an interactions item "
                        "(source_aweme_id) — replies there instead of resolving a feed slot: the video "
                        "is opened by id and its mounted identity is verified before anything is sent."
                    ),
                },
                "allow_send": {"type": "boolean", "description": "Explicit send permission flag."},
                "allow_publish": {
                    "type": "boolean",
                    "description": (
                        "Explicit publishing acknowledgement for mode=publish. Publishing needs "
                        "ALL THREE: mode='publish', this flag true, and send_policy.json "
                        "allow_publish=true. Anything missing returns status=blocked and nothing "
                        "is published; mode=draft never needs it."
                    ),
                },
                "video_path": {
                    "type": "string",
                    "description": (
                        "Absolute local path of the video to upload (publish only; mp4/mov/webm/"
                        "m4v/avi/mkv). The file is picked through the page's file input."
                    ),
                },
                "title": {"type": "string", "description": "Work title typed with real keystrokes (publish)."},
                "desc": {"type": "string", "description": "Work description typed with real keystrokes (publish)."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Topic tags (array or a comma separated string). Each tag is typed into the "
                        "description editor as #tag + Space so the site turns it into a real topic chip."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["draft", "publish"],
                    "description": (
                        "publish only. draft (default) = upload + fill title/desc/tags + 暂存离开 "
                        "(save draft); no publish click, no real post. publish = click 发布, which "
                        "additionally requires send_policy.json allow_publish=true."
                    ),
                },
                "include_frames": {
                    "type": "boolean",
                    "description": "watch_report samples storyboard frames.",
                },
                "verify_aweme_id": {
                    "type": "boolean",
                    "description": (
                        "Default true. Slot-based actions (video/storyboard/comments/send_*/"
                        "reply_to_comment) resolve the target from feed_snapshot.json and verify "
                        "the opened video's aweme_id matches it; drift returns status=mismatch "
                        "instead of another video's data."
                    ),
                },
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
            _atomic_write_json(p, dict(_SEND_POLICY_DEFAULT))
        policy = _read_json(p, {}) or {}
        if not isinstance(policy, dict):
            policy = {}
        # fill missing keys with DISABLED defaults; never enables anything
        merged = {k: v for k, v in _SEND_POLICY_DEFAULT.items()}
        merged.update(policy)
        missing = [k for k in _SEND_POLICY_DEFAULT if k not in policy]
        if missing:
            try:
                _atomic_write_json(p, merged)
            except Exception:
                pass
        return merged

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

    def _login_state_file(self) -> Path:
        return self.state_dir / "login_state.json"

    def _login_state_summary(self) -> dict[str, Any]:
        path = self._login_state_file()
        state = _read_json(path, {}) or {}
        cookies = state.get("cookies") or []
        return {
            "path": str(path),
            "exists": path.exists(),
            "saved_at": state.get("saved_at", ""),
            "cookie_count": len(cookies),
            "auth_cookie_names": state.get("auth_cookie_names", []),
            "session_cookie_names": state.get("session_cookie_names", []),
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }

    async def _acl_status(self) -> dict[str, Any]:
        """Live ACL reading of login_state.json (never stored inside the file)."""
        path = self._login_state_file()
        if not path.exists():
            return {"exists": False}
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        who = f"{domain}\\{user}" if (domain and user) else user
        if os.name != "nt":
            return {"exists": True, "acl": "skipped (non-Windows)", "user": who}
        try:
            proc = await asyncio.create_subprocess_exec(
                "icacls", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
            text = _decode_console(out) or _decode_console(err)
        except Exception as exc:
            return {"exists": True, "error": str(exc), "user": who}
        entries = []
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln.startswith(str(path)):
                ln = ln.replace(str(path), "<login_state.json>")
            entries.append(ln)
        return {
            "exists": True,
            "user": who,
            "icacls_output": text.strip(),
            "entries": entries[:8],
            "only_current_user_full_control": any(f"{who}:(F)" in ln for ln in entries),
            "has_inherited_ace": any("(I)" in ln and ":(F)" in ln for ln in entries),
        }

    def _check_send_gate(self, action: str, allow_send: bool) -> tuple[bool, str]:
        policy = self._send_policy()
        if not policy.get("enabled", False):
            return False, (
                "send_policy.json enabled=false: sending is OFF by default "
                "(only the user may enable it)"
            )
        if not allow_send:
            return False, f"allow_send must be explicitly true by the user for action={action}"
        key = _POLICY_KEY.get(action)
        if key and not policy.get(key, False):
            return False, f"send_policy.json {key}=false: user has not granted this action"
        state = _read_json(self._login_state_file(), {}) or {}
        if not state or not (state.get("auth_cookie_names") or []):
            return False, (
                "not logged in: login_state.json has no douyin session cookie. "
                "Complete the login flow (open -> user scans in the Edge window -> login_check)"
            )
        usage = self._usage()
        today = usage.get("days", {}).get(_day_key(), {})
        limit = int(policy.get(_LIMIT_KEY.get(action, ""), 0) or 0)
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

    def _record_send_usage(self, action: str) -> dict[str, Any]:
        usage = self._touch_usage(action)
        today = usage.get("days", {}).get(_day_key(), {})
        today[f"{action}_last_ts"] = time.time()
        _atomic_write_json(self.state_dir / "usage.json", usage)
        return today

    def _check_publish_gate(self, mode: str, allow_send: bool, allow_publish_flag: bool = False) -> tuple[bool, str]:
        """Double gate for action=publish.

        Layer 1 (both modes): send_policy.json ``enabled`` (human edited the file) AND
        the caller passing ``allow_send=true`` AND a logged-in session AND quota/interval.
        Layer 2 (mode=publish only): send_policy.json ``allow_publish`` must be true AND
        the caller explicitly passed ``allow_publish=true`` — publishing a real post is a
        separately, explicitly granted permission.

        mode=draft (the default) never touches layer 2 and never clicks 发布: it only
        uploads and stores a draft (暂存离开).
        """
        policy = self._send_policy()
        if not policy.get("enabled", False):
            return False, (
                "send_policy.json enabled=false: publish/draft is OFF by default "
                "(only the user may enable it)"
            )
        if not allow_send:
            return False, "allow_send must be explicitly true by the user for action=publish"
        if mode == "publish":
            if not policy.get("allow_publish", False):
                return False, (
                    "send_policy.json allow_publish=false: user has not granted real publishing; "
                    "mode=publish refused (use mode=draft to only store a draft)"
                )
            if not allow_publish_flag:
                return False, (
                    "mode=publish additionally requires the caller to pass allow_publish=true "
                    "(explicit publishing acknowledgement) — missing, nothing was published"
                )
        state = _read_json(self._login_state_file(), {}) or {}
        if not state or not (state.get("auth_cookie_names") or []):
            return False, (
                "not logged in: login_state.json has no douyin session cookie. "
                "Complete the login flow (open -> user scans in the Edge window -> login_check)"
            )
        usage = self._usage()
        today = usage.get("days", {}).get(_day_key(), {})
        limit = int(policy.get("daily_limit_publish", 0) or 0)
        used = int(today.get("publish", 0) or 0)
        if limit > 0 and used >= limit:
            return False, f"daily quota reached: {used}/{limit} for publish"
        interval = float(policy.get("interval_seconds", 0) or 0)
        last = today.get("publish_last_ts")
        if interval > 0 and last:
            elapsed = time.time() - float(last)
            if elapsed < interval:
                return False, f"interval not elapsed: wait {interval - elapsed:.0f}s before next publish"
        return True, ""

    # -- login state persistence -------------------------------------------

    async def _douyin_cookies(self, context) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for url in (
            "https://www.douyin.com/",
            "https://www.douyin.com",
            "https://douyin.com/",
            "https://www.iesdouyin.com/",
            "https://sso.douyin.com/",
            "https://live.douyin.com/",
        ):
            try:
                for c in await context.cookies([url]):
                    key = (c.get("name", ""), c.get("domain", ""), c.get("path", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(c)
            except Exception:
                continue
        return out

    async def _restrict_acl(self, path: Path) -> dict[str, str]:
        """icacls: drop inheritance and grant only the current user."""
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        who = f"{domain}\\{user}" if (domain and user) else user
        result = {"user": who, "acl": "", "owner_check": "", "error": ""}
        if os.name != "nt" or not who:
            result["acl"] = "skipped (non-Windows)"
            return result
        try:
            proc = await asyncio.create_subprocess_exec(
                "icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
            result["acl"] = (_decode_console(out) or _decode_console(err)).strip()
        except Exception as exc:
            result["error"] = str(exc)
        try:
            proc = await asyncio.create_subprocess_exec(
                "icacls", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            result["owner_check"] = _decode_console(out).strip()
        except Exception as exc:
            result["owner_check"] = f"check failed: {exc}"
        return result

    async def _export_login_state(self, page, login: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist douyin cookies to login_state.json (current-user ACL)."""
        try:
            cookies = await self._douyin_cookies(page.context)
        except Exception as exc:
            return {"exported": False, "error": str(exc)}
        auth = sorted({c.get("name", "") for c in cookies} & _LOGIN_COOKIE_NAMES)
        session_names = [
            n for n in ("sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt")
            if n in auth
        ]
        payload_cookies = []
        for c in cookies:
            item = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "expires": c.get("expires", -1),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
            }
            same = c.get("sameSite")
            if same in ("Strict", "Lax", "None"):
                item["sameSite"] = same
            payload_cookies.append(item)
        payload = {
            "saved_at": _iso_now(),
            "cookie_count": len(payload_cookies),
            "auth_cookie_names": auth,
            "session_cookie_names": session_names,
            "domain": "douyin.com",
            "note": "Exported by douyin_native. Contains live douyin cookies; keep it local. ACL restricted to the current user.",
            "cookies": payload_cookies,
        }
        path = self._login_state_file()
        _atomic_write_json(path, payload)
        # ACL must be the LAST thing touched: any later write replaces the file
        # and re-inherits the parent directory ACL.
        acl = await self._restrict_acl(path)
        verify = await self._acl_status()
        return {
            "exported": True,
            "path": str(path),
            "cookie_count": len(payload_cookies),
            "auth_cookie_names": auth,
            "session_cookie_names": session_names,
            "saved_at": payload["saved_at"],
            "acl_apply_output": acl.get("acl", ""),
            "acl_error": acl.get("error", ""),
            "acl_user": acl.get("user", ""),
            "acl_verify": verify,
        }

    async def _restore_login_state(self, context) -> dict[str, Any]:
        """Inject login_state.json cookies into the live context and verify."""
        path = self._login_state_file()
        state = _read_json(path, {}) or {}
        cookies = state.get("cookies") or []
        if not cookies:
            return {"restored": False, "reason": "no login_state.json cookies to inject"}
        live = await self._douyin_cookies(context)
        live_names = {c.get("name", "") for c in live}
        auth_live = sorted(live_names & _LOGIN_COOKIE_NAMES)
        if "sessionid" in live_names or "sessionid_ss" in live_names:
            return {
                "restored": False,
                "skipped": True,
                "reason": "live context already carries a douyin session cookie; injection skipped to avoid clobbering it",
                "live_auth_cookie_names": auth_live,
            }
        try:
            await context.add_cookies(cookies)
        except Exception as exc:
            return {"restored": False, "error": f"add_cookies failed: {exc}"}
        live_after = await self._douyin_cookies(context)
        after_names = {c.get("name", "") for c in live_after}
        injected_auth = sorted(after_names & _LOGIN_COOKIE_NAMES)
        return {
            "restored": bool(set(("sessionid", "sessionid_ss")) & after_names),
            "injected_cookie_count": len(cookies),
            "live_auth_cookie_names": injected_auth,
            "saved_at": state.get("saved_at", ""),
        }

    # -- browser session ----------------------------------------------------

    def _browser_record(self) -> dict[str, Any]:
        return _read_json(self.state_dir / "browser.json", {}) or {}

    def _write_browser_record(self, **kw: Any) -> None:
        rec = self._browser_record()
        rec.update(kw)
        rec.setdefault("started_at", _iso_now())
        _atomic_write_json(self.state_dir / "browser.json", rec)

    async def _find_profile_process(self) -> dict[str, Any] | None:
        """Locate a running msedge whose command line uses our profile dir."""
        if os.name != "nt":
            return None
        profile = str(self.state_dir / "profile")
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
            "Where-Object { $_.CommandLine -like '*douyin_native*' } | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive", "-Command", ps,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        except Exception:
            return None
        text = (out or b"").decode("utf-8", "replace").strip()
        if not text or text == "null":
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        rows = data if isinstance(data, list) else [data]
        # prefer the browser process: it carries --remote-debugging-port=<N>
        candidates = []
        for row in rows:
            cmd = str(row.get("CommandLine") or "")
            if profile.lower() not in cmd.lower():
                continue
            m = re.search(r"--remote-debugging-port=(\d+)", cmd)
            if m:
                candidates.append({"pid": row.get("ProcessId"), "port": int(m.group(1))})
        if candidates:
            return candidates[0]
        for row in rows:
            cmd = str(row.get("CommandLine") or "")
            if profile.lower() in cmd.lower():
                return {"pid": row.get("ProcessId"), "port": None}
        return None

    async def _ensure_session(self):
        self._ensure_dirs()
        rec = self._browser_record()
        port = int(rec.get("port") or 0)
        if port and await _http_alive(port):
            try:
                ctx, page = await self._connect(port)
                self._session_info = {
                    "mode": "reused",
                    "port": port,
                    "pid": rec.get("pid"),
                    "started_at": rec.get("started_at"),
                }
                return ctx, page
            except Exception as exc:
                self._session_info = {"mode": "reconnect_failed", "port": port, "error": str(exc)}
                self._browser = None
                self._context = None
        alive = await self._find_profile_process()
        if alive and alive.get("port"):
            try:
                ctx, page = await self._connect(int(alive["port"]))
                self._write_browser_record(pid=alive.get("pid"), port=int(alive["port"]), ready=True)
                self._session_info = {
                    "mode": "reused_found_port",
                    "port": int(alive["port"]),
                    "pid": alive.get("pid"),
                }
                return ctx, page
            except Exception:
                pass
        return await self._launch()

    async def _connect(self, port: int):
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        if self._browser is None or self._context is None:
            self._browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            self._context = (
                self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            )
        page = await self._page_for(self._context)
        restore = await self._restore_login_state(self._context)
        self._session_info["login_restore"] = restore
        return self._context, page

    async def _launch(self):
        """Start ONE detached, long-lived Edge with a stable CDP port.

        A detached subprocess (not ``launch_persistent_context``) keeps the
        profile owner alive across tool invocations, so the session cookies and
        the login state stop disappearing between actions.
        """
        from playwright.async_api import async_playwright
        edge = _find_edge()
        if edge is None:
            raise RuntimeError("Microsoft Edge not found at " + " / ".join(_EDGE_CANDIDATES))
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._context = None
        port = _pick_free_port()
        profile_dir = self.state_dir / "profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            edge,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir}",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        flags = 0
        if os.name == "nt":
            flags = int(getattr(subprocess, "DETACHED_PROCESS", 0)) | int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        pid = None
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
            pid = proc.pid
        except Exception as exc:
            raise RuntimeError(f"failed to launch Edge: {exc}") from exc
        deadline = time.time() + _CDP_READY_TIMEOUT
        version = None
        while time.time() < deadline:
            version = await _cdp_version(port)
            if version:
                break
            await asyncio.sleep(0.6)
        if not version:
            proc_info = await self._find_profile_process()
            alt_port = (proc_info or {}).get("port")
            if alt_port and alt_port != port:
                version = await _cdp_version(int(alt_port))
                if version:
                    port = int(alt_port)
            if not version:
                raise RuntimeError(
                    f"Edge did not expose a CDP endpoint on port {port} within {_CDP_READY_TIMEOUT:.0f}s"
                )
        self._launched_pid = pid
        proc_info = await self._find_profile_process()
        if proc_info and proc_info.get("pid"):
            pid = proc_info["pid"]
        _atomic_write_json(
            self.state_dir / "browser.json",
            {
                "pid": pid,
                "port": port,
                "ready": True,
                "started_at": _iso_now(),
                "edge": edge,
                "mode": "detached_cdp",
                "browser_version": version.get("Browser") if isinstance(version, dict) else None,
            },
        )
        self._browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        self._context = (
            self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        )
        page = await self._page_for(self._context)
        restore = await self._restore_login_state(self._context)
        self._session_info = {
            "mode": "launched",
            "port": port,
            "pid": pid,
            "started_at": _iso_now(),
            "login_restore": restore,
        }
        return self._context, page

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

    async def _dismiss_overlays(self, page, *, press_escape: bool = False) -> None:
        try:
            await page.evaluate(
                "() => { const els = Array.from(document.querySelectorAll('div,span')); "
                "for (const e of els) { const t=(e.innerText||'').trim(); "
                "if (t === '我知道了') { e.click(); break; } } }"
            )
        except Exception:
            pass
        if press_escape:
            for _ in range(3):
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.4)

    # -- login detection (multi-signal) ------------------------------------

    async def _detect_login(self, page) -> dict[str, Any]:
        ui: dict[str, Any] = {}
        api: dict[str, Any] = {}
        cookie_names: set[str] = set()
        cookie_count = 0
        try:
            cookie_list = await self._douyin_cookies(page.context)
            cookie_count = len(cookie_list)
            cookie_names = {str(c.get("name") or "") for c in cookie_list}
        except Exception as exc:
            ui.setdefault("cookie_error", str(exc))
        auth_present = sorted(cookie_names & _LOGIN_COOKIE_NAMES)
        session_present = bool({"sessionid", "sessionid_ss"} & cookie_names)

        try:
            ui = await page.evaluate(_JS_LOGIN_UI)
        except Exception as exc:
            ui = {"error": str(exc)}
        try:
            body = await page.evaluate("document.body ? document.body.innerText : ''")
        except Exception:
            body = ""
        on_douyin = "douyin.com" in (getattr(page, "url", "") or "")
        if on_douyin:
            try:
                api = await page.evaluate(_JS_ACCOUNT_INFO)
            except Exception as exc:
                api = {"error": str(exc)}

        ui_login_btn = ui.get("login_btn")
        ui_avatar = ui.get("avatar")
        ui_modal = bool(ui.get("modal"))
        ui_wall = bool(ui.get("login_wall_text")) or (
            on_douyin and _is_login_wall(body) and not ui_avatar
        )
        ui_says_logged_out = bool(ui_login_btn) or ui_modal or ui_wall
        ui_says_logged_in = (not ui_says_logged_out) and (bool(ui_avatar) or bool(body))

        api_ok = bool(api.get("ok"))
        api_error_code = api.get("error_code")
        api_description = str(api.get("description") or "")
        # 明确未登录：只有服务端明确否定（会话过期/请登录/未登录 类错误码或文案）才算。
        # 2026-11-01 现网实测：已登录会话 account/info 会返回 1011 "用户不存在"，
        # 但 header 有真实昵称；把 1011 当未登录会误杀真实登录（宁拦勿误发 但也不能误拦）。
        _api_code_str = str(api_error_code) if api_error_code is not None else ""
        _api_definitive_logout_codes = {"1", "2", "100", "1000"}
        api_definitive_logout = (
            api.get("status") == 200
            and not api_ok
            and (
                _api_code_str in _api_definitive_logout_codes
                or "过期" in api_description
                or "请登录" in api_description
                or "未登录" in api_description
            )
        )
        api_ambiguous = (
            api.get("status") == 200
            and not api_ok
            and not api_definitive_logout
            and api_error_code not in (None, 0)
        )

        basis = ""
        if api_ok:
            logged_in = True
            basis = "server_api_session_ok"
        elif api_definitive_logout:
            logged_in = False
            basis = "server_api_session_expired"
        elif api_ambiguous:
            if ui_says_logged_out:
                logged_in = False
                basis = "api_ambiguous_ui_logged_out"
            elif bool(ui_avatar) and not ui_login_btn:
                logged_in = True
                basis = "ui_header_avatar_no_login_button_with_api_ambiguous"
            else:
                logged_in = False
                basis = "api_ambiguous_indeterminate"
        elif ui_says_logged_out:
            logged_in = False
            basis = "ui_login_button_or_modal_visible"
        elif not session_present and not auth_present:
            logged_in = False
            basis = "no_douyin_session_cookie"
        elif ui_says_logged_in:
            logged_in = True
            basis = "ui_header_has_no_login_button"
        else:
            logged_in = False
            basis = "indeterminate_default_false"

        cookie_valid = None
        if api_ok:
            cookie_valid = True
        elif api_definitive_logout:
            cookie_valid = False
        elif api_ambiguous:
            cookie_valid = bool(ui_avatar) and not ui_says_logged_out

        result = {
            "logged_in": bool(logged_in),
            "basis": basis,
            "cookie_valid": cookie_valid,
            "cookie_auth": auth_present,
            "cookie_count": cookie_count,
            "session_cookie_present": session_present,
            "login_modal": ui_modal,
            "nav_login": bool(ui_login_btn),
            "cookie": {
                "auth_names": auth_present,
                "session_cookie_present": session_present,
                "cookie_valid": cookie_valid,
                "douyin_cookie_count": cookie_count,
            },
            "ui": {
                "login_button": ui_login_btn,
                "avatar_present": bool(ui_avatar),
                "avatar": ui_avatar,
                "avatar_src": ui.get("avatar_src", ""),
                "nickname": ui.get("nickname", ""),
                "login_modal": ui_modal,
                "login_wall_text": ui_wall,
                "title": ui.get("title", ""),
                "url": ui.get("url", ""),
                "error": ui.get("error", ""),
            },
            "api": {
                "endpoint": _ACCOUNT_INFO_URL,
                "status": api.get("status"),
                "error_code": api_error_code,
                "description": api_description,
                "ok": api_ok,
                "error": api.get("error", ""),
            },
            "inconsistent_signals": bool(
                (auth_present or session_present) and (not logged_in)
            ),
            "decided_by": "api>ui>cookie (server verdict wins; explicit 会话过期/请登录 = logout; 1011 ambiguous -> header avatar & no login button decide; cookienames last)",
        }
        return result

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

    async def _feed_cards(self, page) -> list[dict[str, Any]]:
        try:
            return await page.evaluate(_JS_CARDS) or []
        except Exception:
            return []

    async def _feed_entries(self, page, max_items: int = 30, keyword: str | None = None) -> list[dict[str, Any]]:
        """Prefer structured feed cards ([data-aweme-id]); fall back to innerText."""
        entries: list[dict[str, Any]] = []
        cards = await self._feed_cards(page)
        for c in cards[:max_items]:
            title = (c.get("title") or "").strip()
            author = (c.get("author") or "").strip()
            for ln in (c.get("lines") or []):
                if ln.startswith("@") and not author:
                    author = ln[1:].strip()
            if not title:
                for ln in (c.get("lines") or []):
                    if ln.startswith("@") or _FEED_ITEM_RE.match(ln) or ln.startswith("·"):
                        continue
                    if _parse_count(ln) is not None:
                        continue
                    title = ln
                    break
            if not (title or author):
                continue
            entry = {
                "slot": len(entries) + 1,
                "duration": c.get("duration", ""),
                "duration_seconds": _parse_duration_seconds(c.get("duration", "")),
                "likes": _parse_count(c.get("likes_text") or "") or 0,
                "likes_text": c.get("likes_text", ""),
                "title": title[:200],
                "author": author,
                "date": c.get("date", ""),
                "link": c.get("link", ""),
                "aweme_id": c.get("aweme_id", ""),
            }
            if keyword and keyword not in (entry["title"] + entry["author"]):
                continue
            entries.append(entry)
        if entries:
            return entries[:max_items]

        # innerText fallback (older page layouts)
        body = await self._page_text(page)
        lines = [ln.strip() for ln in body.splitlines()]
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
                "slot": len(entries) + 1,
                "duration": dur,
                "duration_seconds": _parse_duration_seconds(dur),
                "likes": _parse_count(likes) or 0,
                "likes_text": likes,
                "title": title[:200],
                "author": author[1:].strip() if author.startswith("@") else author,
                "date": date.strip("· ").strip(),
                "link": "",
                "aweme_id": "",
            }
            if keyword and keyword not in (entry["title"] + entry["author"]):
                continue
            entries.append(entry)
        return entries

    async def _search_cards(self, page, max_items: int = 30) -> list[dict[str, Any]]:
        """Parse search-page video cards (li > div.search-result-card > a[href*=/video/])."""
        try:
            rows = await page.evaluate(
                """() => {
                  const anchors = [...document.querySelectorAll('a[href*="/video/"]')];
                  const seen = new Set();
                  const out = [];
                  for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/\\/video\\/(\\d{15,25})/);
                    if (!m) continue;
                    const id = m[1];
                    if (seen.has(id)) continue;
                    seen.add(id);
                    const card = a.closest('li, div.search-result-card') || a;
                    const text = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                    let title = '', duration = '', likesText = '', author = '';
                    for (const ln of text) {
                      if (/^\\d{1,2}:\\d{2}(:\\d{2})?$/.test(ln)) { if (!duration) duration = ln; continue; }
                      if (/^@/.test(ln)) { if (!author) author = ln.slice(1); continue; }
                      if (/^\\d+([.]\\d+)?(万|亿)?$/.test(ln) && !title) { if (!likesText) likesText = ln; continue; }
                      if (!title) title = ln;
                    }
                    out.push({aweme_id: id, link: 'https://www.douyin.com/video/' + id,
                              title, duration, likes_text: likesText, author,
                              text_head: text.slice(0, 6).join(' | ')});
                  }
                  return out;
                }"""
            )
        except Exception:
            return []
        entries: list[dict[str, Any]] = []
        for r in (rows or []):
            entries.append(
                {
                    "slot": len(entries) + 1,
                    "aweme_id": r.get("aweme_id", ""),
                    "link": r.get("link", ""),
                    "title": (r.get("title") or "")[:200],
                    "duration": r.get("duration", ""),
                    "duration_seconds": _parse_duration_seconds(r.get("duration", "")),
                    "likes": _parse_count(r.get("likes_text") or "") or 0,
                    "likes_text": r.get("likes_text", ""),
                    "author": r.get("author", ""),
                    "date": "",
                }
            )
        return entries[:max_items]

    async def _load_more_feed(self, page, rounds: int = 3) -> None:
        for _ in range(max(0, rounds)):
            try:
                await page.evaluate(_JS_SCROLL_FEED)
            except Exception:
                pass
            await asyncio.sleep(1.2)

    async def _current_video_info(self, page, max_wait: float = 12.0) -> dict[str, Any]:
        """Read player detail with polling + first-non-empty merge.

        The /video/<id> page renders its detail panel client-side after
        DOMContentLoaded, so a single immediate read can see an empty shell
        (all fields blank, video_present=false). We poll until the detail panel,
        title and duration are all ready, and merge the first non-empty value per
        field so a partially-rendered frame cannot blank a field that was already
        readable. State fields (identity, play state, url) always come from the
        last poll because they are live.
        """
        content_keys = (
            "author", "author_source", "likes", "likes_text", "likes_source",
            "publish_time", "publish_time_source", "title", "title_source",
            "detail_text", "desc", "duration_seconds", "duration_source",
            "detail_panel_present", "detail_panel_chars",
        )
        state_keys = (
            "video_present", "paused", "ready_state", "video_width", "video_height",
            "url", "aweme_id", "aweme_identity",
        )
        merged: dict[str, Any] = {}
        last: dict[str, Any] = {}
        polls = 0
        deadline = time.monotonic() + max(0.0, float(max_wait))
        while True:
            try:
                info = await page.evaluate(_JS_VIDEO_INFO)
            except Exception:
                info = {}
            if not isinstance(info, dict):
                info = {}
            polls += 1
            if info:
                last = info
                for k in content_keys:
                    val = info.get(k)
                    if val not in (None, "", 0) and merged.get(k) in (None, "", 0):
                        merged[k] = val
                for k in state_keys:
                    val = info.get(k)
                    if val not in (None, ""):
                        merged[k] = val
            doc_title_ready = (
                last.get("title_source") == "document.title"
                and (last.get("title") or "") not in ("", "抖音")
            )
            ready = (
                bool(merged.get("detail_panel_present"))
                and doc_title_ready
                and (merged.get("duration_seconds") or 0) > 0
            )
            if ready or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.6)
        if not merged:
            merged = {"video_present": False}
        merged["poll_count"] = polls
        merged["detail_ready"] = bool(merged.get("detail_panel_present")) and (merged.get("duration_seconds") or 0) > 0
        if not merged.get("video_present"):
            merged.setdefault("note", "no visible <video> element")
        if last:
            for k in state_keys:
                val = last.get(k)
                if val not in (None, ""):
                    merged[k] = val
            # title source must be deterministic across calls: document.title is the
            # canonical one (it matches the feed card title, no "第N集 |" prefix that
            # the detail desc adds for series), so prefer it whenever the last poll
            # has it ready instead of keeping whichever source happened to render first
            last_title = last.get("title") or ""
            if last.get("title_source") == "document.title" and last_title not in ("", "抖音"):
                merged["title"] = last_title
                merged["title_source"] = "document.title"
        return merged

    async def _current_aweme(self, page) -> dict[str, Any]:
        """Best-effort identity of the video currently mounted on the page."""
        try:
            got = await page.evaluate(_JS_AWEME_ID)
        except Exception:
            got = {}
        if not isinstance(got, dict):
            got = {}
        return got

    # -- feed snapshot + slot -> aweme resolution (defect 2) ----------------

    def _snapshot_path(self) -> Path:
        return self.state_dir / "feed_snapshot.json"

    def _read_feed_snapshot(self) -> dict[str, Any]:
        snap = _read_json(self._snapshot_path(), {}) or {}
        if not isinstance(snap, dict):
            snap = {}
        items = snap.get("items")
        if not isinstance(items, list):
            items = []
        snap["items"] = [it for it in items if isinstance(it, dict)]
        return snap

    def _snapshot_slot(self, slot: int) -> dict[str, Any]:
        snap = self._read_feed_snapshot()
        for it in snap.get("items", []):
            try:
                if int(it.get("slot") or 0) == int(slot):
                    return it
            except (TypeError, ValueError):
                continue
        return {}

    def _write_feed_snapshot(self, entries: list[dict[str, Any]], *, source: str, page=None) -> dict[str, Any]:
        items = []
        for idx, e in enumerate(entries, start=1):
            try:
                slot = int(e.get("slot") or idx)
            except (TypeError, ValueError):
                slot = idx
            items.append(
                {
                    "slot": slot,
                    "aweme_id": str(e.get("aweme_id") or ""),
                    "link": e.get("link") or "",
                    "title": (e.get("title") or "")[:200],
                    "author": e.get("author") or "",
                    "duration": e.get("duration") or "",
                    "duration_seconds": e.get("duration_seconds") or 0,
                    "likes": e.get("likes") or 0,
                }
            )
        snap = {
            "written_at": _iso_now(),
            "source": source,
            "page_url": getattr(page, "url", "") if page is not None else "",
            "item_count": len(items),
            "items": items,
            "note": (
                "slot -> aweme_id snapshot. Every slot-based action (video/storyboard/"
                "comments/send_comment/reply_to_comment/send_forward) resolves its target "
                "from this file and verifies the opened video matches it; a mismatch is "
                "reported as status=mismatch instead of returning another video's data."
            ),
        }
        try:
            self._ensure_dirs()
            _atomic_write_json(self._snapshot_path(), snap)
        except Exception as exc:  # pragma: no cover - defensive
            snap["write_error"] = str(exc)
        return snap

    async def _open_player(self, page, slot: int = 1) -> dict[str, Any]:
        """Open feed slot #slot and prove the page really shows that video.

        slot is resolved from data/douyin_native/feed_snapshot.json (written by the
        last feed/watch_report run), never from whatever is on the page: douyin's
        recommend grid is a rotating stream, so re-reading it renumbers the slots
        and silently swaps the video (the old bug). After navigation the mounted
        aweme id is verified (detail panel attr -> player-container class -> URL
        path); a mismatch is retried once by re-navigating to the target id, and if
        it still does not align the call returns status=mismatch and executes no
        further action. Every caller receives target_aweme_id / actual_aweme_id /
        aweme_match so the outside can double-check identity.
        """
        slot = max(1, int(slot or 1))
        snap_item = self._snapshot_slot(slot)
        target = str(snap_item.get("aweme_id") or "")
        snapshot_file = self._read_feed_snapshot()
        if target:
            snapshot_state = "present"
        elif snapshot_file.get("items"):
            snapshot_state = "slot_out_of_range"
        else:
            snapshot_state = "missing"
        if not target:
            # No snapshot for this slot: pin the slot to what the live feed shows
            # *now* and record that snapshot so the rest of this action is at least
            # internally consistent. The caller is told the snapshot was re-taken.
            if "/video/" in (page.url or ""):
                # a video page's DOM is not a feed grid; go back to the feed first
                await self._goto(page, _DOUYIN_JINGXUAN, wait_ms=3500)
            entries = await self._feed_entries(page, max_items=max(slot * 2, 10))
            if entries:
                self._write_feed_snapshot(entries, source="implicit_feed_read", page=page)
                snap_item = self._snapshot_slot(slot)
                target = str(snap_item.get("aweme_id") or "")
                snapshot_state = "retaken_now" if target else "missing"
        result: dict[str, Any] = {
            "slot": slot,
            "target_aweme_id": target,
            "actual_aweme_id": "",
            "aweme_match": False,
            "snapshot_state": snapshot_state,
            "snapshot_path": str(self._snapshot_path()),
            "snapshot_item": snap_item or {},
        }
        if not target:
            info = await self._current_video_info(page, max_wait=2.0)
            info.update(result)
            info["status"] = "aweme_unavailable"
            info["message"] = (
                "feed_snapshot.json 里没有这个 slot 的 aweme_id（先跑 action=feed 生成快照），"
                "也没有可用的推荐流条目；不会打开任意视频"
            )
            return info

        verify = bool(getattr(self, "_verify_aweme_id", True))
        actual = ""
        url_only = False
        attempts = 2 if verify else 1
        for attempt in range(1, attempts + 1):
            await self._goto(page, _DOUYIN_VIDEO.format(vid=target), wait_ms=1500)
            deadline = time.monotonic() + 12.0
            while True:
                ident = await self._current_aweme(page)
                actual = str(ident.get("actual") or "")
                url_only = bool(ident.get("url_only"))
                if actual == target and not url_only:
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.6)
            result["nav_attempt"] = attempt
            result["actual_aweme_id"] = actual
            result["url_only"] = url_only
            if (actual == target and not url_only) or not verify:
                break
        info = await self._current_video_info(page)
        identity = info.get("aweme_identity") or {}
        dom_actual = str(identity.get("panel") or identity.get("player_class") or "")
        if dom_actual:
            actual = dom_actual
        result["actual_aweme_id"] = actual
        result["aweme_match"] = bool(actual and actual == target)
        info.update(result)
        if actual:
            info["link"] = f"https://www.douyin.com/video/{actual}"
        if not result["aweme_match"] and verify:
            info["status"] = "mismatch"
            detail_hint = (
                f"（页面 URL 里是 {target}，但没有渲染出详情面板/播放器锚点，"
                "无法证明打开的确实是这条，按未验证处理）"
                if url_only and not actual
                else ""
            )
            info["message"] = (
                f"要操作的是 aweme_id={target}（feed_snapshot slot={slot}），"
                f"但页面实际打开的是 aweme_id={actual or '未识别'}；"
                "已按 aweme_id 重新跳转一次仍未对齐，因此不执行后续动作，"
                "也不会返回另一条视频的数据" + detail_hint
            )
            return info
        info["verification"] = "verified" if result["aweme_match"] else ("disabled" if not verify else "mismatch")
        if not info.get("video_present"):
            info.setdefault("note", "no visible <video> element")
        return info

    async def _try_video_link(self, page) -> str:
        m = re.search(r"/video/(\d{8,})", page.url or "")
        if m:
            return f"https://www.douyin.com/video/{m.group(1)}"
        cards = await self._feed_cards(page)
        for c in cards:
            if c.get("link"):
                return c["link"]
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
        title = (info.get("title") or info.get("title_guess") or "Douyin storyboard")[:60].replace("\n", " ")
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

    async def _ensure_comments_panel(self, page) -> dict[str, Any]:
        state = await page.evaluate(_JS_PANEL_STATE)
        if state.get("comment_list_present") and state.get("comment_item_count", 0) > 0:
            return state
        for sel in ("[data-e2e='feed-comment-icon']", "[aria-label*='评论']", "[title*='评论']"):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=4000)
                    await asyncio.sleep(2.0)
                    break
            except Exception:
                continue
        return await page.evaluate(_JS_PANEL_STATE)

    async def _read_comments(self, page, target: int = 30) -> dict[str, Any]:
        """Read real comment bodies from [data-e2e="comment-item"] nodes."""
        target = max(1, min(50, int(target or 30)))
        await self._ensure_comments_panel(page)
        data = await page.evaluate(_JS_COMMENTS)
        items = list(data.get("items") or [])
        prev = -1
        for _ in range(8):
            if len(items) >= target:
                break
            if len(items) == prev:
                break
            prev = len(items)
            try:
                await page.evaluate(_JS_SCROLL_COMMENTS)
            except Exception:
                pass
            await asyncio.sleep(1.3)
            data = await page.evaluate(_JS_COMMENTS)
            items = list(data.get("items") or [])
        panel = await page.evaluate(_JS_PANEL_STATE)
        result = {
            "comments_panel": bool(panel.get("comment_list_present")) and bool(items),
            "comment_list_present": bool(panel.get("comment_list_present")),
            "comment_item_count": int(panel.get("comment_item_count") or 0),
            "comment_count": len(items),
            "comments": items[:target],
            "comment_panel_title": panel.get("title_text", ""),
            "login_gate_on_input": bool(panel.get("login_gate_input")),
            "note": "评论正文取自真实评论节点；私信不在读取范围",
        }
        result["status"] = "ok" if items else ("no_comments" if panel.get("comment_list_present") else "panel_not_open")
        return result

    async def _open_comments(self, page) -> dict[str, Any]:
        return await self._read_comments(page, target=30)

    # -- editor / publish helpers (sends) ----------------------------------

    _EDITOR_SELECTORS = (
        '.comment-input-container [contenteditable="true"]',
        '.comment-input-inner-container [contenteditable="true"]',
        '[data-e2e="comment-input"] [contenteditable="true"]',
        'textarea[data-e2e="comment-input"]',
        '.comment-input-container textarea',
        'div[contenteditable="true"]',
        'textarea',
    )
    _PUBLISH_SELECTORS = (
        '[data-e2e="comment-post"]',
        '[data-e2e="comment-submit"]',
        '[data-e2e="comment-publish"]',
        '.commentInput-right-ct span.wchsYBpK',
        '.commentInput-right-ct span:last-child',
        '.comment-input-inner-container span:has-text("发送")',
        '[aria-label="发布"]',
        '[aria-label="发送"]',
        '[aria-label="发表评论"]',
        '[title="发布"]',
        '[title="发表评论"]',
        'button:has-text("发送")',
        'text=发送',
    )

    async def _fill_editor(self, page, body_text: str) -> tuple[bool, str]:
        # 1) classic editors: an existing contenteditable / textarea
        for sel in self._EDITOR_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                if not await loc.is_visible():
                    continue
                await loc.click(timeout=4000)
                try:
                    await loc.fill(body_text, timeout=4000)
                except Exception:
                    await loc.evaluate(
                        "(el, v) => { el.textContent = v; "
                        "el.dispatchEvent(new InputEvent('input', {bubbles: true})); }",
                        body_text,
                    )
                await asyncio.sleep(0.6)
                return True, sel
            except Exception:
                continue

        # 2) current douyin PC layout (measured 2026-11-01): the comment box is a
        #    plain DIV — div.comment-input-inner-container > div._x9Gwl7G with the
        #    placeholder text "留下你的精彩评论吧". There is NO contenteditable and
        #    NO textarea until the container is clicked; the editable node is created
        #    on demand. So: activate by click, re-look for a contenteditable, and
        #    finally fall back to real keystrokes + a read-back check.
        container = page.locator(".comment-input-inner-container").first
        try:
            await container.wait_for(state="visible", timeout=8000)
        except Exception:
            return False, ""
        try:
            await container.scroll_into_view_if_needed()
            ce = page.locator('.comment-input-inner-container [contenteditable="true"]').first
            activated = False
            # 2026-11-03 实测：send_comment（不点「回复」直接发顶级评论）时，输入框
            # 容器在，但点一次不一定会生成 contenteditable；reply 之所以稳，是因为
            # _reply_trigger 先点过「回复」把输入框激活了。这里改成点击→等待→再检查，
            # 最多试 3 次，避免"容器在却激活不出来"直接判失败。
            for _attempt in range(3):
                try:
                    await container.click(timeout=4000)
                except Exception:
                    pass
                await asyncio.sleep(0.9 if _attempt == 0 else 1.2)
                try:
                    if await ce.count() and await ce.is_visible():
                        activated = True
                        break
                except Exception:
                    pass
            if activated:
                await ce.click(timeout=3000)
                try:
                    await ce.focus()
                except Exception:
                    pass
                await asyncio.sleep(0.2)
                # React/自研框架只认真实键盘事件：DOM fill()/textContent 赋值不会触发
                # 状态同步，会出现「看着填进去了、点发送时内容其实是空的」（2026-11-01 实测）。
                await page.keyboard.type(body_text, delay=40)
                await asyncio.sleep(0.5)
                typed = await ce.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
                if body_text[:6] and body_text[:6] in (typed or ""):
                    return True, 'keyboard:.comment-input-inner-container [contenteditable="true"]'
            await page.keyboard.type(body_text, delay=45)
            await asyncio.sleep(0.7)
            typed = await page.evaluate(
                "() => { const c = document.querySelector('.comment-input-inner-container'); "
                "return c ? (c.innerText || '').trim() : ''; }"
            )
            if body_text[:6] and body_text[:6] in (typed or ""):
                return True, 'keyboard:.comment-input-inner-container'
        except Exception:
            return False, ""
        return False, ""

    async def _click_publish(self, page) -> bool:
        for sel in self._PUBLISH_SELECTORS:
            try:
                loc = page.locator(sel).last
                if await loc.count() == 0:
                    continue
                if not await loc.is_visible():
                    continue
                await loc.click(timeout=4000)
                await asyncio.sleep(2.2)
                return True
            except Exception:
                continue
        return False

    def _editor_container(self, page):
        return page.locator('.comment-input-container, [data-e2e="comment-input"]').first

    async def _compose_and_send(self, page, body_text: str) -> dict[str, Any]:
        ok, sel = await self._fill_editor(page, body_text)
        if not ok:
            shot = await self._shot(page, "send_input_missing")
            return {
                "filled": False,
                "sent": False,
                "editor_selector": "",
                "message": "输入框未定位到（可能未登录 / 评论面板未打开 / 页面结构变化）",
                "screenshot": shot,
            }
        sent = await self._click_publish(page)
        return {"filled": True, "editor_selector": sel, "sent": sent}

    async def _reply_trigger(self, page, index: int) -> dict[str, Any]:
        try:
            return await page.evaluate(_JS_CLICK_REPLY, index)
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    # -- search -------------------------------------------------------------

    @staticmethod
    def _normalize_search_attempt(info: dict[str, Any]) -> dict[str, Any]:
        return {
            "entry": info.get("entry", ""),
            "url": info.get("url", ""),
            "http_status": info.get("http_status"),
            "cards": int(info.get("cards") or 0),
            "duration_lines": info.get("duration_lines"),
            "body_len": info.get("body_len"),
            "login_wall": bool(info.get("login_wall")),
            "ok": bool(info.get("ok")),
            "reason": info.get("reason", ""),
        }

    async def _search_attempt(self, page, url: str, wait_ms: int = 6000) -> dict[str, Any]:
        http_status = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT * 1000)
            http_status = resp.status if resp else None
        except Exception as exc:
            http_status = f"error:{exc}"
        await asyncio.sleep(wait_ms / 1000)
        info = await page.evaluate(
            """
            () => {
              const body = document.body ? document.body.innerText : '';
              return {
                url: location.href,
                title: document.title,
                cards: document.querySelectorAll('a[href*="/video/"]').length,
                duration_lines: body.split('\\n').map(s => s.trim()).filter(l => /^\\d{1,2}:\\d{2}(:\\d{2})?$/.test(l)).length,
                body_len: body.length,
                body_head: body.slice(0, 600)
              };
            }
            """
        )
        info["http_status"] = http_status
        info["login_wall"] = _is_login_wall(info.get("body_head", ""))
        info["ok"] = bool(info.get("cards", 0) > 0)
        return info

    async def _search_gesture(self, page, keyword: str) -> dict[str, Any]:
        try:
            await self._goto(page, _DOUYIN_JINGXUAN, wait_ms=3500)
            box = page.locator("[data-e2e='searchbar-input']").first
            if await box.count() == 0:
                return {"entry": "gesture_searchbar", "ok": False, "reason": "searchbar not found", "url": page.url}
            await box.click(timeout=8000)
            await asyncio.sleep(0.8)
            await box.type(keyword, delay=90)
            await asyncio.sleep(1.2)
            await page.keyboard.press("Enter")
            await asyncio.sleep(7)
            info = await page.evaluate(
                """
                () => {
                  const body = document.body ? document.body.innerText : '';
                  return {url: location.href, title: document.title,
                          cards: document.querySelectorAll('[data-aweme-id]').length,
                          body_head: body.slice(0, 600), body_len: body.length};
                }
                """
            )
            login_panel = await page.evaluate(
                "() => document.querySelectorAll('[id^=\"login-full-panel\"], .douyin_login_new_class').length"
            )
            info["login_panel_elements"] = int(login_panel)
            info["http_status"] = None
            info["login_wall"] = _is_login_wall(info.get("body_head", "")) or int(login_panel) > 0
            info["ok"] = bool(info.get("cards", 0) > 0) and not info["login_wall"]
            info["entry"] = "gesture_searchbar"
            return info
        except Exception as exc:
            return {
                "entry": "gesture_searchbar",
                "ok": False,
                "reason": str(exc),
                "url": page.url,
                "http_status": None,
                "cards": 0,
                "login_wall": False,
            }

    async def _act_search(self, page, keyword: str | None, max_items: int) -> dict[str, Any]:
        kw = (keyword or "").strip()
        if not kw:
            return {"status": "error", "message": "keyword required for search"}
        q = quote(kw)
        attempts: list[dict[str, Any]] = []
        result_items: list[dict[str, Any]] = []
        winner = ""

        entries = [
            ("search_video_type", _DOUYIN_SEARCH.format(q=q)),
            ("search_general", _DOUYIN_SEARCH_GENERAL.format(q=q)),
            ("search_ai", _DOUYIN_AISEARCH.format(q=q)),
        ]
        for name, url in entries:
            info = await self._search_attempt(page, url)
            info["entry"] = name
            attempts.append(self._normalize_search_attempt(info))
            if info.get("ok"):
                winner = name
                break
        if not winner:
            info = await self._search_gesture(page, kw)
            attempts.append(self._normalize_search_attempt(info))
            if info.get("ok"):
                winner = "gesture_searchbar"

        shot = await self._shot(page, "search_blocked" if not winner else "search_ok")
        for a in attempts:
            a.pop("body_head", None) if not winner else None
        if winner:
            await self._load_more_feed(page, rounds=1)
            result_items = await self._search_cards(page, max_items=max_items)
            self._record_seen(result_items)
            login = await self._detect_login(page)
            if result_items:
                self._write_feed_snapshot(result_items[:max_items], source="search", page=page)
                return {
                    "status": "ok",
                    "keyword": kw,
                    "entry": winner,
                    "url": page.url,
                    "items": result_items[:max_items],
                    "item_count": len(result_items),
                    "attempts": attempts,
                    "login": login,
                    "screenshot": shot,
                }
            return {
                "status": "blocked",
                "reason": "search page rendered but no video cards parsed",
                "keyword": kw,
                "entry": winner,
                "attempts": attempts,
                "login": login,
                "screenshot": shot,
            }
        login = await self._detect_login(page)
        walls = [a for a in attempts if a.get("login_wall")]
        return {
            "status": "blocked",
            "reason": (
                "all search entries were gated by the douyin login wall (登录后即可搜索更多精彩视频) — "
                "NOT a gateway 502; results never render while logged out"
                if walls
                else "all search entries returned an empty shell / no video cards"
            ),
            "keyword": kw,
            "attempts": attempts,
            "login": login,
            "screenshot": shot,
            "advice": "完成扫码登录后重试 search；未登录时抖音搜索页只返回登录墙，工具不会伪造结果",
        }

    # -- actions ------------------------------------------------------------

    async def _act_open(self, page) -> dict[str, Any]:
        await self._goto(page, _DOUYIN_HOME, wait_ms=4500)
        await self._dismiss_overlays(page, press_escape=False)
        login = await self._detect_login(page)
        prompted = False
        if not login["logged_in"]:
            # surface the QR login modal for the human (do NOT press Escape here)
            try:
                if not login.get("login_modal"):
                    for sel in ("div:text-is('登录')", "button:text-is('登录')", "[data-e2e='login-button']"):
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            await loc.click(timeout=4000)
                            prompted = True
                            await asyncio.sleep(2.0)
                            break
            except Exception:
                prompted = False
            login = await self._detect_login(page)
        shot = await self._shot(page, "open_home")
        title = await page.title()
        url = page.url
        payload: dict[str, Any] = {
            "page_title": title,
            "url": url,
            "screenshot": shot,
            "login": login,
            "login_prompt_opened": prompted,
            "session": self._session_info,
            "login_state_file": self._login_state_summary(),
        }
        if login["logged_in"]:
            export = await self._export_login_state(page, login)
            payload["login_state_export"] = export
            payload["status"] = "ok"
        else:
            payload["status"] = "login_required"
            payload["message"] = (
                "请在打开的 Edge 窗口里扫码/手机确认登录抖音；完成后调用 login_check 继续。工具不会伪造登录。"
            )
        return payload

    async def _act_login_status(self, page) -> dict[str, Any]:
        if "douyin.com" not in (page.url or ""):
            await self._goto(page, _DOUYIN_HOME, wait_ms=3500)
        login = await self._detect_login(page)
        shot = await self._shot(page, "login_status")
        payload = {
            "status": "ok",
            "logged_in": login["logged_in"],
            "basis": login["basis"],
            "cookie_valid": login["cookie_valid"],
            "login": login,
            "screenshot": shot,
            "session": self._session_info,
            "login_state_file": self._login_state_summary(),
            "login_state_acl": await self._acl_status(),
        }
        if login["logged_in"]:
            payload["login_state_export"] = await self._export_login_state(page, login)
        return payload

    async def _act_login_check(self, page) -> dict[str, Any]:
        login = await self._detect_login(page)
        shot = await self._shot(page, "login_check")
        if login["logged_in"]:
            export = await self._export_login_state(page, login)
            return {
                "status": "ok",
                "message": "已确认登录（服务端/UI 校验通过）；登录态已导出到 login_state.json，发送类动作可进入许可检查",
                "logged_in": True,
                "basis": login["basis"],
                "cookie_valid": login["cookie_valid"],
                "login": login,
                "login_state_export": export,
                "screenshot": shot,
            }
        return {
            "status": "not_logged_in",
            "message": "仍未检测到有效登录态：请在打开的 Edge 窗口完成扫码/手机确认后再调用 login_check",
            "logged_in": False,
            "basis": login["basis"],
            "cookie_valid": login["cookie_valid"],
            "login": login,
            "screenshot": shot,
        }

    async def _act_login_export(self, page) -> dict[str, Any]:
        login = await self._detect_login(page)
        export = await self._export_login_state(page, login)
        return {
            "status": "ok" if export.get("exported") else "error",
            "logged_in": login["logged_in"],
            "basis": login["basis"],
            "cookie_valid": login["cookie_valid"],
            "login_state_export": export,
            "login_state_file": self._login_state_summary(),
            "screenshot": await self._shot(page, "login_export"),
        }

    async def _act_login_restore(self, context, page) -> dict[str, Any]:
        restore = await self._restore_login_state(context)
        login = await self._detect_login(page)
        return {
            "status": "ok",
            "restore": restore,
            "logged_in": login["logged_in"],
            "basis": login["basis"],
            "cookie_valid": login["cookie_valid"],
            "login": login,
            "login_state_file": self._login_state_summary(),
            "screenshot": await self._shot(page, "login_restore"),
        }

    async def _act_feed(self, page, keyword: str | None, max_items: int) -> dict[str, Any]:
        url = page.url or ""
        # slot numbering always means "this feed grid": never read cards off a video
        # page (its DOM has no [data-aweme-id] grid, which used to yield empty ids and
        # garbage entries through the innerText fallback)
        if "/video/" in url or "jingxuan" not in url:
            await self._goto(page, _DOUYIN_JINGXUAN, wait_ms=3500)
        await self._dismiss_overlays(page, press_escape=False)
        entries = await self._feed_entries(page, max_items=max_items, keyword=keyword)
        if len(entries) < max_items:
            await self._load_more_feed(page, rounds=2)
            entries = await self._feed_entries(page, max_items=max_items, keyword=keyword)
        self._record_seen(entries)
        snap = self._write_feed_snapshot(entries, source="feed", page=page)
        id_missing = sum(1 for it in snap.get("items", []) if not it.get("aweme_id"))
        snapshot_payload = {
            "path": str(self._snapshot_path()),
            "written_at": snap.get("written_at"),
            "item_count": snap.get("item_count", 0),
            "write_error": snap.get("write_error", ""),
            "aweme_id_missing_count": id_missing,
            "slots": [
                {"slot": it.get("slot"), "aweme_id": it.get("aweme_id"), "title": (it.get("title") or "")[:60]}
                for it in snap.get("items", [])
            ],
            "note": (
                "随后 video/comments/storyboard/send_* 的 slot 都以这份快照为准；"
                "打开的 aweme_id 与之不一致会报 status=mismatch"
                + ("；本次有条目缺 aweme_id，这些 slot 将报 aweme_unavailable" if id_missing else "")
            ),
        }
        login = await self._detect_login(page)
        shot = await self._shot(page, "feed")
        payload = {
            "status": "ok",
            "items": entries[:max_items],
            "item_count": len(entries),
            "login": login,
            "screenshot": shot,
            "session": self._session_info,
            "feed_snapshot": snapshot_payload,
        }
        if login["logged_in"]:
            payload["login_state_export"] = await self._export_login_state(page, login)
        return payload

    async def _act_video(self, page, slot: int) -> dict[str, Any]:
        info = await self._open_player(page, slot)
        target = info.get("target_aweme_id") or ""
        actual = info.get("actual_aweme_id") or ""
        if info.get("status") in ("mismatch", "aweme_unavailable"):
            return {
                "status": info.get("status"),
                "slot": slot,
                "target_aweme_id": target,
                "actual_aweme_id": actual,
                "aweme_match": bool(info.get("aweme_match")),
                "message": info.get("message", ""),
                "info": info,
                "screenshot": await self._shot(page, "video_mismatch"),
            }
        link = info.get("link") or (f"https://www.douyin.com/video/{actual}" if actual else "")
        shot = await self._shot(page, "video_info")
        return {
            "status": "ok",
            "slot": slot,
            "target_aweme_id": target,
            "actual_aweme_id": actual,
            "aweme_match": bool(info.get("aweme_match")),
            "info": info,
            "link": link,
            "screenshot": shot,
        }

    async def _read_like_state(self, page) -> dict[str, Any]:
        try:
            got = await page.evaluate(_JS_LIKE_STATE)
        except Exception:
            got = {}
        return got if isinstance(got, dict) else {}

    async def _wait_like_stable(self, page, tries: int = 8, gap: float = 0.5) -> dict[str, Any]:
        """Player like state may hydrate a beat after the page mounts; keep reading until
        two consecutive samples agree on state+count (or give up and return the last sample)."""
        prev = {}
        for _ in range(tries):
            cur = await self._read_like_state(page)
            if (
                cur.get("present")
                and prev.get("present")
                and cur.get("state") == prev.get("state")
                and cur.get("count_text") == prev.get("count_text")
            ):
                return cur
            prev = cur
            await asyncio.sleep(gap)
        return prev or {}

    async def _click_like_button(self, page) -> str:
        """Try plain click -> hover+force click -> JS dispatch. Player controls may need a hover first."""
        sel = '[data-e2e="video-player-digg"]'
        btn = page.locator(sel)
        try:
            await btn.click(timeout=5000)
            return "click"
        except Exception:
            pass
        try:
            box = await btn.bounding_box()
            if box:
                await page.mouse.move(
                    box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                )
                await asyncio.sleep(0.4)
            await btn.click(timeout=5000, force=True)
            return "force_click"
        except Exception:
            pass
        try:
            ok = await page.evaluate(_JS_LIKE_CLICK)
            if ok:
                return "js_click"
        except Exception:
            pass
        return "failed"

    async def _act_like(self, page, slot: int | None, aweme_id: str | None, want_like: bool) -> dict[str, Any]:
        """Like (digg) / unlike a video by clicking [data-e2e="video-player-digg"], then re-reading state to verify."""
        if aweme_id:
            info = await self._open_video_by_id(page, aweme_id)
        else:
            info = await self._open_player(page, slot)
        target = info.get("target_aweme_id") or ""
        actual = info.get("actual_aweme_id") or ""
        if info.get("status") in ("mismatch", "aweme_unavailable") or not info.get("aweme_match"):
            return {
                "status": info.get("status", "mismatch"),
                "slot": slot,
                "aweme_id": aweme_id,
                "target_aweme_id": target,
                "actual_aweme_id": actual,
                "aweme_match": bool(info.get("aweme_match")),
                "message": info.get("message", "视频身份校验未通过，未点赞"),
                "screenshot": await self._shot(page, "like_mismatch"),
            }
        before = await self._wait_like_stable(page)
        if not before.get("present"):
            return {
                "status": "error",
                "aweme_id": actual,
                "like": want_like,
                "message": "未找到点赞按钮 [data-e2e=\"video-player-digg\"]（页面结构变化或视频未就绪）",
                "before": before,
                "screenshot": await self._shot(page, "like_no_button"),
            }
        if bool(before.get("liked")) == want_like:
            return {
                "status": "ok",
                "action": "like",
                "like": want_like,
                "changed": False,
                "aweme_id": actual,
                "aweme_match": True,
                "before": before,
                "after": before,
                "message": "已是目标状态，无需操作（幂等）",
                "screenshot": await self._shot(page, "like_unchanged"),
            }
        how = await self._click_like_button(page)
        if how == "failed":
            return {
                "status": "error",
                "like": want_like,
                "aweme_id": actual,
                "message": "点赞按钮点击失败（普通/强制/JS 派发均未成功）",
                "before": before,
                "screenshot": await self._shot(page, "like_click_failed"),
            }
        await asyncio.sleep(1.4)
        after = await self._wait_like_stable(page)
        ui_ok = bool(after.get("liked")) == want_like
        # 点赞按钮是前端乐观更新：UI 变红 + 计数 +1 不代表服务端真的记了这次点赞。
        # 必须 reload 一次再读，才能区分「真的点上了」和「只是页面上变了」。
        persisted: dict[str, Any] = {}
        persist_ok = False
        if ui_ok:
            try:
                await page.reload(wait_until="domcontentloaded")
                reidentified = False
                for _ in range(10):
                    await asyncio.sleep(0.8)
                    cur = await self._current_aweme(page)
                    if str(cur.get("actual") or "") == actual:
                        reidentified = True
                        break
                if reidentified:
                    persisted = await self._wait_like_stable(page)
                    persist_ok = bool(persisted.get("liked")) == want_like
                else:
                    persisted = {"skipped": "identity_unverified_after_reload"}
            except Exception as exc:
                persisted = {"error": f"reload_failed: {exc.__class__.__name__}"}
        ok = ui_ok and persist_ok
        skipped = bool(persisted.get("skipped")) or bool(persisted.get("error"))
        if ok:
            status, receipt, message = (
                "ok",
                "confirmed",
                "点赞/取消成功，重新加载后状态保持（已持久化）",
            )
        elif ui_ok and skipped:
            status, receipt, message = (
                "persist_unverified",
                "未核验",
                "UI 已切换且计数变化，但刷新后没能重新确认视频身份，持久化未核验（既不算成功也不断言失败）",
            )
        elif ui_ok:
            status, receipt, message = (
                "not_persisted",
                "未持久化",
                "UI 已切换且计数变化，但重新加载后状态回退：服务端未接受这次操作，不能算点赞成功",
            )
        else:
            status, receipt, message = (
                "not_confirm",
                "未确认送达",
                "点击已执行，但回读状态未匹配，请人工核对",
            )
        return {
            "status": status,
            "action": "like",
            "like": want_like,
            "changed": ok
            and (
                bool(after.get("liked")) != bool(before.get("liked"))
                or after.get("count") != before.get("count")
            ),
            "aweme_id": actual,
            "aweme_match": True,
            "ui_confirmed": ui_ok,
            "persisted": persist_ok,
            "before": before,
            "after": after,
            "after_reload": persisted,
            "receipt": receipt,
            "message": message,
            "screenshot": await self._shot(page, "like_done" if ok else "like_unconfirmed"),
        }

    async def _act_storyboard(self, page, slot: int, frame_count: int) -> dict[str, Any]:
        info = await self._open_player(page, slot)
        target = info.get("target_aweme_id") or ""
        actual = info.get("actual_aweme_id") or ""
        if info.get("status") in ("mismatch", "aweme_unavailable"):
            return {
                "status": info.get("status"),
                "slot": slot,
                "target_aweme_id": target,
                "actual_aweme_id": actual,
                "aweme_match": bool(info.get("aweme_match")),
                "message": info.get("message", ""),
                "info": info,
                "screenshot": await self._shot(page, "storyboard_mismatch"),
            }
        result, info = await self._storyboard(page, frame_count)
        # re-verify identity after sampling: the page may have auto-advanced while we
        # sampled frames, and those cells would then belong to another video
        after = await self._current_aweme(page)
        actual_after = str(after.get("actual") or "")
        drift = bool(target) and bool(actual_after) and actual_after != target
        info["actual_aweme_id_after"] = actual_after
        info["video_swapped_during_sampling"] = drift
        result["info"] = info
        result["slot"] = slot
        result["target_aweme_id"] = target
        result["actual_aweme_id"] = actual_after or actual
        result["aweme_match"] = bool(info.get("aweme_match")) and not drift
        result["screenshot"] = await self._shot(page, "storyboard_state")
        if drift:
            result["status"] = "mismatch"
            result["message"] = (
                f"采样期间页面换片：目标是 {target}，采样后变成 {actual_after}；"
                "分镜格可能属于另一条视频，已按 mismatch 报告，请重试"
            )
        return result

    async def _act_comments(self, page, slot: int, max_items: int) -> dict[str, Any]:
        info = await self._open_player(page, slot)
        target = info.get("target_aweme_id") or ""
        actual = info.get("actual_aweme_id") or ""
        if info.get("status") in ("mismatch", "aweme_unavailable"):
            return {
                "status": info.get("status"),
                "slot": slot,
                "target_aweme_id": target,
                "actual_aweme_id": actual,
                "aweme_match": bool(info.get("aweme_match")),
                "message": info.get("message", ""),
                "info": info,
                "screenshot": await self._shot(page, "comments_mismatch"),
            }
        comments = await self._read_comments(page, target=max_items)
        # re-verify identity after reading: the page can auto-advance mid-read, and the
        # comment list we just read would then belong to another video
        after = await self._current_aweme(page)
        actual_after = str(after.get("actual") or "")
        drift = bool(target) and bool(actual_after) and actual_after != target
        payload = dict(comments)
        payload.update(
            {
                "slot": slot,
                "target_aweme_id": target,
                "actual_aweme_id": actual,
                "actual_aweme_id_after": actual_after,
                "aweme_match": bool(info.get("aweme_match")),
                "video_swapped_during_read": drift,
                "info": info,
                "screenshot": await self._shot(page, "comments"),
            }
        )
        if drift:
            payload["status"] = "mismatch"
            payload["message"] = (
                f"读取评论期间页面换片：目标是 {target}，读完变成 {actual_after}；"
                "评论正文可能属于另一条视频，因此不返回这些评论，请重试"
            )
            payload["comments"] = []
            payload["comment_count"] = 0
            payload["comment_item_count"] = 0
        return payload

    async def _act_watch_report(self, page, keyword: str | None, max_items: int, include_frames: bool, frame_count: int) -> dict[str, Any]:
        kw = (keyword or "").strip()
        notes: list[str] = []
        await self._goto(page, _DOUYIN_HOME, wait_ms=3500)
        await self._dismiss_overlays(page, press_escape=False)
        entries = await self._feed_entries(page, max_items=max_items, keyword=kw or None)
        if not entries and kw:
            notes.append(f"推荐流中没有命中「{kw}」的条目；已尝试在精选/推荐流只读浏览并如实记录")
        if not entries:
            entries = await self._feed_entries(page, max_items=max_items)
        self._record_seen(entries)
        snap = self._write_feed_snapshot(entries, source="watch_report", page=page)
        enriched: list[dict[str, Any]] = []
        frames_for = min(3, max_items, len(entries)) if include_frames else 0
        blocked_count = 0
        for idx in range(len(entries)):
            item = dict(entries[idx])
            item["index"] = idx + 1
            item["frame_path"] = ""
            if idx < frames_for:
                info = await self._open_player(page, slot=idx + 1)
                item["target_aweme_id"] = info.get("target_aweme_id", "")
                item["actual_aweme_id"] = info.get("actual_aweme_id", "")
                item["aweme_match"] = bool(info.get("aweme_match"))
                item["detail"] = info
                if info.get("status") == "mismatch":
                    notes.append(
                        f"slot {idx + 1} 打开的视频与 feed_snapshot 不一致"
                        f"（目标 {info.get('target_aweme_id')} / 实际 {info.get('actual_aweme_id')}），已按 mismatch 跳过采样"
                    )
                    blocked_count += 1
                elif info.get("video_present"):
                    item["link"] = info.get("link") or await self._try_video_link(page)
                    if include_frames:
                        sb, _ = await self._storyboard(page, frame_count)
                        item["frame_path"] = sb.get("sheet_path", "")
                    else:
                        blocked_count += 1
                else:
                    blocked_count += 1
            enriched.append(item)
        login = await self._detect_login(page)
        if not login["logged_in"]:
            notes.append("未登录状态；搜索/部分视频页可能被登录墙拦截")
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
            "feed_snapshot": {
                "path": str(self._snapshot_path()),
                "written_at": snap.get("written_at"),
                "item_count": snap.get("item_count", 0),
                "slots": [
                    {"slot": it.get("slot"), "aweme_id": it.get("aweme_id")}
                    for it in snap.get("items", [])
                ],
            },
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
            lines.append(f"- 值得看：{why}")
            if e.get("frame_path"):
                lines.append(f"- 分镜图：{e['frame_path']}")
            lines.append("")
        out = self.output_root / "reports" / f"{_day_key()}_{int(time.time()*1000)}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
        return str(out)

    def _why_worth(self, e: dict[str, Any]) -> str:
        reasons: list[str] = []
        likes = e.get("likes") or 0
        if likes >= 10000:
            reasons.append(f"点赞 {e.get('likes_text') or likes}，热度高")
        elif likes >= 1000:
            reasons.append(f"点赞 {e.get('likes_text') or likes}，热度中等")
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
            return {"status": "blocked", "reason": reason, "policy_path": str(self.state_dir / "send_policy.json")}
        info = await self._open_player(page, slot)
        if info.get("status") in ("mismatch", "aweme_unavailable"):
            return {
                "status": info.get("status"),
                "action": "send_comment",
                "slot": slot,
                "target_aweme_id": info.get("target_aweme_id") or "",
                "actual_aweme_id": info.get("actual_aweme_id") or "",
                "aweme_match": bool(info.get("aweme_match")),
                "message": info.get("message", ""),
                "info": info,
                "screenshot": await self._shot(page, "send_comment_mismatch"),
            }
        if not info.get("video_present"):
            return {"status": "blocked", "reason": "player not open (login wall or page change); send aborted"}
        login = await self._detect_login(page)
        if not login["logged_in"]:
            return {
                "status": "blocked",
                "reason": f"page says not logged in ({login['basis']}); send aborted, nothing was sent",
                "login": login,
                "screenshot": await self._shot(page, "send_comment_not_logged_in"),
            }
        await self._ensure_comments_panel(page)
        send = await self._compose_and_send(page, body_text)
        receipt = "未确认送达"
        # 第一次核对（发送后立即）：抖音会把新评论乐观插入列表，"立刻读到"不等于真的公开。
        # 必须再做一次延迟核对，确认评论仍在列表里（2026-11-01 实测：立即读到 →
        # confirmed，25 秒后列表里已消失 → 未公开/审核中）。只报未确认送达，绝不谎报成功。
        comments = await self._read_comments(page, target=30)
        joined = " ".join(c["text"] for c in comments.get("comments", []))
        if send.get("sent") and body_text[:20] in joined:
            await asyncio.sleep(4.0)
            comments2 = await self._read_comments(page, target=30)
            joined2 = " ".join(c["text"] for c in comments2.get("comments", []))
            if body_text[:20] in joined2:
                receipt = "confirmed"
            else:
                receipt = "pending_review_or_not_visible"
        today = self._record_send_usage("send_comment")
        shot = await self._shot(page, "send_comment_result")
        return {
            "status": "sent_attempted" if send.get("sent") else "not_sent",
            "receipt": receipt,
            "message": (
                "发送完成并做回执核对；核对不到即如实报「未确认送达」"
                if send.get("sent")
                else (send.get("message") or "输入框/发送按钮未定位到，未发送")
            ),
            "fill_error": send.get("message", ""),
            "editor_filled": bool(send.get("filled")),
            "text_snippet": body_text[:40],
            "editor_selector": send.get("editor_selector", ""),
            "comments_after": comments.get("comment_count", 0),
            "usage_today": today.get("send_comment", 0),
            "screenshot": shot,
        }

    async def _act_reply_to_comment(
        self,
        page,
        slot: int,
        text: str | None,
        comment_index: int | None,
        comment_text_match: str | None,
        allow_send: bool,
        aweme_id: str | None = None,
    ) -> dict[str, Any]:
        body_text = (text or "").strip()
        if not body_text:
            return {"status": "error", "message": "text required for reply_to_comment"}
        ok, reason = self._check_send_gate("reply_to_comment", allow_send)
        if not ok:
            return {
                "status": "blocked",
                "reason": reason,
                "nothing_happened": True,
                "policy_path": str(self.state_dir / "send_policy.json"),
                "policy_hint": "用户本人需在 send_policy.json 设 enabled=true 且 allow_reply=true，并在调用时传 allow_send=true",
            }
        # Identity: an explicit aweme_id (e.g. taken from an 互动消息 notification) wins over the
        # feed slot; the slot path keeps its old behaviour (feed_snapshot.json + verification).
        if aweme_id:
            info = await self._open_video_by_id(page, aweme_id)
        else:
            info = await self._open_player(page, slot)
        if info.get("status") in ("mismatch", "aweme_unavailable"):
            return {
                "status": info.get("status"),
                "action": "reply_to_comment",
                "slot": slot,
                "identity_source": info.get("identity_source") or "feed_slot",
                "target_aweme_id": info.get("target_aweme_id") or "",
                "actual_aweme_id": info.get("actual_aweme_id") or "",
                "aweme_match": bool(info.get("aweme_match")),
                "message": info.get("message", ""),
                "nothing_happened": True,
                "info": info,
                "screenshot": await self._shot(page, "reply_mismatch"),
            }
        if not info.get("video_present"):
            return {"status": "blocked", "reason": "player not open (login wall or page change); reply aborted", "nothing_happened": True}
        second_verify = await self._check_second_verify(page)
        if second_verify.get("blocked"):
            return {
                "status": "blocked_second_verify",
                "reason": (
                    "页面出现短信/账号安全二次验证遮罩：按要求立即中止，不绕过、不重试，"
                    "没有输入任何内容，也没有发送任何内容"
                ),
                "second_verify": second_verify,
                "nothing_happened": True,
                "screenshot": await self._shot(page, "reply_second_verify"),
            }
        login = await self._detect_login(page)
        if not login["logged_in"]:
            return {
                "status": "blocked",
                "reason": f"page says not logged in ({login['basis']}); reply aborted, nothing was sent",
                "login": login,
                "screenshot": await self._shot(page, "reply_not_logged_in"),
            }
        comments = await self._read_comments(page, target=30)
        items = comments.get("comments", [])
        target_idx = None
        needle = (comment_text_match or "").strip()
        if comment_index is not None:
            idx = int(comment_index) - 1
            if 0 <= idx < len(items):
                target_idx = idx
        if target_idx is None and needle:
            for i, c in enumerate(items):
                if needle in c.get("text", "") or needle in c.get("author", ""):
                    target_idx = i
                    break
        if target_idx is None:
            return {
                "status": "error",
                "message": "目标评论未定位到（comment_index / comment_text_match 都没有命中）",
                "comment_count": len(items),
                "comments_sample": [{"index": c["index"], "author": c["author"], "text": c["text"][:80]} for c in items[:10]],
                "screenshot": await self._shot(page, "reply_target_missing"),
            }
        target = items[target_idx]
        raw_idx = int(target.get("raw_index") or (target_idx + 1)) - 1
        trigger = await self._reply_trigger(page, raw_idx)
        if not trigger.get("ok"):
            return {
                "status": "error",
                "message": f"未能点开该评论的回复入口：{trigger.get('reason')}",
                "target": target,
                "screenshot": await self._shot(page, "reply_trigger_missing"),
            }
        await asyncio.sleep(1.5)
        send = await self._compose_and_send(page, body_text)
        if not send.get("filled"):
            return {
                "status": "error",
                "message": send.get("message", "回复输入框未定位到"),
                "target": target,
                "screenshot": send.get("screenshot", ""),
            }
        # receipt: (a) expand the target thread and look there, (b) fall back to panel-wide text
        confirmed = False
        receipt_detail = ""
        try:
            await page.evaluate(
                "() => { const els = Array.from(document.querySelectorAll('span,div,a')); "
                "for (const el of els) { const t = (el.innerText || '').trim(); "
                "if (/^展开\\s*\\d+\\s*条回复$/.test(t)) { el.click(); break; } } }"
            )
        except Exception:
            pass
        await asyncio.sleep(2.0)
        after = await self._read_comments(page, target=30)
        for c in after.get("comments", []):
            if body_text[:20] in c.get("text", ""):
                confirmed = True
                receipt_detail = "该楼/面板中检索到回复文本"
                break
        if not confirmed:
            try:
                present = await page.evaluate(_JS_TEXT_PRESENT, body_text[:20])
            except Exception:
                present = False
            if present:
                confirmed = True
                receipt_detail = "页面文本中检索到回复文本"
        today = self._record_send_usage("reply_to_comment")
        shot = await self._shot(page, "reply_result")
        return {
            "status": "sent_attempted" if send.get("sent") else "not_sent",
            "receipt": "confirmed" if confirmed else "未确认送达",
            "receipt_detail": receipt_detail,
            "message": (
                "回复已发送并做回执核对"
                if confirmed
                else "已尝试发送，但回执核对未通过，如实报「未确认送达」，需人工核对"
            ),
            "target": target,
            "text_snippet": body_text[:40],
            "editor_selector": send.get("editor_selector", ""),
            "usage_today": today.get("reply_to_comment", 0),
            "screenshot": shot,
        }

    async def _act_send_forward(self, page, slot: int, allow_send: bool) -> dict[str, Any]:
        ok, reason = self._check_send_gate("send_forward", allow_send)
        if not ok:
            return {"status": "blocked", "reason": reason}
        info = await self._open_player(page, slot)
        if info.get("status") in ("mismatch", "aweme_unavailable"):
            return {
                "status": info.get("status"),
                "action": "send_forward",
                "slot": slot,
                "target_aweme_id": info.get("target_aweme_id") or "",
                "actual_aweme_id": info.get("actual_aweme_id") or "",
                "aweme_match": bool(info.get("aweme_match")),
                "message": info.get("message", ""),
                "info": info,
                "screenshot": await self._shot(page, "forward_mismatch"),
            }
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
        today = self._record_send_usage("send_forward")
        return {
            "status": "panel_clicked" if clicked else "panel_not_found",
            "receipt": "未确认送达",
            "message": "已打开官方面板尝试转发；平台未给出发送回执，如实报「未确认送达」，需要人工核对",
            "screenshot": shot,
            "usage_today": today.get("send_forward", 0),
        }

    # -- publish (creator.douyin.com upload page; draft by default) ---------

    @staticmethod
    def _normalize_tags(tags: Any) -> list[str]:
        """Accept an array or a comma / 、 / ； / space separated string of #tags."""
        raw: list[Any] = []
        if isinstance(tags, (list, tuple)):
            raw = list(tags)
        elif isinstance(tags, str):
            raw = [t for t in re.split(r"[,，;；\s]+", tags) if t]
        out: list[str] = []
        for item in raw:
            token = re.sub(r"\s+", "", str(item or "").strip().lstrip("#").strip())
            if token and token not in out:
                out.append(token)
        return out

    async def _check_second_verify(self, page) -> dict[str, Any]:
        """Detect Douyin's SMS / account-safety mask. On sight we stop — never bypass,
        never auto-retry (the user must finish verification by hand)."""
        info: dict[str, Any] = {"element": False, "sms_text": False}
        try:
            info = await page.evaluate(
                """() => {
      const sels = ['#uc-second-verify', '.uc-second-verify', '[class*="second-verify"]'];
      const element = sels.some(s => !!document.querySelector(s));
      const txt = document.body ? (document.body.innerText || '') : '';
      return {element, sms_text: /短信验证|手机验证码|账号安全验证|请完成安全验证/.test(txt)};
    }"""
            )
        except Exception as exc:
            info = {"element": False, "sms_text": False, "probe_error": str(exc)}
        info["blocked"] = bool(info.get("element") or info.get("sms_text"))
        return info

    async def _creator_page_probe(self, page) -> dict[str, Any]:
        """Anchor-based probe of the creator upload page (no hashed class names)."""
        try:
            return await page.evaluate(
                """() => {
      const txt = document.body ? (document.body.innerText || '') : '';
      return {
        url: location.href,
        file_inputs: document.querySelectorAll('input[type="file"]').length,
        title_input: !!document.querySelector('input[placeholder*="作品标题"]'),
        desc_editor: !!document.querySelector('div.zone-container[contenteditable="true"]'),
        upload_shell: /点击上传|上传视频|发布视频/.test(txt),
        pending_draft_prompt: /上次未发布的视频/.test(txt),
        login_wall_text: /扫码登录|验证码登录|密码登录|登录后即可/.test(txt) && !/点击上传|上传视频/.test(txt),
        on_login_page: /\\/login|passport\\//.test(location.href),
      };
    }"""
            )
        except Exception as exc:
            return {"url": getattr(page, "url", ""), "probe_error": str(exc)}

    async def _wait_upload_ready(self, page, timeout: float = 150.0) -> dict[str, Any]:
        """Wait until the upload settles before touching the form.

        Readiness = the title input exists AND the upload/analysis indicator is either gone
        or unchanged for several consecutive polls AND the preview <video> reports data.
        Measured 2026-11-01 on creator.douyin.com: progress renders as '0%' then
        '检测中N%', and the 发布 / 暂存离开 buttons stay enabled the whole time — so button
        state alone is not a completion signal.
        """
        deadline = time.time() + timeout
        started = time.time()
        last_sig = "__init__"
        stable = 0
        last: dict[str, Any] = {}
        while time.time() < deadline:
            try:
                last = await page.evaluate(
                    """() => {
          const vis = e => !!(e && e.offsetParent !== null);
          const leaf = Array.from(document.querySelectorAll('*')).filter(e => vis(e) && e.children.length === 0);
          const marks = leaf.map(e => (e.innerText || '').trim())
              .filter(t => /^(\\d{1,3}(\\.\\d)?%|检测中\\d{1,3}%|上传中\\d{1,3}%|处理中\\d{1,3}%)$/.test(t)).slice(0, 3);
          const video = document.querySelector('video');
          const uploadingWord = leaf.some(e => /上传过程中请不要删除|正在上传/.test(e.innerText || ''));
          return {title_input: !!document.querySelector('input[placeholder*="作品标题"]'),
                  progress: marks[0] || '', uploading_word: uploadingWord,
                  video_ready: video ? (video.readyState || 0) : 0,
                  video_duration: video ? (video.duration || 0) : 0};
        }"""
                )
            except Exception as exc:
                last = {"probe_error": str(exc)}
            sig = f"{last.get('progress', '')}|{last.get('uploading_word')}"
            stable = stable + 1 if sig == last_sig else 0
            last_sig = sig
            settled = not last.get("progress") and not last.get("uploading_word")
            if last.get("title_input") and (stable >= 4 or settled and stable >= 2):
                return {
                    "ready": True,
                    "elapsed_seconds": round(time.time() - started, 1),
                    "progress": last.get("progress", ""),
                    "video_ready_state": last.get("video_ready", 0),
                    "video_duration": round(float(last.get("video_duration") or 0), 2),
                }
            await asyncio.sleep(1.5)
        return {
            "ready": False,
            "elapsed_seconds": round(time.time() - started, 1),
            "last_probe": last,
            "message": "upload did not settle before timeout (file too large / network / page change)",
        }

    async def _fill_publish_form(
        self, page, title: str, desc: str, tags: list[str]
    ) -> dict[str, Any]:
        """Fill title / description / topics with REAL keyboard events.

        React-style editors only sync on real keystrokes: DOM fill()/textContent injection
        leaves the visible text in place while the app state stays empty (measured
        2026-11-01 on the comment box; same editor stack on the publish form). Topics are
        typed as '#tag' + Space, which makes the site emit a real topic chip
        (<div data-mention="#">#tag</div>), then we read the field back and compare.
        """
        res: dict[str, Any] = {
            "title_filled": False,
            "title_selector": "",
            "desc_filled": False,
            "desc_selector": "",
            "tags_filled": [],
            "tags_missing": [],
            "readback": {},
            "input_method": "keyboard",
        }
        title = (title or "").strip()
        desc = (desc or "").strip()

        ti = page.locator(_TITLE_INPUT_SELECTOR).first
        try:
            await ti.wait_for(state="visible", timeout=15000)
            await ti.scroll_into_view_if_needed()
            await ti.click(timeout=8000)
            await asyncio.sleep(0.3)
            if title:
                # never inject via fill()/textContent: React-style editors only sync on
                # real keystrokes (measured 2026-11-01) — use page.keyboard.type only.
                await page.keyboard.type(title, delay=40)
                await asyncio.sleep(0.6)
            rb_title = await ti.input_value()
            res["title_selector"] = _TITLE_INPUT_SELECTOR
            res["readback"]["title"] = rb_title
            res["title_filled"] = bool(rb_title) if title else True
        except Exception as exc:
            res["title_error"] = str(exc)

        ed = page.locator(_DESC_EDITOR_SELECTOR).first
        try:
            await ed.wait_for(state="visible", timeout=15000)
            await ed.scroll_into_view_if_needed()
            await ed.click(timeout=8000)
            await asyncio.sleep(0.4)
            if desc:
                await page.keyboard.type(desc, delay=35)
                await asyncio.sleep(0.4)
            for idx, tag in enumerate(tags):
                lead = "" if (not desc and idx == 0) else " "
                await page.keyboard.type(f"{lead}#{tag}", delay=70)
                await asyncio.sleep(1.2)
                await page.keyboard.press("Space")
                await asyncio.sleep(0.8)
            rb_desc = await ed.evaluate("(el) => (el.innerText || '')")
            res["desc_selector"] = _DESC_EDITOR_SELECTOR
            res["readback"]["desc"] = rb_desc
            res["desc_filled"] = (desc in rb_desc) if desc else True
            res["tags_filled"] = [t for t in tags if f"#{t}" in rb_desc]
            res["tags_missing"] = [t for t in tags if f"#{t}" not in rb_desc]
        except Exception as exc:
            res["desc_error"] = str(exc)

        return res

    async def _verify_draft_receipt(
        self, page, title: str, desc: str, tags: list[str]
    ) -> dict[str, Any]:
        """Receipt check for mode=draft.

        Measured behaviour (2026-11-01): 暂存离开 lands back on /content/upload and the page
        shows 「你还有上次未发布的视频，是否继续编辑？继续编辑 放弃」. Clicking 继续编辑
        reopens /content/post/video?enter_from=draft with the saved title/description/topics
        restored — that read-back is the receipt. This creator build has NO 草稿箱 tab in
        内容管理, so 'the draft exists' is proven through the pending-draft prompt + resume,
        never assumed.
        """
        res: dict[str, Any] = {
            "receipt": "未确认送达",
            "pending_draft_prompt": False,
            "resume_clicked": False,
            "readback": {},
            "checked_at_url": "",
        }
        for _ in range(10):
            probe = await self._creator_page_probe(page)
            if probe.get("pending_draft_prompt"):
                res["pending_draft_prompt"] = True
                res["checked_at_url"] = probe.get("url", "")
                break
            await asyncio.sleep(1.2)
        if not res["pending_draft_prompt"]:
            res["message"] = "暂存离开 之后没有出现「上次未发布的视频」提示，草稿是否留存未能确认"
            return res

        try:
            res["resume_clicked"] = bool(
                await page.evaluate(
                    """() => {
          const els = Array.from(document.querySelectorAll('div,span,button'))
              .filter(e => e.children.length === 0 && (e.innerText || '').trim() === '继续编辑');
          if (!els.length) return false;
          els[els.length - 1].click();
          return true;
        }"""
                )
            )
        except Exception as exc:
            res["resume_error"] = str(exc)
        if not res["resume_clicked"]:
            res["message"] = "找到了草稿提示但未点到「继续编辑」，草稿内容未回读"
            return res

        for _ in range(10):
            await asyncio.sleep(1.5)
            if await page.locator(_TITLE_INPUT_SELECTOR).count():
                break
        rb: dict[str, Any] = {}
        try:
            ti = page.locator(_TITLE_INPUT_SELECTOR)
            if await ti.count():
                rb["title"] = await ti.first.input_value()
            ed = page.locator(_DESC_EDITOR_SELECTOR)
            if await ed.count():
                rb["desc"] = await ed.first.evaluate("(el) => (el.innerText || '')")
        except Exception as exc:
            rb["error"] = str(exc)
        res["readback"] = rb
        res["resumed_url"] = getattr(page, "url", "")
        rb_title = rb.get("title") or ""
        rb_desc = rb.get("desc") or ""
        title_ok = bool(title) and title in rb_title
        desc_ok = bool(desc) and desc in rb_desc
        tags_ok = bool(tags) and all(f"#{t}" in rb_desc for t in tags)
        res["title_match"] = title_ok
        res["desc_match"] = desc_ok
        res["tags_match"] = tags_ok if tags else None
        typed_anything = bool(title or desc or tags)
        if not typed_anything:
            res["receipt"] = "confirmed"
            res["message"] = "草稿已留存（上传页出现「上次未发布的视频」提示）；本次未填写标题/简介，无内容可回读比对"
        elif title_ok or desc_ok or tags_ok:
            res["receipt"] = "confirmed"
            res["message"] = "草稿已留存并回读一致（继续编辑后可读到填写的标题/简介/话题）"
        else:
            res["message"] = "草稿提示出现了，但继续编辑后回读不到填写的内容，按未确认处理"
        return res

    async def _verify_publish_receipt(self, page, title: str) -> dict[str, Any]:
        """Receipt check for mode=publish: success toast, else the content list."""
        res: dict[str, Any] = {"receipt": "未确认送达", "signal": "none"}
        for _ in range(10):
            await asyncio.sleep(2.0)
            try:
                body = await page.evaluate("() => (document.body ? document.body.innerText : '')")
            except Exception:
                body = ""
            if any(w in body for w in ("发布成功", "作品已发布", "发布完成", "审核中")):
                res.update({"receipt": "confirmed", "signal": "publish_toast",
                            "body_snippet": body[:200]})
                return res
        try:
            await self._goto(page, _DOUYIN_CREATOR_MANAGE, wait_ms=5000)
            body = await page.evaluate("() => (document.body ? document.body.innerText : '')")
            if title and title in body:
                res.update({"receipt": "confirmed", "signal": "content_manage_list",
                            "body_snippet": body[:200]})
                return res
            res["body_snippet"] = body[:200]
        except Exception as exc:
            res["error"] = str(exc)
        res["message"] = "发布后未拿到可核对回执（未出现在内容管理列表/无成功提示），如实报「未确认送达」"
        return res

    async def _creator_login_wall(self, page) -> dict[str, Any]:
        """Detect a clear login wall on the creator centre page (URL redirect or login text)."""
        try:
            url = page.url or ""
        except Exception:
            url = ""
        info: dict[str, Any] = {"blocked": False, "url": url}
        if "content/manage" not in url:
            info["redirected_away"] = True
            info["blocked"] = True
            return info
        try:
            ui = await page.evaluate(
                """() => {
                    const body = document.body ? document.body.innerText : '';
                    const text = body || '';
                    return {
                        loginText: /扫码登录|请登录|登录后即可|账号登录/.test(text),
                        bodyHead: text.slice(0, 120),
                    };
                }"""
            )
        except Exception:
            ui = {}
        info.update(ui or {})
        if info.get("loginText"):
            info["blocked"] = True
        return info

    async def _act_my_works(self, page, max_items: int = 20) -> dict[str, Any]:
        """Read this account's own works from the creator centre content list (read-only)."""
        result: dict[str, Any] = {
            "action": "my_works",
            "source": "creator_center_content_manage",
            "manage_url": _DOUYIN_CREATOR_MANAGE,
            "items": [],
            "total": None,
            "max_items": max_items,
        }
        await self._goto(page, _DOUYIN_CREATOR_MANAGE, wait_ms=6000)
        login = await self._detect_login(page)
        result["login"] = login
        # 创作者中心域下 _detect_login 的头像探测不适用（avatar_present 常为 false），
        # 且 account/info 的 1011 属模糊信号；所以这里以「内容管理页能否真的渲染出作品列表」为准，
        # 只有明确的登录墙（URL 被重定向到登录页 / 出现登录按钮或二维码）才判 login_required。
        wall = await self._creator_login_wall(page)
        result["login_wall"] = wall
        if wall.get("blocked"):
            result.update(
                {
                    "status": "login_required",
                    "message": (
                        "创作者中心内容管理页未登录，读不到本账号作品；"
                        "请在打开的 Edge 窗口扫码登录后重试（未做任何写操作）"
                    ),
                    "screenshot": await self._shot(page, "my_works_login_required"),
                }
            )
            return result

        cards: list[dict[str, Any]] = []
        total: int | None = None
        for _ in range(12):
            snapshot = await page.evaluate(
                """() => {
                    const body = document.body ? document.body.innerText : '';
                    const totalMatch = body.match(/作品\\s*\\(\\s*(\\d+)\\s*\\)/);
                    const all = [...document.querySelectorAll('[class*="video-card"]')];
                    const top = all.filter(e => !all.some(o => o !== e && o.contains(e)));
                    const rows = top.map(card => {
                        const t = card.innerText || '';
                        const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
                        const stat = {};
                        const labels = ['播放','点赞','评论','分享','收藏','完播率','2秒跳出率','吸粉量'];
                        for (let i = 0; i < lines.length - 1; i++) {
                            if (labels.includes(lines[i])) stat[lines[i]] = lines[i+1];
                        }
                        const timeMatch = t.match(/\\d{4}年\\d{1,2}月\\d{1,2}日\\s*\\d{1,2}:\\d{2}/);
                        const statusMatch = t.match(/已发布|审核中|未通过|仅自己可见|草稿|已删除/);
                        const titleEl = card.querySelector('[class*="info-title-text"]');
                        const badgeEl = card.querySelector('[class*="badge"]');
                        return {
                            title: titleEl ? (titleEl.innerText || '').trim() : (lines[1] || ''),
                            duration: badgeEl ? (badgeEl.innerText || '').trim() : '',
                            publish_time: timeMatch ? timeMatch[0] : '',
                            status: statusMatch ? statusMatch[0] : '',
                            stats: stat,
                            text_head: t.slice(0, 120),
                        };
                    });
                    return {total: totalMatch ? Number(totalMatch[1]) : null, rows: rows};
                }"""
            )
            if isinstance(snapshot, dict):
                total = snapshot.get("total")
                cards = snapshot.get("rows") or []
            if cards:
                break
            await asyncio.sleep(1.5)

        result["total"] = total
        result["items"] = cards[: max(1, max_items)]
        result["item_count"] = len(result["items"])
        result["screenshot"] = await self._shot(page, "my_works")
        if cards:
            result["status"] = "ok"
        else:
            result.update(
                {
                    "status": "ok",
                    "message": (
                        "内容管理页已打开但没解析到作品卡片：要么确实没有作品（total 为 0），"
                        "要么创作者中心列表结构变了；按 total 与截图核对，不臆造条目"
                    ),
                }
            )
        return result

    # -- interaction notifications (互动消息) --------------------------------

    async def _open_notify_panel(self, page) -> dict[str, Any]:
        """Open the 通知 dropdown in the www header and report what actually rendered.

        Anchors are stable ids / visible text only: `#douyin-header-menuCt` (the header icon
        row) + the visible label 通知, then the dropdown panel's own 互动消息 header text.
        The header icon only exists on `/user/self` in the current build (the feed pages render
        a slim nav-only header), so the caller navigates there first.
        """
        probe: dict[str, Any] = {"clicked": False, "click_strategy": "", "panel_found": False}
        state = await page.evaluate(
            "() => { const m = document.querySelector('#douyin-header-menuCt'); "
            "const has = (document.body ? document.body.innerText : '').includes('互动消息'); "
            "return {menu: !!m, panel: has, url: location.href, "
            "menu_text: m ? (m.innerText || '').slice(0, 200) : ''}; }"
        )
        probe.update(state or {})
        if not probe.get("menu"):
            probe["reason"] = "header_message_menu_missing"
            return probe

        async def poll(rounds: int = 10) -> bool:
            for _ in range(rounds):
                await asyncio.sleep(0.6)
                ok = bool(
                    await page.evaluate(
                        "() => (document.body ? document.body.innerText : '').includes('互动消息')"
                    )
                )
                if ok:
                    return True
            return False

        if probe.get("panel"):
            probe["panel_found"] = True
            probe["click_strategy"] = "already_open"
            return probe

        # The 「下载客户端，实时接收消息通知」toast can sit over the icon and eat the pointer
        # event, so the first attempt is a synthetic DOM click on the icon itself (no hit test),
        # then a real locator/mouse click as fallback. Measured 2026-11-02: both paths have been
        # observed to open the dropdown; the synthetic one is the reliable one when the toast shows.
        for strategy in ("dispatch", "locator", "mouse"):
            probe["click_strategy"] = strategy
            try:
                if strategy == "dispatch":
                    res = await page.evaluate(_JS_CLICK_NOTIFY_ICON)
                    probe["click_dispatch"] = res
                    if not (res or {}).get("ok"):
                        continue
                    probe["clicked"] = True
                elif strategy == "locator":
                    await page.locator("#douyin-header-menuCt >> text=通知").first.click(timeout=6000)
                    probe["clicked"] = True
                else:
                    box = await page.evaluate(
                        """() => { const root = document.querySelector('#douyin-header-menuCt');
                           if (!root) return null;
                           for (const el of Array.from(root.querySelectorAll('div,p,span,li'))) {
                             if ((el.innerText || '').trim() !== '通知') continue;
                             const r = el.getBoundingClientRect();
                             if (r.width > 4 && r.height > 4) {
                               return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
                             }
                           }
                           return null; }"""
                    )
                    if not box:
                        continue
                    await page.mouse.click(box[0], box[1])
                    probe["clicked"] = True
            except Exception as exc:
                probe.setdefault("click_errors", []).append(f"{strategy}: {str(exc)[:160]}")
                continue
            if await poll():
                probe["panel_found"] = True
                break
        return probe

    async def _click_notify_tab(self, page, label: str) -> dict[str, Any]:
        """Switch the notification dropdown to one tab (评论 / @我的 / 赞 / 粉丝 / 推荐).

        Prefers a real pointer click (locator, then mouse at the label centre) because the
        dropdown may ignore synthetic (isTrusted=false) events; dispatch is only the last resort.
        """
        errors: list[str] = []
        try:
            await page.locator(f"#douyin-header-menuCt >> text={label}").first.click(timeout=6000)
            return {"ok": True, "label": label, "method": "locator"}
        except Exception as exc:
            errors.append(f"locator: {str(exc)[:150]}")
        box = await self._notify_label_center(page, label)
        if box:
            try:
                await page.mouse.click(box[0], box[1])
                return {"ok": True, "label": label, "method": "mouse"}
            except Exception as exc:
                errors.append(f"mouse: {str(exc)[:150]}")
        try:
            res = await page.evaluate(_JS_NOTIFY_TAB, label)
            if isinstance(res, dict) and res.get("ok"):
                await asyncio.sleep(2.0)
                return {"ok": True, "label": label, "method": "dispatch", "res": res}
            errors.append(f"dispatch: {res}")
        except Exception as exc:
            errors.append(f"dispatch: {str(exc)[:150]}")
        await asyncio.sleep(2.0)
        return {"ok": False, "label": label, "errors": errors}

    async def _notify_label_center(self, page, label: str) -> list[int] | None:
        try:
            return await page.evaluate(
                f"""() => {{ const menu = document.querySelector('#douyin-header-menuCt') || document.body;
                  const clean = (s) => (s || '').replace(/[\\u200B-\\u200F\\uFEFF]/g, '').trim();
                  for (const el of Array.from(menu.querySelectorAll('div,span,a,li'))) {{
                    if (el.children.length > 2) continue;
                    if (clean(el.innerText) !== '{label}') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width >= 8 && r.height >= 8) return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
                  }}
                  return null; }}"""
            )
        except Exception:
            return None

    async def _expand_notify_panel(self, page) -> dict[str, Any]:
        """Reveal the tabbed inbox (粉丝/@我的/评论/赞/推荐) by clicking 「全部消息」.

        The dropdown's compact first state only shows the like feed; comment replies and
        @ mentions live in the tabbed view that appears after 全部消息. Measured 2026-11-02.
        """
        errors: list[str] = []
        try:
            await page.locator("#douyin-header-menuCt >> text=全部消息").first.click(timeout=6000)
            method = "locator"
        except Exception as exc:
            errors.append(f"locator: {str(exc)[:150]}")
            box = await self._notify_label_center(page, "全部消息")
            if box:
                try:
                    await page.mouse.click(box[0], box[1])
                    method = "mouse"
                except Exception as exc2:
                    errors.append(f"mouse: {str(exc2)[:150]}")
                    res = await page.evaluate(_JS_NOTIFY_EXPAND)
                    method = f"dispatch:{res}"
            else:
                res = await page.evaluate(_JS_NOTIFY_EXPAND)
                method = f"dispatch:{res}"
        await asyncio.sleep(2.5)
        tabs = await self._notify_tabs(page)
        return {"ok": bool(tabs), "method": method, "tabs": tabs, "errors": errors[:4]}

    async def _notify_tabs(self, page) -> list[str]:
        try:
            tabs = await page.evaluate(_JS_NOTIFY_TAB_LABELS)
        except Exception:
            return []
        return [str(t) for t in (tabs or [])]

    @staticmethod
    def _notify_row_to_item(row: dict[str, Any], hours: float, now: datetime) -> dict[str, Any]:
        """Turn one measured notification row (text lines + hrefs) into a structured item."""
        lines = [str(x) for x in (row.get("lines") or []) if str(x).strip()]
        action = ""
        action_idx = -1
        for i, ln in enumerate(lines):
            m = _NOTIFY_ACTION_FULL_RE.search(ln)
            if m:
                action = m.group("action")
                action_idx = i
                break
        time_text = ""
        time_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            if _NOTIFY_TIME_RE.match(lines[i].strip()):
                time_text = lines[i].strip()
                time_idx = i
                break
        consumed: set[int] = {i for i in (action_idx, time_idx) if i >= 0}
        author = ""
        group = ""
        if action_idx >= 0:
            inline = lines[action_idx].split(action)[0].strip()
            if inline.startswith("等"):
                # douyin groups likes as「昵称 / 等N人赞了你的作品」on two lines
                group = inline
                if action_idx > 0:
                    prev = lines[action_idx - 1].strip()
                    if prev and not _NOTIFY_TIME_RE.match(prev):
                        author = f"{prev} {inline}".strip()
                        consumed.add(action_idx - 1)
                if not author:
                    author = inline
            elif inline:
                author = inline
            elif action_idx > 0 and action_idx - 1 not in consumed:
                author = lines[action_idx - 1].strip()
                consumed.add(action_idx - 1)
        if not author:
            for i, ln in enumerate(lines):
                if i in consumed:
                    continue
                author = ln.strip()
                consumed.add(i)
                break
        content_parts = [
            t for i, t in ((i, ln.strip()) for i, ln in enumerate(lines))
            if t and i not in consumed and not _NOTIFY_ACTION_FULL_RE.search(t)
            and not _NOTIFY_TIME_RE.match(t)
        ]
        age_hours, precision = _notify_age_hours(time_text, now)
        aweme_ids = []
        for href in row.get("hrefs") or []:
            m = re.search(r"/video/(\d{8,})", str(href))
            if m and m.group(1) not in aweme_ids:
                aweme_ids.append(m.group(1))
        profile_ids = []
        for href in row.get("hrefs") or []:
            m = re.search(r"/user/([A-Za-z0-9_\-]+)", str(href))
            if m and m.group(1) not in profile_ids:
                profile_ids.append(m.group(1))
        within = age_hours is not None and age_hours <= hours
        content_text = " ".join(content_parts)[:300]
        content_kind = "comment_body" if content_text else ""
        if not content_text and action_idx >= 0:
            # 通知行本身通常不携带正文（只有「赞了你的评论 / 回复了你的评论」这类动作短语）；
            # 此时 content 如实回填该动作行原文，并用 content_kind 标明这不是对方正文，
            # 真正文要用 locate_item 落到视频后从评论面板读。绝不臆造。
            content_text = lines[action_idx].strip()[:300]
            content_kind = "action_phrase"
        return {
            "index": int(row.get("index") or 0),
            "author": author[:80],
            "author_group": group,
            "action": action,
            "is_reply_to_me": bool(action in ("回复了你的评论", "回复了你")),
            "is_mention": bool(action in ("@了你", "提到了你")),
            "is_about_my_comment": bool(action in ("赞了你的评论", "回复了你的评论", "回复了你")),
            "content": content_text,
            "content_kind": content_kind,
            "time_text": time_text,
            "age_hours_estimate": None if age_hours is None else round(age_hours, 2),
            "time_precision": precision,
            "within_hours": within,
            "source_aweme_id": aweme_ids[0] if aweme_ids else "",
            "source_aweme_ids": aweme_ids,
            "author_user_ids": profile_ids,
            "row_lines": lines[:10],
            "rect": row.get("rect") or {},
        }

    async def _open_video_by_id(self, page, aweme_id: str) -> dict[str, Any]:
        """Navigate straight to /video/<aweme_id> and prove which video is mounted.

        This is the aweme_id-based twin of `_open_player`: `_open_player` resolves a *slot*
        through feed_snapshot.json (which is only meaningful for feed/watch_report reads),
        while a notification already carries the concrete video, so no snapshot is involved.
        Identity verification is identical: URL alone is never accepted as proof.
        """
        target = re.sub(r"\D", "", str(aweme_id or ""))
        result: dict[str, Any] = {
            "target_aweme_id": target,
            "actual_aweme_id": "",
            "aweme_match": False,
            "identity_source": "aweme_id",
            "snapshot_state": "not_applicable",
        }
        if len(target) < 8:
            result["status"] = "aweme_unavailable"
            result["message"] = "aweme_id 无效（需要 8 位以上数字）"
            return result
        verify = bool(getattr(self, "_verify_aweme_id", True))
        actual = ""
        url_only = False
        for attempt in range(1, (2 if verify else 1) + 1):
            await self._goto(page, _DOUYIN_VIDEO.format(vid=target), wait_ms=1800)
            deadline = time.monotonic() + 12.0
            while True:
                ident = await self._current_aweme(page)
                actual = str(ident.get("actual") or "")
                url_only = bool(ident.get("url_only"))
                if actual == target and not url_only:
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.6)
            result["nav_attempt"] = attempt
            if (actual == target and not url_only) or not verify:
                break
        info = await self._current_video_info(page)
        identity = info.get("aweme_identity") or {}
        dom_actual = str(identity.get("panel") or identity.get("player_class") or "")
        if dom_actual:
            actual = dom_actual
        result["actual_aweme_id"] = actual
        result["url_only"] = url_only
        result["aweme_match"] = bool(actual and actual == target)
        info.update(result)
        if actual:
            info["link"] = f"https://www.douyin.com/video/{actual}"
        if not result["aweme_match"] and verify:
            info["status"] = "mismatch"
            info["message"] = (
                f"要定位的是 aweme_id={target}（来自互动消息通知），但页面实际挂载的是 "
                f"aweme_id={actual or '未识别'}；已按 aweme_id 重新跳转一次仍未对齐，"
                "因此不返回另一条视频的数据，也不做后续动作"
            )
            return info
        info["verification"] = "verified" if result["aweme_match"] else ("disabled" if not verify else "mismatch")
        if not info.get("status"):
            info["status"] = "ok"
        return info

    async def _act_interactions(
        self,
        page,
        hours: float,
        max_items: int,
        tab: str,
        locate_index: int | None,
        comment_text_match: str | None,
    ) -> dict[str, Any]:
        """Read the 互动消息 (comment replies / @ mentions / likes) and optionally locate one.

        Read-only: opens the header 通知 dropdown, switches to the requested tab, and parses the
        rendered rows. It never types or clicks a send control. `locate_index` only follows a row
        into its video page to prove the aweme_id (and, with `comment_text_match`, which comment),
        i.e. exactly the information `reply_to_comment(aweme_id=…)` needs next.
        """
        hours = max(1.0, float(hours or 24))
        max_items = max(1, min(50, int(max_items or 20)))
        tab_key = (tab or "comment").strip().lower()
        tab_label = _NOTIFY_TABS.get(tab_key, None)
        now = datetime.now(_BEIJING_TZ)
        result: dict[str, Any] = {
            "action": "interactions",
            "source": "www.douyin.com header 通知 → 互动消息 面板",
            "hours": hours,
            "max_items": max_items,
            "tab": tab_key,
            "tab_label": tab_label or "(面板默认)",
            "items": [],
            "item_count": 0,
            "notes": [],
            "status": "ok",
        }
        await self._goto(page, _DOUYIN_USER_SELF, wait_ms=5000)
        await self._dismiss_overlays(page)
        second_verify = await self._check_second_verify(page)
        result["second_verify"] = second_verify
        if second_verify.get("blocked"):
            result.update(
                {
                    "status": "blocked_second_verify",
                    "message": (
                        "页面出现短信/账号安全二次验证遮罩（#uc-second-verify 或相关文案）："
                        "按要求立即中止，不绕过、不重试，也没有读取任何通知内容"
                    ),
                    "screenshot": await self._shot(page, "interactions_second_verify"),
                }
            )
            return result
        body = await self._page_text(page)
        login_wall = _is_login_wall(body)
        result["login_wall_text"] = login_wall
        panel = await self._open_notify_panel(page)
        result["panel_probe"] = panel
        login = await self._detect_login(page)
        # keep only the verdict in the payload: cookie names / api raw payload are not needed here
        result["login"] = {
            "logged_in": login.get("logged_in"),
            "basis": login.get("basis"),
            "cookie_valid": login.get("cookie_valid"),
            "login_modal": login.get("login_modal"),
            "nav_login": login.get("nav_login"),
        }
        if not panel.get("panel_found"):
            result["status"] = "login_required" if login_wall else "panel_not_open"
            result["message"] = (
                "互动消息面板没有渲染出来：页面出现登录墙文案，需要先在打开的 Edge 窗口里登录"
                if login_wall
                else (
                    "互动消息面板没有渲染出来（header 通知 入口未出现或点击后面板未挂载）。"
                    "本轮不做猜测：没有条目就不返回条目，请按截图核对站点结构是否变化"
                )
            )
            result["screenshot"] = await self._shot(page, "interactions_panel_missing")
            return result

        if tab_label:
            expand = await self._expand_notify_panel(page)
            result["expand"] = expand
            result["tabs_seen"] = await self._notify_tabs(page)
            tab_res = await self._click_notify_tab(page, tab_label)
            result["tab_switch"] = tab_res
            if not tab_res.get("ok"):
                result["notes"].append(
                    f"未找到「{tab_label}」tab（站点结构可能变化）；保留面板当前列表并如实标注"
                )
            result["tabs_after_tab_click"] = await self._notify_tabs(page)

        data = await page.evaluate(_JS_NOTIFY_PANEL)
        result["panel"] = {
            "found": bool(data.get("found")),
            "url": data.get("url", ""),
            "tabs": [t.get("text") for t in (data.get("tabs") or [])],
            "row_count": int(data.get("row_count") or 0),
            "list_present": bool(data.get("list_present")),
            "list_class_prefix": data.get("list_class_prefix", ""),
            "text_head": str(data.get("panel_text") or "")[:400],
        }
        rows = list(data.get("rows") or [])
        all_items = [self._notify_row_to_item(r, hours, now) for r in rows]
        # The dropdown's tab row is a site-side view switch; it collapses again as soon as a tab is
        # picked in the current build, so the tab choice is additionally enforced here by action
        # type. That keeps `tab=` deterministic instead of "whatever the panel happened to show".
        tab_actions = {
            "comment": ("回复了你的评论", "回复了你", "赞了你的评论", "评论了你的作品", "评论了你"),
            "mention": ("@了你", "提到了你"),
            "like": ("赞了你的作品", "赞了你的评论", "赞了你的视频"),
            "fan": ("关注了你",),
        }
        wanted = tab_actions.get(tab_key)
        items = [it for it in all_items if it.get("action") in wanted] if wanted else list(all_items)
        result["tab_action_filter"] = list(wanted) if wanted else "(全部动作)"
        kept = [it for it in items if it.get("within_hours")]
        dropped = [it for it in items if not it.get("within_hours")]
        result["filter"] = {
            "hours": hours,
            "rows_seen": len(all_items),
            "rows_after_tab_filter": len(items),
            "kept": len(kept),
            "dropped_by_time": len(dropped),
            "dropped_time_labels": [it.get("time_text") for it in dropped][:10],
            "coarse_time_rows": len([it for it in items if it.get("time_precision") == "coarse"]),
        }
        result["items"] = kept[:max_items]
        result["item_count"] = len(result["items"])
        result["reply_items"] = [it for it in result["items"] if it.get("is_reply_to_me")]
        result["mention_items"] = [it for it in result["items"] if it.get("is_mention")]
        result["panel_row_sample"] = data.get("row_html_sample", "")
        if not all_items:
            result["notes"].append(
                "面板已打开但没解析到通知行：可能当前 tab 下确实没有通知，或行结构变了；"
                "不臆造条目，按截图与 panel.row_count 核对"
            )
        elif not items:
            result["notes"].append(
                f"面板有 {len(all_items)} 行通知，但没有符合 tab={tab_key} 的动作类型"
                f"（{result['tab_action_filter']}）"
            )
        elif not kept:
            result["notes"].append(f"面板有 {len(items)} 行通知，但没有一行落在 {hours:g} 小时内")

        # optional: follow one row into its video to prove aweme_id (+ comment) for the reply step
        if locate_index:
            locate: dict[str, Any] = {"index": int(locate_index)}
            # locate_index addresses the *item* (match by raw row index among kept items), because
            # items is the tab/hours-filtered view the caller is looking at
            target_item = next(
                (it for it in kept if int(it.get("index") or 0) == int(locate_index)), None
            )
            if target_item is None:
                locate.update(
                    {
                        "ok": False,
                        "reason": "item_out_of_range_or_filtered",
                        "item_count": len(kept),
                        "available_indexes": [int(it.get("index") or 0) for it in kept][:20],
                    }
                )
                result["locate"] = locate
            else:
                raw_idx = int(target_item.get("index") or 1) - 1
                pre_pages = list(page.context.pages)
                point = await page.evaluate(_JS_NOTIFY_ROW_POINT, {"index": raw_idx, "preferVideo": True})
                locate["row_point"] = point
                opened_new = False
                if point.get("ok"):
                    try:
                        await page.mouse.click(int(point["x"]), int(point["y"]))
                    except Exception as exc:
                        locate["click_error"] = str(exc)[:200]
                    await asyncio.sleep(3.5)
                    target_page = page
                    for pg in page.context.pages:
                        if pg not in pre_pages:
                            target_page = pg
                            opened_new = True
                            break
                    locate["opened_new_page"] = opened_new
                    url_probe = await target_page.evaluate(_JS_CURRENT_VIDEO_ID)
                    locate["url_probe"] = url_probe
                    known_id = (
                        target_item.get("source_aweme_id")
                        or url_probe.get("modal_id")
                        or url_probe.get("path_id")
                        or ""
                    )
                    if known_id:
                        locate["resolved_aweme_id"] = known_id
                    info = await self._open_video_by_id(target_page, known_id) if known_id else {
                        "status": "no_known_aweme_id"
                    }
                    if info.get("status") == "no_known_aweme_id":
                        ident = await self._current_aweme(target_page)
                        info = {
                            "status": "ok" if ident.get("actual") else "aweme_unavailable",
                            "actual_aweme_id": str(ident.get("actual") or ""),
                            "url_only": bool(ident.get("url_only")),
                            "page_url": target_page.url,
                        }
                    locate["video"] = {
                        "status": info.get("status"),
                        "target_aweme_id": info.get("target_aweme_id", ""),
                        "actual_aweme_id": info.get("actual_aweme_id", ""),
                        "aweme_match": info.get("aweme_match"),
                        "link": info.get("link", ""),
                        "page_url": info.get("page_url") or getattr(target_page, "url", ""),
                        "message": info.get("message", ""),
                    }
                    located_id = str(info.get("actual_aweme_id") or "")
                    if located_id and not target_item.get("source_aweme_id"):
                        target_item["source_aweme_id"] = located_id
                        if located_id not in (target_item.get("source_aweme_ids") or []):
                            target_item.setdefault("source_aweme_ids", []).append(located_id)
                        locate["resolved_aweme_id"] = located_id
                    if located_id:
                        comments = await self._read_comments(target_page, target=30)
                        needle = (comment_text_match or "").strip()
                        match = None
                        for c in comments.get("comments", []):
                            hay = f"{c.get('author', '')} {c.get('text', '')}"
                            if needle and needle in hay:
                                match = c
                                break
                        locate["comments_read"] = int(comments.get("comment_count") or 0)
                        locate["comment_match"] = match
                        if match:
                            target_item["content"] = str(match.get("text", ""))[:300]
                            target_item["content_kind"] = "comment_body"
                            target_item["comment_id"] = str(match.get("comment_id") or "")
                            target_item["matched_comment_author"] = str(match.get("author") or "")
                        if needle and not match:
                            locate["note"] = (
                                f"该视频评论区前 {locate['comments_read']} 条里没有匹配「{needle}」的评论；"
                                "站点按热度/时间排序，可能需要滚动更多或换一个更精确的子串"
                            )
                        locate["reply_hint"] = {
                            "action": "reply_to_comment",
                            "aweme_id": located_id,
                            "comment_text_match": needle or "(必填：目标评论的一段正文或作者名)",
                            "comment_id": (match or {}).get("comment_id", ""),
                            "allow_send": "true（仍需 send_policy.json allow_reply=true）",
                        }
                    locate["ok"] = bool(located_id)
                else:
                    locate["ok"] = False
                    locate["reason"] = point.get("reason", "row_point_unavailable")
                if opened_new:
                    try:
                        await target_page.close()
                    except Exception:
                        pass
                result["locate"] = locate

        result["screenshot"] = await self._shot(page, "interactions")
        if result["items"]:
            result["status"] = "ok"
        elif result["status"] == "ok":
            result["status"] = "empty"
        return result

    async def _act_publish(
        self,
        page,
        video_path: str | None,
        title: str | None,
        desc: str | None,
        tags: Any,
        mode: str | None,
        allow_send: bool,
        allow_publish_flag: bool = False,
    ) -> dict[str, Any]:
        mode = str(mode or "draft").strip().lower()
        if mode not in ("draft", "publish"):
            return {"status": "error", "message": "mode must be 'draft' (default) or 'publish'"}
        tag_list = self._normalize_tags(tags)
        title_s = (title or "").strip()
        desc_s = (desc or "").strip()

        raw_path = (video_path or "").strip().strip('"')
        if not raw_path:
            return {"status": "error", "message": "video_path (absolute local video path) is required"}
        path = Path(raw_path)
        if not path.is_absolute():
            path = (self.workspace / raw_path).resolve()
        if not path.is_file():
            return {"status": "error", "message": f"video file not found: {raw_path}", "video_path": str(path)}
        if path.suffix.lower() not in (".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"):
            return {"status": "error", "message": f"unsupported video suffix {path.suffix}", "video_path": str(path)}

        ok, reason = self._check_publish_gate(mode, allow_send, allow_publish_flag)
        if not ok:
            return {
                "status": "blocked",
                "action": "publish",
                "mode": mode,
                "reason": reason,
                "nothing_happened": True,
                "policy_path": str(self.state_dir / "send_policy.json"),
            }

        result: dict[str, Any] = {
            "action": "publish",
            "mode": mode,
            "video_path": str(path),
            "video_size_bytes": path.stat().st_size,
            "title": title_s,
            "desc": desc_s,
            "tags": tag_list,
            "publish_clicked": False,
            "draft_saved": False,
        }

        await self._goto(page, _DOUYIN_CREATOR_UPLOAD, wait_ms=4000)
        probe = await self._creator_page_probe(page)
        result["upload_page_reachable"] = bool(probe.get("upload_shell") or probe.get("file_inputs"))
        result["upload_page"] = probe
        if probe.get("on_login_page") or not probe.get("file_inputs"):
            login = await self._detect_login(page)
            result.update(
                {
                    "status": "login_required",
                    "login": login,
                    "message": (
                        "创作者中心上传页没有文件输入（未登录或被跳转到登录页）；"
                        "请在打开的 Edge 窗口扫码登录后重试，未发生任何上传/发布"
                    ),
                    "screenshot": await self._shot(page, "publish_login_required"),
                }
            )
            return result

        verify = await self._check_second_verify(page)
        if verify.get("blocked"):
            result.update(
                {
                    "status": "blocked_second_verify",
                    "second_verify": verify,
                    "message": (
                        "检测到抖音短信/账号安全验证遮罩（#uc-second-verify）；按策略立即中止，"
                        "不绕过、不自动重试。请本人在窗口内完成验证后重新调用。"
                    ),
                    "nothing_uploaded": True,
                    "screenshot": await self._shot(page, "publish_second_verify"),
                }
            )
            return result

        # 1) real file-input selection
        try:
            file_input = page.locator('input[type="file"]').first
            await file_input.set_input_files(str(path))
            result["file_selected"] = True
        except Exception as exc:
            result.update(
                {
                    "status": "error",
                    "file_selected": False,
                    "message": f"选中本地视频失败：{exc}",
                    "screenshot": await self._shot(page, "publish_file_select_failed"),
                }
            )
            return result

        # 2) wait for the upload / pre-analysis to settle before touching the form
        ready = await self._wait_upload_ready(page)
        result["upload_wait"] = ready
        if not ready.get("ready"):
            result.update(
                {
                    "status": "upload_not_settled",
                    "message": "上传/预审未在超时内完成，未填写表单、未存草稿、未发布",
                    "screenshot": await self._shot(page, "publish_upload_timeout"),
                }
            )
            return result
        verify_after_upload = await self._check_second_verify(page)
        result["second_verify_after_upload"] = verify_after_upload
        if verify_after_upload.get("blocked"):
            result.update(
                {
                    "status": "blocked_second_verify",
                    "message": "上传过程中弹出短信/安全验证遮罩，已中止；未存草稿、未发布",
                    "screenshot": await self._shot(page, "publish_second_verify_after_upload"),
                }
            )
            return result

        # 3) title / desc / topics — real keyboard events only
        fill = await self._fill_publish_form(page, title_s, desc_s, tag_list)
        result["form"] = fill
        if not (fill.get("title_filled") and fill.get("desc_filled")):
            result.update(
                {
                    "status": "form_fill_failed",
                    "message": "标题/简介未能通过真实键盘输入填入并回读一致；未存草稿、未发布",
                    "screenshot": await self._shot(page, "publish_form_fill_failed"),
                }
            )
            return result

        # 4) draft (default) or publish (explicit, double-gated)
        if mode == "draft":
            clicked = False
            for sel in ('button:text-is("暂存离开")', 'text=暂存离开', '[aria-label*="暂存"]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    if not await loc.is_visible():
                        continue
                    await loc.click(timeout=8000)
                    clicked = True
                    break
                except Exception:
                    continue
            result["draft_clicked"] = clicked
            await asyncio.sleep(3.0)
            result["url_after_draft"] = getattr(page, "url", "")
            shot = await self._shot(page, "publish_draft_saved")
            receipt = await self._verify_draft_receipt(page, title_s, desc_s, tag_list)
            today = self._record_send_usage("publish")
            usage = _read_json(self.state_dir / "usage.json", {})
            day = usage.get("days", {}).get(_day_key(), {})
            day["publish_last_mode"] = "draft"
            _atomic_write_json(self.state_dir / "usage.json", usage)
            result.update(
                {
                    "status": "draft_saved" if clicked else "draft_not_saved",
                    "draft_saved": bool(clicked),
                    "receipt": receipt.get("receipt", "未确认送达"),
                    "receipt_detail": receipt,
                    "published": False,
                    "publish_clicked": False,
                    "message": (
                        "已按草稿箱模式处理：上传 + 真实键盘填写标题/简介/话题 + 点「暂存离开」"
                        "（草稿），全程未点「发布」，没有产生公开作品"
                        if clicked
                        else "未定位到「暂存离开」按钮，草稿未保存"
                    ),
                    "screenshot": shot,
                    "usage_today": today.get("publish", 0),
                }
            )
            return result

        # mode=publish — reachable only when send_policy allow_publish=true
        clicked = False
        for sel in ('button:text-is("发布")', '[aria-label="发布"]', '[data-e2e="publish-button"]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                if not await loc.is_visible():
                    continue
                await loc.click(timeout=8000)
                clicked = True
                break
            except Exception:
                continue
        result["publish_clicked"] = clicked
        receipt = (
            await self._verify_publish_receipt(page, title_s)
            if clicked
            else {"receipt": "未确认送达", "signal": "publish_button_not_found"}
        )
        today = self._record_send_usage("publish")
        usage = _read_json(self.state_dir / "usage.json", {})
        day = usage.get("days", {}).get(_day_key(), {})
        day["publish_last_mode"] = "publish"
        _atomic_write_json(self.state_dir / "usage.json", usage)
        result.update(
            {
                "status": "publish_attempted" if clicked else "not_published",
                "published": bool(clicked and receipt.get("receipt") == "confirmed"),
                "receipt": receipt.get("receipt", "未确认送达"),
                "receipt_detail": receipt,
                "message": (
                    "已点击「发布」并做回执核对；核对不到即如实报「未确认送达」"
                    if clicked
                    else "未定位到「发布」按钮，未发布（页面结构可能变化）"
                ),
                "screenshot": await self._shot(page, "publish_result"),
                "usage_today": today.get("publish", 0),
            }
        )
        return result

    # -- delete own work (创作者中心 内容管理) --------------------------------

    async def _list_work_cards(self, page) -> list[dict[str, Any]]:
        """Top-level work cards on 内容管理 as index + text + hrefs (read-only)."""
        cards = await page.evaluate(
            """() => {
                const all = [...document.querySelectorAll('[class*="video-card"]')];
                const top = all.filter(e => !all.some(o => o !== e && o.contains(e)));
                return top.map((card, idx) => ({
                    idx: idx,
                    text: (card.innerText || '').slice(0, 300),
                    hrefs: Array.from(card.querySelectorAll('a')).map(a => a.getAttribute('href') || '').join(' '),
                    title: card.querySelector('[class*="info-title-text"]')
                        ? card.querySelector('[class*="info-title-text"]').innerText.trim() : '',
                }));
            }"""
        )
        return [c for c in (cards or []) if isinstance(c, dict)]

    async def _click_box(self, page, box: list[int]) -> bool:
        """Real pointer click at an element centre, with a dispatch fallback."""
        x, y = int(box[0]), int(box[1])
        try:
            await page.mouse.move(x, y)
            await asyncio.sleep(0.15)
            await page.mouse.click(x, y)
            return True
        except Exception:
            pass
        try:
            await page.evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return false;
                    const target = el.closest('button,li,a,div[role="menuitem"]') || el;
                    target.click();
                    return true;
                }""",
                [x, y],
            )
            return True
        except Exception:
            return False

    async def _act_delete_work(
        self,
        page,
        aweme_id: str | None,
        title: str | None,
        confirm: bool,
    ) -> dict[str, Any]:
        """Delete one of this account's own works from the creator centre list.

        Locates the card on 内容管理 by aweme_id (matched against any link href or the
        card text) or by a title substring. ``confirm=False`` is a dry run: it reports
        what it found and clicks nothing. With ``confirm=True`` it opens the card's
        更多 menu, clicks 删除, confirms in the modal, then re-reads the list to prove
        the work is gone. Delete reduces public content, so it is gated on an explicit
        ``confirm=true`` (mirroring publish's double gate) but intentionally does NOT
        depend on send_policy allow_publish.
        """
        target_id = (aweme_id or "").strip()
        target_title = (title or "").strip()
        if not target_id and not target_title:
            return {
                "status": "error",
                "action": "delete_work",
                "message": "delete_work 需要 aweme_id 或 title 二选一，用于在内容管理页定位作品",
            }
        if not confirm:
            return {
                "status": "blocked",
                "action": "delete_work",
                "confirm": False,
                "reason": (
                    "删除是不可逆的破坏性操作：请带 aweme_id/title 并显式 confirm=true 重试。"
                    "本次未点击任何按钮，未删除任何作品。"
                ),
                "target_aweme_id": target_id,
                "target_title": target_title,
            }

        result: dict[str, Any] = {
            "action": "delete_work",
            "manage_url": _DOUYIN_CREATOR_MANAGE,
            "target_aweme_id": target_id,
            "target_title": target_title,
            "confirm": True,
            "card_found": False,
            "matched_card_index": None,
            "delete_button_clicked": False,
            "confirm_clicked": False,
        }

        await self._goto(page, _DOUYIN_CREATOR_MANAGE, wait_ms=6000)
        wall = await self._creator_login_wall(page)
        result["login_wall"] = wall
        if wall.get("blocked"):
            result.update(
                {
                    "status": "login_required",
                    "message": "创作者中心内容管理页未登录，无法删除作品（未做任何删除操作）",
                    "screenshot": await self._shot(page, "delete_work_login_required"),
                }
            )
            return result

        # let the list render, then locate the target card
        cards: list[dict[str, Any]] = []
        for _ in range(10):
            cards = await self._list_work_cards(page)
            if cards:
                break
            await asyncio.sleep(1.5)
        result["cards_on_page"] = [
            {"index": c.get("idx"), "title": c.get("title"), "head": (c.get("text") or "")[:120]}
            for c in cards
        ]

        def _matches(card: dict[str, Any]) -> bool:
            text = card.get("text") or ""
            hrefs = card.get("hrefs") or ""
            ti = card.get("title") or ""
            if target_id and (target_id in hrefs or target_id in text or target_id in ti):
                return True
            if target_title and target_title in ti:
                return True
            return False

        matched = [c for c in cards if _matches(c)]
        if not matched:
            result.update(
                {
                    "status": "not_found",
                    "message": (
                        "内容管理页没有匹配到目标作品：按 aweme_id 未在卡片链接/文本命中，"
                        "按 title 也未在卡片标题命中。未做任何删除。"
                    ),
                    "screenshot": await self._shot(page, "delete_work_not_found"),
                }
            )
            return result

        card = matched[0]
        idx = int(card.get("idx"))
        result.update({"card_found": True, "matched_card_index": idx, "matched_card_title": card.get("title")})
        result["screenshot_before"] = await self._shot(page, "delete_work_before")

        # Real DOM (probed 2026-11-05): each card renders an op-btns row with four direct
        # buttons — 编辑作品 / 设置权限 / 作品置顶 / 删除作品. There is NO 更多 hover menu,
        # so click 删除作品 straight from the card.
        del_box = await page.evaluate(
            """(idx) => {
                const all = [...document.querySelectorAll('[class*="video-card"]')];
                const top = all.filter(e => !all.some(o => o !== e && o.contains(e)));
                const card = top[idx];
                if (!card) return null;
                let best = null;
                for (const el of card.querySelectorAll('div,span,button,a')) {
                    const tt = (el.innerText || '').trim();
                    if (!['删除作品', '删除'].includes(tt)) continue;
                    const b = el.getBoundingClientRect();
                    if (b.width <= 2 || b.height <= 2) continue;
                    const area = b.width * b.height;
                    if (!best || area < best.area) {
                        best = {area: area, box: [Math.round(b.x + b.width / 2), Math.round(b.y + b.height / 2)], text: tt};
                    }
                }
                return best;
            }""",
            idx,
        )
        if not del_box:
            result.update(
                {
                    "status": "delete_button_not_found",
                    "message": "卡片上没找到「删除作品」按钮（页面结构可能变化），未执行删除",
                    "screenshot": await self._shot(page, "delete_work_button_missing"),
                }
            )
            return result
        result["delete_button_text"] = del_box.get("text")
        result["delete_button_clicked"] = await self._click_box(page, del_box["box"])
        await asyncio.sleep(1.5)

        # confirm inside the modal — prefer 确认删除 / 删除 / 确定, scoped to a modal/dialog
        confirm_btn = await page.evaluate(
            """() => {
                const labels = ['确认删除', '删除', '确定', '确认'];
                const modals = Array.from(document.querySelectorAll(
                    '[class*="modal"],[class*="Modal"],[class*="dialog"],[class*="Dialog"],[role="dialog"]'));
                const scope = modals.length ? modals : [document];
                let best = null;
                for (const root of scope) {
                    for (const el of root.querySelectorAll('button,[role="button"],div,span,a')) {
                        const tt = (el.innerText || '').trim();
                        if (!labels.includes(tt)) continue;
                        const b = el.getBoundingClientRect();
                        if (b.width <= 2 || b.height <= 2) continue;
                        const cls = typeof el.className === 'string' ? el.className : '';
                        const isBtn = el.tagName === 'BUTTON' || /semi-button|btn/i.test(cls);
                        const score = (isBtn ? 0 : 5000) + b.width * b.height;
                        if (!best || score < best.score) {
                            best = {score: score, box: [Math.round(b.x + b.width / 2), Math.round(b.y + b.height / 2)],
                                    text: tt, cls: cls.slice(0, 80)};
                        }
                    }
                }
                return best;
            }"""
        )
        if not confirm_btn:
            modal_dump = await page.evaluate(
                """() => {
                    const modals = Array.from(document.querySelectorAll(
                        '[class*="modal"],[class*="Modal"],[class*="dialog"],[class*="Dialog"],[role="dialog"]'));
                    return modals.map(m => (m.innerText || '').slice(0, 300));
                }"""
            )
            result["modal_texts"] = modal_dump
            result.update(
                {
                    "status": "confirm_dialog_not_found",
                    "message": "没找到删除确认弹窗的确认按钮，删除可能未生效",
                    "screenshot": await self._shot(page, "delete_work_confirm_missing"),
                }
            )
        else:
            result["confirm_button_text"] = confirm_btn.get("text")
            result["confirm_button_cls"] = confirm_btn.get("cls")
            result["confirm_clicked"] = await self._click_box(page, confirm_btn["box"])
        await asyncio.sleep(3.0)

        # re-read the list to prove the work is gone
        still = [c for c in (await self._list_work_cards(page)) if _matches(c)]
        result["still_present"] = bool(still)
        result["screenshot_after"] = await self._shot(page, "delete_work_after")
        today = self._record_send_usage("delete")
        result.update(
            {
                "status": "deleted" if (result["confirm_clicked"] and not result["still_present"]) else "delete_unverified",
                "deleted": bool(result["confirm_clicked"] and not result["still_present"]),
                "usage_today_delete": today.get("delete", 0),
                "message": (
                    "已确认弹窗并回读：该作品已不在内容管理列表"
                    if (result["confirm_clicked"] and not result["still_present"])
                    else "删除流程已点击，但回读列表仍能看到目标作品，需人工核对"
                ),
            }
        )
        return result

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
            "login_state_file": self._login_state_summary(),
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
        session = self._browser_record()
        port = int(session.get("port") or 0)
        cdp = await _cdp_version(port) if port else None
        return {
            "status": "ok",
            "edge": edge,
            "edge_found": edge is not None,
            "playwright_available": pw_ok,
            "pillow_available": pil_ok,
            "state_dir": str(self.state_dir),
            "browser_session": session,
            "browser_recorded_cdp_alive": bool(cdp),
            "login_state_file": self._login_state_summary(),
            "login_state_acl": await self._acl_status(),
            "send_policy_path": str(self.state_dir / "send_policy.json"),
        }

    async def _act_close(self, page=None, context=None) -> dict[str, Any]:
        """Graceful shutdown so cookies hit disk; keeps the profile."""
        exported = None
        try:
            if page is not None:
                login = await self._detect_login(page)
                if login.get("logged_in"):
                    exported = await self._export_login_state(page, login)
        except Exception:
            pass
        closed = False
        try:
            if self._context is not None:
                await self._context.close()
                closed = True
            elif self._browser is not None:
                await self._browser.close()
                closed = True
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._browser = None
        self._context = None
        pid = self._browser_record().get("pid")
        killed = False
        kill_detail = ""
        # give the detached Edge a moment to flush cookies, then stop it.
        # Re-resolve the pid from the profile command line so we never kill a
        # process that merely reused a stale recorded pid.
        await asyncio.sleep(1.5)
        info = await self._find_profile_process()
        target = (info or {}).get("pid") or pid
        if target:
            try:
                if os.name == "nt":
                    proc = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(target), "/T", "/F",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
                    killed = proc.returncode == 0
                    kill_detail = (
                        f"taskkill pid={target} rc={proc.returncode} "
                        f"{_decode_console(out).strip()} {_decode_console(err).strip()}"
                    ).strip()
            except Exception as exc:
                kill_detail = f"taskkill failed: {exc}"
        try:
            (self.state_dir / "browser.json").unlink(missing_ok=True)
        except Exception:
            pass
        return {
            "status": "ok",
            "context_closed_gracefully": closed,
            "process_killed": killed,
            "kill_detail": kill_detail,
            "login_state_export": exported,
            "message": "Edge session closed gracefully; cookie profile and login_state.json preserved",
        }

    # -- dispatch -----------------------------------------------------------

    async def _run_action(self, action: str, **kw: Any) -> dict[str, Any]:
        if action in ("health", "usage"):
            if action == "health":
                return await self._act_health()
            return await self._act_usage()
        session = self._session_info or {}
        self._verify_aweme_id = bool(kw.get("verify_aweme_id", True))
        if kw.get("slot") is not None:
            slot = max(1, int(kw.get("slot") or 1))
        else:
            slot = 1
        keyword = kw.get("keyword")
        include_frames = bool(kw.get("include_frames", True))
        frame_count = max(2, min(16, int(kw.get("frame_count") or 4)))
        max_items = max(1, min(50, int(kw.get("max_items") or 0)))

        ctx, page = await self._ensure_session()
        session = self._session_info
        if action == "close":
            return await self._act_close(page=page, context=ctx)
        if action == "open":
            return await self._act_open(page)
        if action == "login_status":
            return await self._act_login_status(page)
        if action == "login_check":
            return await self._act_login_check(page)
        if action == "login_export":
            return await self._act_login_export(page)
        if action == "login_restore":
            return await self._act_login_restore(ctx, page)
        if action == "feed":
            return await self._act_feed(page, keyword, max_items or 10)
        if action == "search":
            return await self._act_search(page, keyword, max_items or 20)
        if action == "video":
            return await self._act_video(page, slot)
        if action == "like":
            return await self._act_like(page, slot, kw.get("aweme_id"), bool(kw.get("like", True)))
        if action == "storyboard":
            return await self._act_storyboard(page, slot, frame_count)
        if action == "comments":
            return await self._act_comments(page, slot, max_items or 30)
        if action == "watch_report":
            return await self._act_watch_report(page, keyword, max_items or 10, include_frames, frame_count)
        if action == "my_works":
            return await self._act_my_works(page, max_items or 20)
        if action == "send_comment":
            return await self._act_send_comment(page, slot, kw.get("text"), bool(kw.get("allow_send", False)))
        if action == "reply_to_comment":
            return await self._act_reply_to_comment(
                page,
                slot,
                kw.get("text"),
                int(kw["comment_index"]) if kw.get("comment_index") is not None else None,
                kw.get("comment_text_match"),
                bool(kw.get("allow_send", False)),
                kw.get("aweme_id"),
            )
        if action == "interactions":
            return await self._act_interactions(
                page,
                float(kw.get("hours") or 24),
                max_items or 20,
                kw.get("tab") or "comment",
                int(kw["locate_item"]) if kw.get("locate_item") is not None else None,
                kw.get("comment_text_match"),
            )
        if action == "send_forward":
            return await self._act_send_forward(page, slot, bool(kw.get("allow_send", False)))
        if action == "publish":
            return await self._act_publish(
                page,
                kw.get("video_path"),
                kw.get("title"),
                kw.get("desc"),
                kw.get("tags"),
                kw.get("mode") or "draft",
                bool(kw.get("allow_send", False)),
                bool(kw.get("allow_publish", False)),
            )
        if action == "delete_work":
            return await self._act_delete_work(
                page,
                kw.get("aweme_id"),
                kw.get("title"),
                bool(kw.get("confirm", False)),
            )
        return {"status": "error", "message": f"unknown action {action}", "session": session}

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
