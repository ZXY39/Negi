"""换码这一步没能保住的本地改动：对象形状、两条车道的落点、保留策略。

设备上"用户改过 Negi 的代码"只有两种结局可选：在换码时被无声覆盖，或者被存成一份谁都
能读的对象。这个模块负责后者。两条安装车道（git 检出、源码包/离线套件）调用同一份实现，
写同一形状的清单；`g3ku.update_apply` 读它并并进「重启并更新」的台账，界面据此列文件、
给"交给 Negi 解决"那颗按钮。

    <数据根>/update-conflicts/<utc 戳>/manifest.json
    <数据根>/update-conflicts/<utc 戳>/local-edits.patch       lane=merge-conflict / restore-failed
    <数据根>/update-conflicts/<utc 戳>/overwritten/<相对路径>  lane=copy-overwrite

lane 取值就这三档，界面按档说话：

- `merge-conflict`：git 车道，`stash pop` 撞行。只回退真正未合并的路径，同一批里没撞行的
  改动留在树上（旧做法是整树 `reset --hard`，那会把没撞行的也一起抹掉，用户只看到"更新成功"）。
- `restore-failed`：git 车道，pop 非零但没有未合并路径（工作树被别的东西占住等）。保守回退
  整树，清单里是这一批 stash 的全部内容。
- `copy-overwrite`：拷贝车道（Setup.exe / 离线套件装的，树里没有 `.git`）。`cp -Rf` 直接覆盖
  跟踪文件，过去连一份副本都不留；现在按安装时指纹判"是不是用户改的"，把原件拷走。

拷贝车道没有 git index 可比，所以另存一份安装时的文件指纹
`<数据根>/update-installed-tree.json`：下一轮升级时，指纹与实际内容不符、而新版本又与两者
都不同的文件，才算用户改过。指纹不存在的那一轮（升级到这个版本之前装的设备）不产出清单，
行为与今天一致，不做误报。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFLICTS_DIRNAME = "update-conflicts"
CONFLICTS_KEEP = 3
TREE_LEDGER_FILE = "update-installed-tree.json"

# 换码永不触碰的顶层目录（与两条安装脚本的 PROTECTED_ENTRIES 同源）。
PROTECTED_TOP_LEVELS = frozenset({".venv", ".g3ku", ".git"})
SKIP_DIR_NAMES = frozenset({".venv", ".g3ku", ".git", "__pycache__", "node_modules"})

BATCH_PREFIX = "NEGI_LOCAL_EDITS"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_data_root() -> Path:
    from g3ku.config.loader import get_data_dir

    return Path(get_data_dir())


def conflicts_root(data_root: Path | None = None) -> Path:
    return (data_root if data_root is not None else default_data_root()) / CONFLICTS_DIRNAME


def tree_ledger_path(data_root: Path | None = None) -> Path:
    return (data_root if data_root is not None else default_data_root()) / TREE_LEDGER_FILE


# ---- 清单的读写与保留 ------------------------------------------------------


def write_manifest(
    batch_dir: Path,
    *,
    lane: str,
    ref: str,
    paths: list[str],
    stash_label: str = "",
    artifact: str = "",
    at: str = "",
) -> dict[str, Any]:
    payload = {
        "at": at or _utc_iso(),
        "ref": str(ref or ""),
        "lane": lane,
        "stash_label": stash_label,
        "artifact": artifact,
        "paths": [str(item) for item in paths],
    }
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {**payload, "batch_dir": str(batch_dir)}


def read_batch(batch_dir: Path) -> dict[str, Any] | None:
    """读一份批次；形状不对就当没发生过，调用方不许把缺失误报成"已保住"。"""
    try:
        data = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    paths = [str(item).strip() for item in (data.get("paths") or []) if str(item).strip()]
    if not paths:
        return None
    return {**data, "paths": paths, "batch_dir": str(batch_dir)}


def list_batches(data_root: Path | None = None) -> list[Path]:
    base = conflicts_root(data_root)
    try:
        return sorted(
            (entry for entry in base.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
        )
    except OSError:
        return []


def read_latest(
    *,
    since: str = "",
    data_root: Path | None = None,
) -> dict[str, Any] | None:
    """取这一轮升级留下的批次；早于 `since`（UTC ISO 串）的属于上一次升级。

    上一轮的陈旧批次仍然留在盘上（用户可能还没处理），但不该再占这一行的文案 ——
    否则一次没解决的冲突会每轮被重复播报。
    """
    for entry in reversed(list_batches(data_root)):
        record = read_batch(entry)
        if record is None:
            continue
        stamp = str(record.get("at") or "")
        if since and stamp and stamp < since:
            return None
        return record
    return None


def prune(keep: int = CONFLICTS_KEEP, data_root: Path | None = None) -> int:
    """只留最近 `keep` 批，返回删掉的批次数。清的是自己写的子树，不动别处。"""
    entries = list_batches(data_root)
    removed = 0
    for entry in entries[: max(len(entries) - keep, 0)]:
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


# ---- git 车道 --------------------------------------------------------------


def _git(root: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git") or "git"
    return subprocess.run(
        [git, "-C", str(root), *args],
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _stash_side_paths(root: Path, label_index: str = "stash@{0}") -> list[str]:
    got = _git(root, "stash", "show", "--name-only", label_index)
    if got.returncode != 0:
        return []
    return [line.strip() for line in (got.stdout or "").splitlines() if line.strip()]


def _git_show(root: Path, revision: str) -> str:
    got = _git(root, "show", revision)
    if got.returncode != 0:
        return ""
    return got.stdout or ""


def _exists_in_head(root: Path, rel: str) -> bool:
    got = _git(root, "cat-file", "-e", f"HEAD:{rel}")
    return got.returncode == 0


def _save_user_side(root: Path, batch: Path, rel: str) -> None:
    """把用户那一份原文存进批次目录 —— 从 stash 侧取，不从工作树取。

    工作树里那个文件在 UU 情形下带的是冲突标记，不是用户的干净版本；`stash@{0}:<路径>`
    才是他写下的内容。被新版本删掉的文件也走这里，否则那份改动只存在于 patch 文本里。
    """
    text = _git_show(root, f"stash@{{0}}:{rel}")
    if not text.strip():
        return
    destination = batch / "overwritten" / rel
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8", newline="\n")
    except OSError:
        return


def preserve_git(root: Path, ref: str, stash_label: str = "", data_root: Path | None = None) -> dict[str, Any] | None:
    """`stash pop` 失败后的处置：只回退真正未合并的路径，其余留在树上，并落成清单。

    守住的不变量是"活服务永远拿不到未合并的路径或带冲突标记的文件"。旧实现顺带把同一批里
    没撞行的改动也抹掉了（整树 `reset --hard`），这一版不顺带。
    """
    root = Path(root)
    unmerged = [
        line.strip()
        for line in (_git(root, "diff", "--name-only", "--diff-filter=U").stdout or "").splitlines()
        if line.strip()
    ]
    if unmerged:
        lane = "merge-conflict"
        paths = unmerged
    else:
        # pop 非零却找不到未合并路径：说不清工作树现在处于什么形态，保守整树回退，
        # 宁可少留改动也不把半成品交给活进程。
        lane = "restore-failed"
        paths = _stash_side_paths(root)
        _git(root, "reset", "--hard", "HEAD")
    if not paths:
        return None
    batch = (data_root if data_root is not None else default_data_root()) / CONFLICTS_DIRNAME / _utc_stamp()
    batch.mkdir(parents=True, exist_ok=True)
    patch_name = ""
    patch = _git(root, "stash", "show", "-p", "stash@{0}")
    if patch.returncode == 0 and (patch.stdout or "").strip():
        patch_name = "local-edits.patch"
        (batch / patch_name).write_text(patch.stdout, encoding="utf-8", newline="\n")

    if lane == "merge-conflict":
        for rel in paths:
            _save_user_side(root, batch, rel)
            if _exists_in_head(root, rel):
                # 新版本有这一份：树回到新版内容，用户的版本留在 stash 与 overwritten/。
                _git(root, "checkout", "HEAD", "--", rel)
            else:
                # 新版本把它删了（modify/delete）。`checkout HEAD -- <路径>` 对这种路径
                # 不起作用，实测会留下 `error: could not write index` 和一个未合并条目 ——
                # 那正是不能交给活进程的形状。这里摘掉索引条目并让文件随新版本一起消失，
                # 用户那一份已经在 overwritten/ 与 patch 里。
                _git(root, "rm", "-f", "--cached", "--", rel)
                try:
                    (root / rel).unlink()
                except OSError:
                    pass
    return write_manifest(
        batch,
        lane=lane,
        ref=ref,
        paths=paths,
        stash_label=stash_label,
        artifact=patch_name or "overwritten",
    )


# ---- 拷贝车道（源码包 / 离线套件）------------------------------------------


def _walk_files(base: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = Path(dirpath).relative_to(base)
        if rel_dir.parts:
            head = rel_dir.parts[0]
            if head in PROTECTED_TOP_LEVELS:
                dirnames[:] = []
                continue
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            out.append((rel_dir / name).as_posix())
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def read_tree_ledger(data_root: Path | None = None) -> dict[str, str]:
    target = tree_ledger_path(data_root)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(key): str(value) for key, value in files.items() if isinstance(value, str)}


def record_tree(root: Path, ref: str, data_root: Path | None = None) -> int:
    """把刚装好的这棵树的文件指纹落进数据根，供下一轮升级判"是不是用户改的"。

    没有这份指纹，拷贝车道就分不开"用户改过"与"新版本本来就改了" —— 实测过按内容比对
    会把新版本改动的每一个文件都算成用户的改动（一轮 73 个文件全进清单）。
    """
    root = Path(root)
    files: dict[str, str] = {}
    for rel in _walk_files(root):
        digest = _sha256(root / rel)
        if digest:
            files[rel] = digest
    target = (data_root if data_root is not None else default_data_root()) / TREE_LEDGER_FILE
    payload = {"at": _utc_iso(), "ref": str(ref or ""), "files": files}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8", newline="\n")
        os.replace(temp, target)
    except OSError:
        return 0
    return len(files)


def preserve_copy(
    root: Path,
    new_checkout: Path,
    ref: str,
    data_root: Path | None = None,
) -> dict[str, Any] | None:
    """覆盖之前，把"设备上的内容与安装指纹不符"的文件原件拷进批次目录。

    驱动集是新检出的文件清单：用户自己加的目录不在新版本的文件清单里，永远不会被列进
    这份"被覆盖"名单（它们本来也不会被删 —— 拷贝车道是顶层合并）。
    """
    root = Path(root)
    ledger = read_tree_ledger(data_root)
    if not ledger:
        return None
    changed: list[str] = []
    originals: list[tuple[str, Path]] = []
    for rel in _walk_files(Path(new_checkout)):
        installed_digest = ledger.get(rel)
        if not installed_digest:
            continue
        current = root / rel
        if not current.is_file():
            continue
        current_digest = _sha256(current)
        if not current_digest or current_digest == installed_digest:
            continue
        changed.append(rel)
        originals.append((rel, current))
    if not changed:
        return None
    batch = (data_root if data_root is not None else default_data_root()) / CONFLICTS_DIRNAME / _utc_stamp()
    overwrites = batch / "overwritten"
    for rel, source in originals:
        destination = overwrites / rel
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        except OSError:
            continue
    return write_manifest(
        batch,
        lane="copy-overwrite",
        ref=ref,
        paths=changed,
        artifact="overwritten",
    )


# ---- 安装脚本的调用面 ------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """两条安装脚本的唯一入口：失败也要把话说完再退，绝不因为保护动作拖住升级。

    stdout 一律一行 `NEGI_LOCAL_EDITS <manifest 路径|none|error:...>`，日志里可 grep。
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m g3ku.update_local_edits")
    parser.add_argument("command", choices=("preserve-git", "preserve-copy", "record"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--new", default="")
    parser.add_argument("--data-root", default="")
    args = parser.parse_args(argv)

    root = Path(args.root)
    data_root = Path(args.data_root) if args.data_root else None
    try:
        if args.command == "preserve-git":
            record = preserve_git(root, args.ref, args.label, data_root=data_root)
        elif args.command == "preserve-copy":
            record = preserve_copy(root, Path(args.new), args.ref, data_root=data_root)
        else:
            written = record_tree(root, args.ref, data_root=data_root)
            print(f"{BATCH_PREFIX} ledger:{written}")
            return 0
    except Exception as exc:  # noqa: BLE001 - 保护动作绝不能让升级失败
        print(f"{BATCH_PREFIX} error:{exc.__class__.__name__}")
        return 0
    if record is None:
        print(f"{BATCH_PREFIX} none")
        return 0
    print(f"{BATCH_PREFIX} {record.get('batch_dir')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
