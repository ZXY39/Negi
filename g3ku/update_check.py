"""发布标签识别与检查台账。

"这一台设备能取到的最新发布版是谁"收在 `resolve_latest_release()` 一处，两条车道：
git 车道在自己的检出里跑 `git ls-remote --tags origin`，HTTP 车道读
`https://github.com/<slug>/releases/latest` 的重定向目标。两条都只读，不上传任何本地
信息（版本号也不外发）。任何一条不通都落一条**带档**的 error 台账：查不到不等于已是
最新，而每一档失败在界面上说的话必须不一样——"没有 git""不是 git 检出""网络没通""超时"
"远端没有 vX.Y.Z 形状的标签"是五件不同的事。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_TAG_REF = re.compile(r"refs/tags/(v\d+\.\d+\.\d+)$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_GITHUB_REMOTE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_ORIGIN_SECTION = re.compile(r'^\[remote\s+["\']origin["\']\]\s*$')
_RELEASE_TAG_IN_URL = re.compile(r"/releases/tag/(v\d+\.\d+\.\d+)")
LEDGER_FILE = 'update-check.json'
DEFAULT_INTERVAL_SECONDS = 5 * 3600.0
# 上一次没查出标签时，自动道不必等满整个间隔：网络类失败按这个节拍重试。
RETRY_INTERVAL_SECONDS = 15 * 60.0
# 失败档。`remote_unreachable` 是其中一档，不再是所有失败的合称。
CODE_GIT_MISSING = 'git_missing'
CODE_NOT_A_CHECKOUT = 'not_a_checkout'
CODE_REMOTE_UNREACHABLE = 'remote_unreachable'
CODE_TIMEOUT = 'timeout'
CODE_NO_RELEASE_TAG = 'no_release_tag'
REMOTE_UNREACHABLE = CODE_REMOTE_UNREACHABLE
DEFAULT_REPO_SLUG = 'ZXY39/Negi'
GIT_TIMEOUT_SECONDS = 2.0
RELEASE_PAGE_TIMEOUT_SECONDS = 8.0
RELEASE_NOTES_TIMEOUT_SECONDS = 8.0
RELEASE_NOTES_UNAVAILABLE = 'release_notes_unavailable'
LANE_GIT = 'git'
LANE_HTTP = 'http'
PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def _no_window_kwargs() -> dict[str, Any]:
    # 桌面图标那条链上，宿主是 pythonw：没有控制台可继承，所以这里起的 git 会
    # 自己分配一个控制台窗口，用户在服务起来几秒后看到一个黑窗（检查台账为空时
    # 首轮就会跑）。标记只在 Windows 上有；真终端下子进程仍继承父控制台。
    popen_kwargs: dict[str, Any] = {}
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if create_no_window is not None and os.name == "nt":
        popen_kwargs["creationflags"] = create_no_window
    return popen_kwargs


def _registry_git_paths() -> list[str]:
    """Git for Windows 写下的安装位置，PATH 里没有 git 时的第二条线索。"""
    try:
        import winreg
    except ImportError:
        return []
    paths: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows") as key:
                install_path, _ = winreg.QueryValueEx(key, "InstallPath")
        except OSError:
            continue
        if install_path:
            paths.append(str(Path(str(install_path)) / "cmd" / "git.exe"))
    return paths


def resolve_git_executable(configured: str = "") -> str | None:
    """替 git 车道找可执行文件：PATH → `update_check.git_path` → 注册表 → 默认安装位置。

    PATH 排第一不是省事：「重启并更新」的执行体是安装脚本，它自己按名字调 git，所以
    只有从 PATH 找到的那一份，界面上报出的那一版才是这台设备真能升上去的。
    """
    found = shutil.which("git")
    if found:
        return found
    override = str(configured or "").strip().strip('"')
    candidates: list[str] = [override] if override else []
    if os.name == "nt":
        candidates.extend(_registry_git_paths())
        for root in (
            os.environ.get("ProgramFiles") or r"C:\Program Files",
            os.environ.get("ProgramFiles (x86)") or os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
        ):
            candidates.append(str(Path(root) / "Git" / "cmd" / "git.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def git_is_on_path() -> bool:
    """这台设备的升级车道能不能按名字调到 git——安装脚本自己就是这么找的。"""
    return shutil.which("git") is not None


def upgrade_lane_available(project_root: Path | None = None) -> tuple[bool, str]:
    """这台设备自己动手升级的那条车道现在能不能跑。

    不是 git 检出（归档包、离线套件装出来的）走 `archive/<ref>.zip`，不需要 git；git
    检出走安装脚本的 fetch，而脚本按名字调 git——PATH 里没有 git 时这一跳必然失败，
    所以报出新版本也不许把升级入口递过去：执行体会先关停服务、再升不动、再把旧版本
    拉回来，用户白花一次停机。返回的档位就是界面要说的那一句。
    """
    if not is_git_checkout(project_root):
        return True, ""
    if git_is_on_path():
        return True, ""
    return False, CODE_GIT_MISSING


def is_git_checkout(project_root: Path | None = None) -> bool:
    """只认这一层目录自己的 `.git`。

    向上找父仓库是 git 的默认语义，也正是"把别的仓库的标签当自己的最新版本"那条路，
    所以这里按安装根那一层判，不交给 git 去回溯。
    """
    return (Path(project_root or PROJECT_ROOT) / ".git").exists()


def _git_lane(
    project_root: Path,
    git_executable: str,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> tuple[str | None, str, str]:
    try:
        completed = subprocess.run(
            [git_executable, "ls-remote", "--tags", "origin"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_no_window_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return None, CODE_TIMEOUT, f"git ls-remote 超过 {timeout:g} 秒"
    except OSError as exc:
        return None, CODE_GIT_MISSING, str(exc)[:200]
    if completed.returncode != 0:
        return None, CODE_REMOTE_UNREACHABLE, (completed.stderr or "").strip()[:200]
    tag = latest_release_tag(completed.stdout or "")
    return (tag, "", "") if tag else (None, CODE_NO_RELEASE_TAG, "远端没有 vX.Y.Z 形状的标签")


def fetch_latest_release_tag(project_root: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> str | None:
    """git 车道的"只回标签"外形，给只关心标签的调用方与用例用。"""
    git_executable = resolve_git_executable()
    if not git_executable:
        return None
    tag, _code, _detail = _git_lane(Path(project_root), git_executable, timeout=timeout)
    return tag


class _StopAtRedirect(urllib.request.HTTPRedirectHandler):
    """只要 302 的 Location，不跟进那一页。

    发布页本身没有"最新"这个概念，能回答它的只有重定向目标；跟进去还会把一次
    读数变成一整页下载。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def latest_tag_from_release_target(target: str) -> str | None:
    """从 `releases/latest` 的重定向目标里取那一版标签。

    只认 `.../releases/tag/vX.Y.Z` 这一段形状；跟到别处（草稿、预览、不认识的
    路径）就是没读到，宁可回 None 也不猜一个版本给用户。
    """
    match = _RELEASE_TAG_IN_URL.search(str(target or ""))
    return match.group(1) if match else None


def _http_lane(slug: str, timeout: float = RELEASE_PAGE_TIMEOUT_SECONDS) -> tuple[str | None, str, str]:
    """HTTP 车道：读 `releases/latest` 的重定向目标拿那一版标签。

    地址与一行安装命令同源，因此不吃 `api.github.com` 的未鉴权限流。GitHub 的
    `latest` 取最新的非草稿非预览发布，这一点与"origin 上最高的 semver 标签"不
    总是同一个集合，所以车道选取规则见 `resolve_latest_release()`。
    """
    url = f"https://github.com/{slug}/releases/latest"
    opener = urllib.request.build_opener(_StopAtRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Negi-UpdateCheck"})
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            body = response.read(262144).decode("utf-8", "replace")
        tag = latest_tag_from_release_target(final_url) or latest_tag_from_release_target(body)
        if tag:
            return tag, "", url
        return None, CODE_NO_RELEASE_TAG, url
    except urllib.error.HTTPError as exc:
        location = str(exc.headers.get("Location") or "") if exc.headers is not None else ""
        tag = latest_tag_from_release_target(location)
        if tag:
            return tag, "", location
        code = CODE_REMOTE_UNREACHABLE if int(exc.code) >= 500 else CODE_NO_RELEASE_TAG
        return None, code, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None) or exc
        timed_out = isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower()
        return None, (CODE_TIMEOUT if timed_out else CODE_REMOTE_UNREACHABLE), str(reason)[:200]



def _release_result(
    tag: str,
    lane: str,
    attempts: list[str],
    *,
    root: Path,
    checkout: bool,
) -> dict[str, Any]:
    """把一次解析收成台账要写的形状。

    `code` 取**最后一条**失败档，也就是"搜索停在哪一步"：没有 git 的设备回落到发布页、
    发布页也没通时，用户当下该修的是网络，而 `detail` 里两条车道的原文都在。读到版本时
    `code` 为空、`degraded` 为真——换车道这件事必须留下证据，否则 git 装坏了和一切正常
    在界面上长得一模一样。

    `actionable` 答的是"报出去的这一版这台设备升不升得动"：git 检出要靠安装脚本、而脚本
    按名字找 git；不是检出的设备走归档包，不需要 git。
    """
    head = attempts[-1] if attempts else ""
    code = head.split(":", 1)[0]
    return {
        "tag": tag,
        "lane": lane,
        "code": "" if tag else code,
        "detail": "；".join(attempts),
        "checkout": checkout,
        "actionable": upgrade_lane_available(root)[0],
        "degraded": bool(attempts) and bool(tag),
    }


def resolve_latest_release(
    *,
    project_root: Path | None = None,
    git_path: str = "",
    git_timeout: float = GIT_TIMEOUT_SECONDS,
    http_timeout: float = RELEASE_PAGE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """查最新发布版，回答标签 + 用的哪条车道 + 失败档。

    只守一条判据：**报出去的那一版必须是这台设备自己取得到的那一版**。git 检出先问
    自己的 origin；origin 不是 GitHub 地址时**不**回落到发布页——那条读的是规范仓库，
    而这台设备的升级只会从它的 origin 取码，报出来却升不上去比不报更坏。不是 git 检出
    的设备（归档包、离线套件装出来的）本来就走 `archive/<ref>.zip`，与发布页同源，
    所以规范 slug 就是它能取到的那一版。
    """
    root = Path(project_root or PROJECT_ROOT)
    checkout = is_git_checkout(root)
    git_executable = resolve_git_executable(git_path)
    slug = origin_repo_slug(root) if checkout else DEFAULT_REPO_SLUG
    attempts: list[str] = []

    if not checkout:
        attempts.append(f"{CODE_NOT_A_CHECKOUT}: {root} 没有 .git")
    elif not git_executable:
        attempts.append(f"{CODE_GIT_MISSING}: PATH、git_path 与常见安装位置都没有 git")
    else:
        tag, code, detail = _git_lane(root, git_executable, timeout=git_timeout)
        if tag:
            return _release_result(tag, LANE_GIT, attempts, root=root, checkout=checkout)
        attempts.append(f"{code}: {detail}" if detail else code)

    if slug:
        tag, code, detail = _http_lane(slug, timeout=http_timeout)
        if tag:
            return _release_result(tag, LANE_HTTP, attempts, root=root, checkout=checkout)
        attempts.append(f"{code}: {detail}" if detail else code)

    return _release_result("", "", attempts, root=root, checkout=checkout)



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


def build_ledger_payload(result: dict[str, Any], *, source: str) -> dict[str, Any]:
    """把一次解析落成台账。

    `error` 仍是那个"查失败"的字段，只是现在它带的是档位而不是一个合称；调用方读
    `error` 判失败、读 `lane`/`detail` 说清是哪条车道为什么失败、读 `actionable` 决定
    给不给升级入口。
    """
    from g3ku import __version__

    latest_tag = str(result.get("tag") or "")
    current = parse_version(__version__)
    latest = parse_version(latest_tag) if latest_tag else None
    return {
        "checked_at": _now().isoformat(timespec="seconds"),
        "current_version": __version__,
        "latest_tag": latest_tag,
        "newer": bool(latest and current and latest > current),
        "source": source,
        "error": str(result.get("code") or ""),
        "lane": str(result.get("lane") or ""),
        "detail": str(result.get("detail") or ""),
        "checkout": bool(result.get("checkout")),
        "actionable": bool(result.get("actionable")),
        "degraded": bool(result.get("degraded")),
    }


def retry_gate_seconds(ledger: dict[str, Any] | None, interval_seconds: float) -> float:
    """上一次什么标签都没查出来时，自动道按 `RETRY_INTERVAL_SECONDS` 重试。

    五小时对一个瞬时网络失败太钝：那条车道失败过一次，就要等满半天才再有第二次
    机会，而界面在此期间一直显示"上次检查失败"。查成功仍然回到配置的间隔。
    """
    if ledger and not str(ledger.get("latest_tag") or "").strip():
        return min(max(1.0, float(interval_seconds)), RETRY_INTERVAL_SECONDS)
    return max(1.0, float(interval_seconds))


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
    git_path: str = "",
) -> dict[str, Any] | None:
    """Check the remote tags and persist the outcome.

    ``source='auto'`` respects the interval so a periodic caller can run on every
    tick; ``force`` (a manual click) bypasses it. Returns the ledger, or the
    untouched previous ledger when an automatic pass is not due yet.

    根目录默认取**本项目根**，不是进程的工作目录：桌面图标与「重启并更新」重拉起
    传下来的 cwd 各有各的落点，按 cwd 判"是不是 git 检出"会让一台设备读到别人仓库的
    标签，或干脆把自己判成非检出。
    """
    root = Path(project_root or PROJECT_ROOT)
    target = ledger_file or ledger_path()
    ledger = read_update_ledger(target)
    if source == "auto" and not force and not check_is_due(ledger, retry_gate_seconds(ledger, interval_seconds)):
        return ledger
    result = resolve_latest_release(project_root=root, git_path=git_path)
    payload = build_ledger_payload(result, source=source)
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


def _origin_url_from_config(text: str) -> str | None:
    """在 `.git/config` 的 `[remote "origin"]` 段里找那一行 url。"""
    inside = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            if inside:
                break
            inside = bool(_ORIGIN_SECTION.match(stripped))
            continue
        if inside and stripped.lower().startswith("url"):
            _, _, value = stripped.partition("=")
            return value.strip().strip('"') or None
    return None


def origin_repo_slug(project_root: Path | None = None) -> str | None:
    """这台设备自己的 origin 是哪一家，读 `<root>/.git/config`。

    不起 git：PATH 里没有 git 的设备照样要能取发布说明、也要能让车道选取判出"我的
    origin 是不是 GitHub"。向上找父仓库同样不在这里发生——只读这一层的 config。
    """
    config = Path(project_root or PROJECT_ROOT) / ".git" / "config"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_repo_slug(_origin_url_from_config(text) or "")



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
