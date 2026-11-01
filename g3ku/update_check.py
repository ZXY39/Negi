"""发布标签识别与检查台账。

远端读取只做 `git ls-remote --tags`，不上传任何本地信息（版本号也不外发）。
任何失败都返回 None / 落一条 error 台账：没有 git、没有网络或远端不是 GitHub
的设备，只是"查不到"，不该影响调用方，也绝不被渲染成"已是最新"。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_TAG_REF = re.compile(r"refs/tags/(v\d+\.\d+\.\d+)$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_GITHUB_REMOTE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
LEDGER_FILE = 'update-check.json'
DEFAULT_INTERVAL_SECONDS = 5 * 3600.0
REMOTE_UNREACHABLE = 'remote_unreachable'
DEFAULT_REPO_SLUG = 'ZXY39/Negi'
RELEASE_NOTES_TIMEOUT_SECONDS = 8.0
RELEASE_NOTES_UNAVAILABLE = 'release_notes_unavailable'


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def latest_release_tag(ls_remote_output: str) -> str | None:
    """Pick the highest semver tag from `git ls-remote --tags` stdout.

    Non-releases are filtered by shape: only a bare `refs/tags/vX.Y.Z` counts, so
    `refs/tags/backup/...` and the peeled `^{}` duplicates are skipped.
    """
    best: tuple[int, int, int] | None = None
    best_tag: str | None = None
    for line in ls_remote_output.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        match = _TAG_REF.search(fields[1].strip())
        if not match:
            continue
        tag = match.group(1)
        version = parse_version(tag)
        if version is None:
            continue
        if best is None or version > best:
            best = version
            best_tag = tag
    return best_tag


def fetch_latest_release_tag(project_root: Path, timeout: float = 2.0) -> str | None:
    # 桌面图标那条链上，宿主是 pythonw：没有控制台可继承，所以这里起的 git 会
    # 自己分配一个控制台窗口，用户在服务起来几秒后看到一个黑窗（检查台账为空时
    # 首轮就会跑）。标记只在 Windows 上有；真终端下子进程仍继承父控制台。
    popen_kwargs: dict[str, Any] = {}
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if create_no_window is not None and os.name == "nt":
        popen_kwargs["creationflags"] = create_no_window
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--tags", "origin"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **popen_kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return latest_release_tag(completed.stdout or "")


def _now() -> datetime:
    return datetime.now().astimezone()


def ledger_path() -> Path:
    from g3ku.config.loader import get_data_dir

    return get_data_dir() / LEDGER_FILE


def read_update_ledger(path: Path | None = None) -> dict[str, Any] | None:
    """Last check result, or None when nothing has been checked yet.

    A missing or corrupt ledger is "unknown", which callers must not render as
    "up to date".
    """
    target = path or ledger_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_update_ledger(payload: dict[str, Any], path: Path | None = None) -> None:
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def check_is_due(ledger: dict[str, Any] | None, interval_seconds: float) -> bool:
    if not ledger:
        return True
    checked_at = str(ledger.get("checked_at") or "").strip()
    if not checked_at:
        return True
    try:
        last = datetime.fromisoformat(checked_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.astimezone()
    return (_now() - last).total_seconds() >= max(0.0, float(interval_seconds))


def build_ledger_payload(latest_tag: str | None, *, source: str) -> dict[str, Any]:
    from g3ku import __version__

    current = parse_version(__version__)
    latest = parse_version(latest_tag) if latest_tag else None
    return {
        "checked_at": _now().isoformat(timespec="seconds"),
        "current_version": __version__,
        "latest_tag": latest_tag or "",
        "newer": bool(latest and current and latest > current),
        "source": source,
        "error": "" if latest_tag else REMOTE_UNREACHABLE,
    }


def ledger_newer_now(ledger: dict[str, Any] | None) -> bool:
    """台账里那次检查发现的标签，此刻是否仍然比本机版本新。

    台账存的 `newer` 是检查那一刻的结论，而升级只换代码不换台账：照抄它会让红点在
    升级成功后一直亮到下一次检查（实测要等满一个 5 小时间隔），启动横幅也会继续念
    旧版本号。调用方一律用本函数读数，台账原字段只回答"上次检查看到了什么"。
    """
    from g3ku import __version__

    if not isinstance(ledger, dict):
        return False
    current = parse_version(__version__)
    latest = parse_version(str(ledger.get("latest_tag") or ""))
    return bool(current and latest and latest > current)


def run_update_check(
    *,
    source: str = "auto",
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    project_root: Path | None = None,
    ledger_file: Path | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Check the remote tags and persist the outcome.

    ``source='auto'`` respects the interval so a periodic caller can run on every
    tick; ``force`` (a manual click) bypasses it. Returns the ledger, or the
    untouched previous ledger when an automatic pass is not due yet.
    """
    root = project_root or Path.cwd()
    target = ledger_file or ledger_path()
    ledger = read_update_ledger(target)
    if source == "auto" and not force and not check_is_due(ledger, interval_seconds):
        return ledger
    latest_tag = fetch_latest_release_tag(root)
    payload = build_ledger_payload(latest_tag, source=source)
    try:
        write_update_ledger(payload, target)
    except OSError:
        return payload
    return payload


APPLY_RESULT_FILE = 'update-apply-result.json'


def apply_result_path() -> Path:
    from g3ku.config.loader import get_data_dir

    return get_data_dir() / APPLY_RESULT_FILE


def read_apply_result(path: Path | None = None) -> dict[str, Any] | None:
    """上一次「重启并更新」的结局，没有记录时返回 None。

    与台账同一个道理：没有文件就是"没发生过"，调用方不许把它渲染成"已经升级好了"。
    """
    target = path or apply_result_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_apply_result(payload: dict[str, Any], path: Path | None = None) -> None:
    """执行体的终态回写：让"点了没反应"在设置行里变成可读的一句。

    写失败不影响升级本身，所以这里只吞 OSError。
    """
    target = path or apply_result_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        return


# ---- 发布说明 ---------------------------------------------------------------
#
# 发布页正文由 .github/workflows/release.yml 从**附注标签的正文**生成，所以"这一版
# 改了什么"在生产链路里只有这一份来源：检查更新只读标签名，正文要单独取。取不到
# 正文不是错误状态 —— 升级走 git，和网页取不到说明互不影响。

_RELEASE_NOTES_CACHE: dict[str, dict[str, Any]] = {}


def parse_repo_slug(remote_url: str) -> str | None:
    """从 origin 地址取 `owner/repo`，兼容 https / ssh / 镜像前缀。"""
    match = _GITHUB_REMOTE.search(str(remote_url or "").strip())
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    return f"{owner}/{repo}" if owner and repo else None


def origin_repo_slug(project_root: Path | None = None) -> str | None:
    popen_kwargs: dict[str, Any] = {}
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if create_no_window is not None and os.name == "nt":
        popen_kwargs["creationflags"] = create_no_window
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_root or Path.cwd()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            **popen_kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return parse_repo_slug(completed.stdout or "")


class _ReleaseBodyParser(HTMLParser):
    """收集 GitHub 发布页里 `.markdown-body` 那一段的文字。

    HTML 兜底只在 API 被限流时用到，所以只保段落/列表/换行的形状：不还原链接地址、
    不保留图片（正文里它们只是说明），最终按纯文本画进弹窗。
    """

    _BLOCK = {
        "p", "div", "section", "article", "blockquote", "pre", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6", "li",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capturing = False
        self._div_depth = 0
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if not self._capturing:
            if tag == "div" and "markdown-body" in classes:
                self._capturing = True
            return
        if tag == "div":
            self._div_depth += 1
        if tag in {"script", "style", "svg"}:
            self._skip += 1
            return
        if tag == "br":
            self._parts.append("\n")
            return
        if tag in self._BLOCK:
            self._parts.append("\n")
            if tag == "li":
                self._parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if not self._capturing:
            return
        if tag in {"script", "style", "svg"}:
            self._skip = max(0, self._skip - 1)
        if tag in self._BLOCK:
            self._parts.append("\n")
        if tag == "div":
            if self._div_depth == 0:
                self._capturing = False
                return
            self._div_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._capturing or self._skip:
            return
        text = data.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            # 块级标签之间那些只用来缩进源码的换行不参与排版。
            return
        # `<br>` 已在流里放了换行，HTML 源码里紧跟的那个换行不能再叠一层，
        # 否则 GitHub 的软换行会变成一行一个空行。
        if self._parts and self._parts[-1].endswith("\n"):
            text = text.lstrip("\n")
        self._parts.append(text)

    def text(self) -> str:
        raw = "".join(self._parts).replace("\r\n", "\n").replace("\r", "\n")
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in raw.split("\n")]
        collapsed: list[str] = []
        for line in lines:
            if not line and (not collapsed or not collapsed[-1]):
                continue
            collapsed.append(line)
        return "\n".join(collapsed).strip()


def extract_release_body(page_html: str) -> str:
    parser = _ReleaseBodyParser()
    try:
        parser.feed(page_html or "")
    except Exception:
        return ""
    return parser.text()


def _http_get_text(url: str, timeout: float, accept: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "Negi-UpdateCheck",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_release_notes(
    tag: str,
    *,
    project_root: Path | None = None,
    timeout: float = RELEASE_NOTES_TIMEOUT_SECONDS,
    use_cache: bool = True,
) -> dict[str, Any]:
    """取某一版的发布说明；取不到时 `error` 非空、`body` 为空。

    先走 API（一次请求拿到正文），被限流或离线再读发布页 HTML。失败不缓存，
    下一次点开还会重试；成功按 tag 缓存在进程内，弹窗反复开关不重复联网。
    """
    ref = str(tag or "").strip()
    if ref and not ref.startswith("v"):
        ref = f"v{ref}"
    slug = origin_repo_slug(project_root) or DEFAULT_REPO_SLUG
    cache_key = f"{slug}:{ref}"
    if use_cache and cache_key in _RELEASE_NOTES_CACHE:
        return dict(_RELEASE_NOTES_CACHE[cache_key])

    page_url = f"https://github.com/{slug}/releases/tag/{ref}"
    payload: dict[str, Any] = {
        "ref": ref,
        "title": ref,
        "body": "",
        "html_url": page_url,
        "source": "",
        "error": "",
    }
    api_url = f"https://api.github.com/repos/{slug}/releases/tags/{ref}"
    try:
        detail = json.loads(_http_get_text(api_url, timeout, "application/vnd.github+json"))
        body = str((detail or {}).get("body") or "").strip()
        if body:
            payload.update(
                title=str(detail.get("name") or ref),
                body=body,
                html_url=str(detail.get("html_url") or page_url),
                source="api",
            )
            if use_cache:
                _RELEASE_NOTES_CACHE[cache_key] = dict(payload)
            return payload
    except Exception:
        pass

    try:
        body = extract_release_body(_http_get_text(page_url, timeout, "text/html"))
        if body:
            payload.update(body=body, source="page")
            if use_cache:
                _RELEASE_NOTES_CACHE[cache_key] = dict(payload)
            return payload
    except Exception:
        pass

    payload["error"] = RELEASE_NOTES_UNAVAILABLE
    return payload
