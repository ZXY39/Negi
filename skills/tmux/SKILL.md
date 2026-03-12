# tmux 技能

仅在需要交互式 TTY 时使用 tmux。对于长时间运行的非交互式任务，首选 `exec` 后台模式。

## 快速开始 (隔离的 socket, exec 工具)

```bash
SOCKET_DIR="${G3KU_TMUX_SOCKET_DIR:-${TMPDIR:-/tmp}/g3ku-tmux-sockets}"
mkdir -p "$SOCKET_DIR"
SOCKET="$SOCKET_DIR/g3ku.sock"
SESSION=g3ku-python

tmux -S "$SOCKET" new -d -s "$SESSION" -n shell
tmux -S "$SOCKET" send-keys -t "$SESSION":0.0 -- 'PYTHON_BASIC_REPL=1 python3 -q' Enter
tmux -S "$SOCKET" capture-pane -p -J -t "$SESSION":0.0 -S -200
```

启动会话后，始终打印监控命令：

```
监控方式：
  tmux -S "$SOCKET" attach -t "$SESSION"
  tmux -S "$SOCKET" capture-pane -p -J -t "$SESSION":0.0 -S -200
```

## Socket 约定

- 使用 `G3KU_TMUX_SOCKET_DIR` 环境变量。
- 默认 socket 路径：`"$G3KU_TMUX_SOCKET_DIR/g3ku.sock"`。

## 指定面板 (Pane) 和命名

- 目标格式：`session:window.pane` (默认为 `:0.0`)。
- 保持名称简短；避免使用空格。
- 检查：`tmux -S "$SOCKET" list-sessions`, `tmux -S "$SOCKET" list-panes -a`。

## 查找会话 (Session)

- 列出你 socket 上的会话：`{baseDir}/scripts/find-sessions.sh -S "$SOCKET"`。
- 扫描所有 socket：`{baseDir}/scripts/find-sessions.sh --all` (使用 `G3KU_TMUX_SOCKET_DIR`)。

## 安全地发送输入

- 首选字面量发送：`tmux -S "$SOCKET" send-keys -t target -l -- "$cmd"`。
- 控制键：`tmux -S "$SOCKET" send-keys -t target C-c`。

## 监视输出

- 捕获最近的历史记录：`tmux -S "$SOCKET" capture-pane -p -J -t target -S -200`。
- 等待提示符：`{baseDir}/scripts/wait-for-text.sh -t session:0.0 -p 'pattern'`。
- 可以使用附着 (attach)；使用 `Ctrl+b d` 脱离 (detach)。

## 派生进程

- 对于 Python REPL，设置 `PYTHON_BASIC_REPL=1` (非基本 REPL 会破坏 send-keys 流程)。

## Windows / WSL

- tmux 支持 macOS/Linux。在 Windows 上，请使用 WSL 并在 WSL 内部安装 tmux。
- 此技能仅限 `darwin`/`linux` 使用，且要求 `tmux` 在 PATH 路径中。

## 编排编码代理 (Codex, Claude Code)

tmux 非常适合并行运行多个编码代理：

```bash
SOCKET="${TMPDIR:-/tmp}/codex-army.sock"

# 创建多个会话
for i in 1 2 3 4 5; do
  tmux -S "$SOCKET" new-session -d -s "agent-$i"
done

# 在不同的工作目录启动代理
tmux -S "$SOCKET" send-keys -t agent-1 "cd /tmp/project1 && codex --yolo 'Fix bug X'" Enter
tmux -S "$SOCKET" send-keys -t agent-2 "cd /tmp/project2 && codex --yolo 'Fix bug Y'" Enter

# 轮询完成状态 (检查是否返回了提示符)
for sess in agent-1 agent-2; do
  if tmux -S "$SOCKET" capture-pane -p -t "$sess" -S -3 | grep -q "❯"; then
    echo "$sess: DONE"
  else
    echo "$sess: Running..."
  fi
done

# 从已完成的会话中获取完整输出
tmux -S "$SOCKET" capture-pane -p -t agent-1 -S -500
```

**提示：**
- 使用独立的 git worktree 进行并行修复（避免分支冲突）。
- 在全新的克隆中使用 codex 前，先运行 `pnpm install`。
- 检查 Shell 提示符（`❯` 或 `$`）来检测是否完成。
- Codex 需要 `--yolo` 或 `--full-auto` 才能进行非交互式修复。

## 清理

- 关闭会话：`tmux -S "$SOCKET" kill-session -t "$SESSION"`。
- 关闭一个 socket 上的所有会话：`tmux -S "$SOCKET" list-sessions -F '#{session_name}' | xargs -r -n1 tmux -S "$SOCKET" kill-session -t`。
- 彻底关闭私有服务器：`tmux -S "$SOCKET" kill-server`。

## 辅助脚本：wait-for-text.sh

`{baseDir}/scripts/wait-for-text.sh` 轮询面板以匹配正则表达式（或固定字符串），并设置超时。

```bash
{baseDir}/scripts/wait-for-text.sh -t session:0.0 -p 'pattern' [-F] [-T 20] [-i 0.5] [-l 2000]
```

- `-t`/`--target`: 面板目标 (必需)
- `-p`/`--pattern`: 要匹配的正则是 (必需)；添加 `-F` 使用固定字符串
- `-T`: 超时秒数 (整数, 默认 15)
- `-i`: 轮询间隔秒数 (默认 0.5)
- `-l`: 搜索的历史行数 (整数, 默认 1000)
