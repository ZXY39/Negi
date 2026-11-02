r"""Both installer lanes must hand a lost local edit to an object, not to log prose only.

The upgrade path swaps code under a dirty working tree. Before this lane existed a conflict
meant a whole-tree `reset --hard`, which also dropped the edits that did not conflict, and the
copy lane (Setup.exe / offline kit: no `.git`) simply overwrote them with nothing left behind.
Both now delegate to `g3ku/update_local_edits.py`, and the settings row reads what it wrote.

These cases pin the four places that have to agree, because a script edit that silently drops
one of them still produces a working installer -- it just loses someone's work again.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_SH = REPO_ROOT / "install.sh"
MODULE = REPO_ROOT / "g3ku" / "update_local_edits.py"


def _ps1_function(name: str) -> str:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    match = re.search(rf"^function {re.escape(name)} \{{\n(.*?)^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"install.ps1 lost function {name}"
    return _code_only(match.group(1))


def _sh_function(name: str) -> str:
    text = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"install.sh lost function {name}"
    return _code_only(match.group(1))


def _code_only(body: str) -> str:
    """剥掉注释后的函数体。

    判据只看着陆在代码里的命令，不扫解释性文字 —— 这些函数里恰恰写着"旧做法是整树
    reset --hard"、"判据用 diff 不用 status"，按全文匹配会把说明当成缺陷。
    """
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split("#", 1)[0])
    return "\n".join(lines)


def test_git_lane_restore_reverts_per_path_instead_of_the_whole_tree() -> None:
    for body in (_ps1_function("Restore-TrackedChanges"), _sh_function("restore_tracked_changes")):
        assert "reset --hard" not in body, "整树 reset --hard 会把同一批里没撞行的改动一起抹掉"
        assert "preserve-git" in body, "冲突处置必须交给两条车道共用的那份实现"
        assert "stash pop" in body


def test_copy_lane_protects_before_it_overwrites() -> None:
    # 没有 .git 的设备只靠这一步：覆盖之后就没有任何东西能说明谁改过什么。
    ps_body = _ps1_function("Copy-CheckoutInto")
    sh_body = _sh_function("copy_checkout_into_root")
    assert "preserve-copy" in ps_body and "Copy-Item" in ps_body
    assert ps_body.index("preserve-copy") < ps_body.index("Copy-Item")
    assert "preserve-copy" in sh_body and "cp -Rf" in sh_body
    assert sh_body.index("preserve-copy") < sh_body.index("cp -Rf")


def test_installed_tree_fingerprint_is_recorded_after_the_environment() -> None:
    # 指纹是拷贝车道区分「用户改的」与「新版本改的」的唯一凭据，所以必须在装完之后记录。
    ps = INSTALL_PS1.read_text(encoding="utf-8")
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert "'record'" in ps and "Install-Environment" in ps
    assert ps.index("Install-Environment -uv") < ps.index("'record'")
    assert re.search(r"^record_installed_tree$", sh, re.MULTILINE)
    assert sh.index("install_environment\n") < sh.index("record_installed_tree\n")


def test_protection_never_holds_up_the_upgrade() -> None:
    # 没有解释器（首装还没有 venv）或模块还不存在（从更早的版本升级上来）时必须静默跳过，
    # 而且不许把退出码变成升级失败。
    ps_body = _ps1_function("Invoke-LocalEdits")
    sh_body = _sh_function("run_local_edits")
    assert "if (-not $python) { return }" in ps_body
    assert "Write-Error" not in ps_body and "throw" not in ps_body
    assert 'py="$(upgrade_python_bin)" || return 0' in sh_body
    assert '|| return 0' in sh_body
    assert "fail " not in sh_body
    # 模块侧同样只报告不抛：保护动作的异常绝不能变成升级失败。
    module = MODULE.read_text(encoding="utf-8")
    assert 'except Exception as exc:  # noqa: BLE001 - 保护动作绝不能让升级失败' in module
    assert "return 0" in module.split("def main(")[1]


def test_the_shared_module_owns_the_object_shape() -> None:
    source = MODULE.read_text(encoding="utf-8")
    for token in ("CONFLICTS_DIRNAME", "CONFLICTS_KEEP", "preserve_git", "preserve_copy", "record_tree", "read_latest", "prune"):
        assert token in source
    # 两侧脚本用的锚点前缀必须与模块一致，否则日志里的批次路径读不出来。
    assert 'BATCH_PREFIX = "NEGI_LOCAL_EDITS"' in source
    assert "NEGI_LOCAL_EDITS" in _ps1_function("Invoke-LocalEdits")
    assert "NEGI_LOCAL_EDITS" in _sh_function("run_local_edits")


def test_untracked_operator_files_are_never_part_of_the_judgement() -> None:
    # 设备上的未跟踪项（用户装的 skill、桥接产物、本地脚本）永远不为空，算进脏判定就把
    # 升级永久拒死；判据必须是 diff 而不是 status。
    for body in (_ps1_function("Test-TrackedChanges"), _sh_function("update_git_checkout")):
        assert "diff --quiet --ignore-cr-at-eol" in body
        assert "diff --cached --quiet" in body
        assert re.search(r"git[^\n]*\bstatus\b", body) is None


def test_the_settings_row_shows_a_count_and_no_path_list() -> None:
    """面板那一行只报数量：清单收在按钮的 title 与批次目录里，不再是原始路径列表。

    这三份文件必须同时改：页面 markup、注册进 U 的节点、模块本身。任何一处漏改都会让
    那一行去要一个页面上不存在的容器，或者悄悄把路径列表又画回来。
    """
    html = (REPO_ROOT / "g3ku" / "web" / "frontend" / "org_graph.html").read_text(encoding="utf-8")
    app = (REPO_ROOT / "g3ku" / "web" / "frontend" / "org_graph_app.js").read_text(encoding="utf-8")
    module = (REPO_ROOT / "g3ku" / "web" / "frontend" / "ceo" / "update-conflicts.js").read_text(encoding="utf-8")

    for name in ("update-conflict-paths", "update-conflict-hint", "updateConflictPaths", "updateConflictHint"):
        assert name not in html, f"{name} 还在页面里"
        assert name not in app, f"{name} 还注册在 U 里"
        assert name not in module, f"{name} 还被模块引用"
    for name in ("update-conflict-block", "update-conflict-headline", "update-conflict-negi-btn"):
        assert name in html and name in app
    # 共用 /ws/ceo 上回合归服务端激活会话，帧里的 session_id 不路由：这条界面不许碰任何
    # 自动发送车道，只把提示词写成那条会话自己的输入框草稿。
    assert "setCeoComposerDraft(sessionId" in module
    assert "restoreCeoComposerDraftForSession" in module
    for forbidden in ("sendActiveCeoFollowUpsToRuntime", "enqueueCeoFollowUp", "flushCeoQueuedFollowUp"):
        assert forbidden not in module, f"{forbidden} 会把消息投进别的会话"


def test_fetch_depth_follows_the_trees_own_shape() -> None:
    """`--depth 1` 只给本来就是浅仓的设备；全历史树被它取一次就把历史塌成一个提交。

    实测过损坏形状：一次升级让 `git log` 从 3 个提交变成 1 个，并多出 `.git/shallow`。
    """
    ps = INSTALL_PS1.read_text(encoding="utf-8")
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert ".git\\shallow" in ps and "git fetch --depth 1" in ps
    assert ps.index("Test-Path -LiteralPath (Join-Path $Root '.git\\shallow')") < ps.index("'fetch', '--depth', '1'")
    assert "fetch', 'origin', $Ref" in ps, "非浅仓要走保留历史的 fetch"
    assert '.git/shallow' in sh and 'fetch --depth 1 origin "$REF"' in sh
    assert sh.index('if [ -f "$DIR/.git/shallow" ]') < sh.index('fetch --depth 1 origin "$REF"')
    assert 'fetch origin "$REF"' in sh


def test_the_ledger_carries_the_conflict_fields_to_the_surface() -> None:
    source = (REPO_ROOT / "g3ku" / "update_apply.py").read_text(encoding="utf-8")
    for field in ("conflict_paths", "conflict_count", "conflict_lane", "conflict_stash", "conflict_dir", "conflict_at"):
        assert f'"{field}"' in source
    # 中间阶段仍写 outcome="started"：单飞闸门判的就是它，新终态会让用户再踢一次执行体。
    assert '_record("started", ref, detail, stage=stage)' in source
