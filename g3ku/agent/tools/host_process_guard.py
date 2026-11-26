"""目标身份判定的宿主进程护栏。

护栏只服务于「别把 Negi 自己杀死」：能静态证明终止目标不是 Negi 的命令放行，
证明不了的（通配 / 无过滤 / 管道输入 / 动态表达式 / 执行汇 / 解析不确定）才拦。
它是 best-effort 的身份快照，不是 OS 隔离；脚本 / native / 嵌套执行汇维持现状。
"""

from __future__ import annotations

import os
import re
from typing import Any

_VERBS = {"stop-process", "spps", "taskkill", "killall", "pkill", "kill"}

_INERT = {
    "get-help", "help", "man", "get-command", "gcm", "select-string", "sls",
    "findstr", "grep", "rg", "ag", "ack", "get-content", "cat", "type", "more",
    "less", "write-output", "write-host", "write-verbose", "write-debug",
    "write-information", "echo", "out-string", "out-host", "format-table",
    "format-list", "format-wide", "measure-object", "select-object", "sort-object",
    "group-object", "where-object", "foreach-object", "compare-object",
    "get-process", "get-service", "test-path", "resolve-path", "get-item",
    "get-childitem", "ls", "dir", "pwd", "get-location",
}

_SINKS = {
    "powershell", "pwsh", "cmd", "bash", "sh", "zsh", "dash", "ksh", "iex",
    "invoke-expression", "python", "python3", "py", "node", "deno", "bun", "perl",
    "ruby", "sudo", "doas", "env", "nohup", "command", "exec", "builtin", "time",
    "nice", "ionice", "setsid", "stdbuf", "start", "start-process", "start-job",
    "invoke-command", "register-scheduledtask", "new-scheduledtaskaction",
    "schtasks", "wmic", "invoke-cimmethod", "xargs", "find", "watch", "busybox",
    "runuser", "su", "chroot",
}

_CODE_PATTERNS = (
    r"\b(?:os|signal)\.(?:kill|killpg|pthread_kill)\s*\(",
    r"\bterminateprocess(?:es)?\b",
    r"\.\s*(?:kill|terminate)\s*\(",
)


def _psutil() -> Any:
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    return psutil


def _scan(cmd: str) -> tuple[list[list[tuple[str, bool]]], bool]:
    """切出命令位置段与 token（text, quoted）；解析不确定返回 confident=False。"""
    segments: list[list[tuple[str, bool]]] = []
    tokens: list[tuple[str, bool]] = []
    cur: list[str] = []
    quoted = False
    quote: str | None = None
    confident = True
    i = 0
    n = len(cmd)

    def flush_token() -> None:
        nonlocal cur, quoted
        if cur:
            tokens.append(("".join(cur), quoted))
            cur = []
            quoted = False

    def flush_segment() -> None:
        nonlocal tokens
        flush_token()
        if tokens:
            segments.append(tokens)
            tokens = []

    while i < n:
        c = cmd[i]
        if quote:
            if c == quote:
                if quote == "'" and i + 1 < n and cmd[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                quote = None
                i += 1
                continue
            if quote == '"' and c == "`" and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 2
                continue
            if quote == '"' and c == "$" and i + 1 < n and cmd[i + 1] == "(":
                confident = False
                break
            cur.append(c)
            i += 1
            continue
        if c in "'\"":
            flush_token()
            quote = c
            quoted = True
            i += 1
            continue
        if c == "#":
            flush_token()
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if c == "`" or (c == "<" and i + 1 < n and cmd[i + 1] == "#"):
            confident = False
            break
        if c == "@" and i + 1 < n and cmd[i + 1] in "'\"":
            confident = False
            break
        if c in ";\n":
            flush_segment()
            i += 1
            continue
        if c in "&|":
            flush_segment()
            i += 2 if (i + 1 < n and cmd[i + 1] == c) else 1
            continue
        if c in "{}()":
            flush_segment()
            i += 1
            continue
        if c in " \t\r":
            flush_token()
            i += 1
            continue
        cur.append(c)
        i += 1
    if quote is not None:
        confident = False
    flush_segment()
    return segments, confident


def _norm_word(text: str) -> str:
    t = text.strip().strip("'\"")
    t = t.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if t.endswith(".exe"):
        t = t[:-4]
    return t


def _negi_pids(psutil: Any) -> set[int]:
    pids = {os.getpid()}
    try:
        for parent in psutil.Process(os.getpid()).parents():
            pids.add(parent.pid)
    except Exception:
        pass
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            cmdline = proc.info.get("cmdline") or []
            if "-m" in cmdline and "g3ku" in cmdline:
                pids.add(proc.info["pid"])
    except Exception:
        pass
    return pids


def _name_safe(name: str, negi: set[int], psutil: Any) -> bool:
    base = name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if not base:
        return False
    for proc in psutil.process_iter(["pid", "name"]):
        pname = (proc.info.get("name") or "").lower()
        if pname.endswith(".exe"):
            pname = pname[:-4]
        if pname == base and proc.info["pid"] in negi:
            return False
    return True


def _tree_has_negi(pid: int, negi: set[int], psutil: Any) -> bool:
    try:
        for child in psutil.Process(pid).children(recursive=True):
            if child.pid in negi:
                return True
    except Exception:
        return True
    return False


def _name_tree_has_negi(name: str, negi: set[int], psutil: Any) -> bool:
    base = name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    for proc in psutil.process_iter(["pid", "name"]):
        pname = (proc.info.get("name") or "").lower()
        if pname.endswith(".exe"):
            pname = pname[:-4]
        if pname == base and _tree_has_negi(proc.info["pid"], negi, psutil):
            return True
    return False


def _static_literal(tok: tuple[str, bool] | None) -> bool:
    if tok is None:
        return False
    text, _ = tok
    if not text or any(ch in text for ch in "*?$(),;|&"):
        return False
    return True


def _param_value(tokens: list[tuple[str, bool]], names: tuple[str, ...]) -> tuple[str, bool] | None:
    for idx, (text, quoted) in enumerate(tokens):
        tl = text.lower()
        if tl in names:
            return tokens[idx + 1] if idx + 1 < len(tokens) else None
        for nm in names:
            if tl.startswith(nm + ":"):
                return (text[len(nm) + 1:], quoted)
    return None


def _termination_safe(word: str, tokens: list[tuple[str, bool]], negi: set[int], psutil: Any) -> bool:
    args = tokens[1:]
    lows = [t.lower() for t, _ in args]
    if word == "kill":
        if any(lo in ("-l", "-L") for lo in lows):
            return True
        pids: list[str] = []
        sig: str | None = None
        i = 0
        while i < len(args):
            tl = args[i][0].lower()
            if tl in ("-s", "--signal"):
                if i + 1 >= len(args):
                    return False
                sig = args[i + 1][0]
                i += 2
                continue
            if tl.startswith("-") and tl[1:].isdigit():
                sig = tl[1:]
                i += 1
                continue
            if tl.startswith("-"):
                return False
            pids.append(args[i][0])
            i += 1
        if sig == "0":
            return True
        if not pids:
            return False
        for p in pids:
            if not p.isdigit() or int(p) <= 0 or int(p) in negi:
                return False
        return True
    if word in ("stop-process", "spps"):
        if any(lo == "-whatif:$false" for lo in lows):
            return False
        if any(lo in ("-whatif", "-whatif:$true") for lo in lows):
            return True
        if any(lo.startswith("-inputobject") for lo in lows):
            return False
        name = _param_value(args, ("-name",))
        pid = _param_value(args, ("-id",))
        if name is not None:
            return _static_literal(name) and _name_safe(name[0], negi, psutil)
        if pid is None or not _static_literal(pid):
            return False
        return pid[0].isdigit() and int(pid[0]) > 0 and int(pid[0]) not in negi
    if word == "taskkill":
        if any(lo == "/?" for lo in lows):
            return True
        if any(lo.startswith("/fi") for lo in lows):
            return False
        tree = any(lo == "/t" for lo in lows)
        im = _param_value(args, ("/im",))
        pid = _param_value(args, ("/pid",))
        if im is not None:
            if not _static_literal(im) or not _name_safe(im[0], negi, psutil):
                return False
            return not (tree and _name_tree_has_negi(im[0], negi, psutil))
        if pid is None or not _static_literal(pid):
            return False
        if not pid[0].isdigit() or int(pid[0]) <= 0 or int(pid[0]) in negi:
            return False
        return not (tree and _tree_has_negi(int(pid[0]), negi, psutil))
    if word == "killall":
        vals = [t for t, _ in args if not t.startswith("-")]
        if len(vals) != 1 or not _static_literal((vals[0], False)):
            return False
        return _name_safe(vals[0], negi, psutil)
    return False


def _unquoted_text(cmd: str) -> str:
    """引号/注释替换为空格、保留括号与代码标点，供代码形态检测。"""
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            if c == quote:
                if quote == "'" and i + 1 < n and cmd[i + 1] == "'":
                    out.append("  ")
                    i += 2
                    continue
                quote = None
                out.append(" ")
                i += 1
                continue
            if quote == '"' and c == "`" and i + 1 < n:
                out.append("  ")
                i += 2
                continue
            out.append(" ")
            i += 1
            continue
        if c in "'\"":
            quote = c
            out.append(" ")
            i += 1
            continue
        if c == "#":
            while i < n and cmd[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def should_block(command: str) -> bool:
    """粗筛命中后调用：True=拦（目标可能是 Negi 或无法静态证明），False=放行。"""
    cmd = str(command or "").strip().lower()
    if not cmd:
        return False
    segments, confident = _scan(cmd)
    if not confident:
        return True
    if any(re.search(p, _unquoted_text(cmd)) for p in _CODE_PATTERNS):
        return True
    sink_present = any(seg and _norm_word(seg[0][0]) in _SINKS for seg in segments)
    psutil = _psutil()
    negi: set[int] | None = None
    for seg in segments:
        if not seg:
            continue
        word = _norm_word(seg[0][0])
        if word in _VERBS:
            if psutil is None:
                return True
            if negi is None:
                negi = _negi_pids(psutil)
            if not _termination_safe(word, seg, negi, psutil):
                return True
        elif word in _SINKS:
            return True
        elif word in _INERT:
            continue
        else:
            if any(_norm_word(t) in _VERBS and not q for t, q in seg[1:]):
                return True
            if sink_present and any(_norm_word(t) in _VERBS for t, q in seg[1:]):
                return True
    return False
