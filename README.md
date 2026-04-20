# G3KU

G3KU 是一个面向长期运行与维护场景的 AI Agent Runtime 仓库。它把命令行对话、Web CEO 工作台、异步任务运行时、工具与技能上下文装配、长期记忆、定时任务，以及中国渠道桥接整合到同一套系统里。

这个仓库不只是一个“聊天壳”或单轮对话 demo。它更偏向一个可持续运行、可调试、可扩展、可被后续维护者接手的 Agent 工程底座，适合做：

- CLI / Web 双入口的智能助理
- 可拆分为异步任务的复杂工作流
- 带工具、技能、记忆和调度能力的长期会话系统
- 需要接入中国 IM 渠道的 Agent 服务

## 仓库是干什么的

从能力边界上看，G3KU 主要解决的是“一个 Agent 系统如何从单轮聊天走向长期运行”：

- 在同步路径上，它提供 CLI 与 Web CEO shell，负责普通用户消息、会话状态和前门调度。
- 在异步路径上，它提供 main runtime / worker，用于执行后台任务、节点分发与任务树推进。
- 在能力装配上，它提供工具系统、技能系统、候选池和 hydration 机制，控制模型当前轮真正能看到和能调用的能力。
- 在状态与恢复上，它提供长期记忆、continuity sidecar、实际请求落盘和调试入口，方便排查缓存、上下文、压缩和恢复问题。
- 在渠道接入上，它提供 China bridge，把 Python 侧会话运行时和 Node 侧渠道宿主连接起来。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| CLI 与 Web 双入口 | 同时支持终端直接对话和 Web CEO 工作台 |
| CEO/frontdoor 与 main runtime 分层 | 前门负责任务识别与会话组织，主运行时负责异步任务树执行 |
| 工具与技能装配 | 支持 fixed builtin、candidate、hydrated tool、skill context 加载 |
| 长期记忆队列 | 使用 `memory/MEMORY.md`、`memory/queue.jsonl`、`memory/ops.jsonl` 管理长期记忆 |
| 定时任务 | 支持 `cron` 计划任务的新增、启停、手动运行 |
| China bridge | 通过 Python + Node 的桥接结构接入中国渠道 |
| 运维与可调试性 | 提供 `status`、memory doctor、request artifact、continuity sidecar 等调试入口 |

## 支持的系统环境

当前仓库至少应按下面的环境来准备：

- Python `3.11+`
  `pyproject.toml` 明确要求 `requires-python >= 3.11`，并标注了 `3.11`、`3.12`。
- Windows PowerShell、Linux、macOS
  仓库同时提供了 `g3ku.cmd`、`g3ku.ps1`、`g3ku.sh`，README 默认按这些环境来写。
- 现代浏览器
  使用 `g3ku web` 时需要浏览器访问 Web UI。
- Node.js `>=20`
  只在启用 China bridge 时需要；`subsystems/china_channels_host/package.json` 中要求 Node `>=20.0.0`。
- `pnpm` 或 `npm`
  只在启用 China bridge 时需要；默认模板里的 `chinaBridge.npmClient` 是 `pnpm`。

如果你只打算跑 CLI、Web、Worker 和 Memory 相关功能，Python 环境就足够。如果你要启用中国渠道，再额外准备 Node.js 与包管理器。

## 快速开始

### 1. 克隆仓库

```bash
git clone <your-repo-url>
cd G3KU
```

### 2. 创建虚拟环境并安装依赖

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

### 3. 生成项目本地配置

```bash
g3ku onboard --project
```

这一步会在当前仓库下生成：

- `.g3ku/config.json`
- `.g3ku/` 下的工作区基础目录

### 4. 修改配置文件

G3KU 当前把项目配置固定在仓库本地的 `.g3ku/config.json`。最简单的入门方式，就是在 `g3ku onboard --project` 生成的文件基础上修改下面这些字段：

- `models.catalog[*].providerModel`
- `models.catalog[*].apiKey`
- `models.catalog[*].apiBase`
- `models.roles.ceo`
- `models.roles.execution`
- `models.roles.inspection`
- `web.host`
- `web.port`
- `chinaBridge.enabled`

最常见的做法是先把模板中的占位值 `replace-me` 改成真实 API Key，再确认 `models.roles.*` 指向你启用的模型 key。

下面这段只是示意字段，不建议直接覆盖整个配置文件：

```json
{
  "models": {
    "catalog": [
      {
        "key": "gpt-5.4",
        "providerModel": "responses:gpt-5.4",
        "apiKey": "your-real-api-key",
        "apiBase": "https://api.openai.com/v1"
      }
    ],
    "roles": {
      "ceo": ["gpt-5.4"],
      "execution": ["gpt-5.4"],
      "inspection": ["gpt-5.4"]
    }
  },
  "web": {
    "host": "0.0.0.0",
    "port": 18790
  }
}
```

### 5. 先做一次基础自检

```bash
g3ku status
g3ku agent -m "你好，请介绍一下你自己"
```

## 运行方式

G3KU 最常见的运行入口是下面几种：

| 场景 | 命令 | 用途 |
| --- | --- | --- |
| 初始化配置 | `g3ku onboard --project` | 在当前仓库下生成本地配置和工作区 |
| 查看状态 | `g3ku status` | 检查配置、工作区、记忆与运行时关键路径 |
| CLI 直接对话 | `g3ku agent` | 进入交互式终端对话 |
| CLI 单次调用 | `g3ku agent -m "..."` | 快速验证模型链路和回复能力 |
| 启动 Web | `g3ku web` | 启动 Web CEO 工作台 |
| 启动 Worker | `g3ku worker` | 启动后台任务 worker |
| Memory 运维 | `g3ku memory ...` | 查看长期记忆当前状态、队列和健康情况 |
| Cron 运维 | `g3ku cron ...` | 新增、查看、执行计划任务 |
| China bridge 运维 | `g3ku china-bridge ...` | 排查中国渠道桥接状态 |
| 资源运维 | `g3ku resource ...` | 检查和重载根级 skills/tools 资源 |

## 常用命令与参数

### `g3ku agent`

直接与 Agent 对话。可以用于交互模式，也可以用于单条消息快速验证。

常用参数：

- `-m`, `--message`
  发送单条消息。省略时进入交互模式。
- `-s`, `--session`
  指定 session id，默认是 `cli:direct`。
- `--markdown`, `--no-markdown`
  是否将回复按 Markdown 渲染。
- `--logs`, `--no-logs`
  是否在聊天时显示运行时日志。
- `--debug`, `--no-debug`
  是否启用完整调试日志。

常用 demo：

```bash
g3ku agent
g3ku agent -m "你好，介绍一下当前仓库"
g3ku agent -m "请帮我总结 docs/architecture/README.md" -s cli:docs
g3ku agent -m "列出当前系统状态" --no-markdown --logs
g3ku agent -m "请用调试模式运行一次" --debug
```

### `g3ku web`

启动 Web CEO 工作台。

常用参数：

- `--host`
  Web 绑定地址，默认取 `.g3ku/config.json -> web.host`。
- `-p`, `--port`
  Web 端口，默认取 `.g3ku/config.json -> web.port`。
- `--reload`, `--no-reload`
  是否启用自动重载。
- `--debug`, `--no-debug`
  是否启用完整后端调试日志。

常用 demo：

```bash
g3ku web
g3ku web --host 127.0.0.1 --port 18790
g3ku web --reload --debug
```

### `g3ku worker`

启动后台任务 worker。这个命令本身没有常用参数，适合在单独终端里运行，用于任务执行和 Web/runtime 分离部署场景。

常用 demo：

```bash
g3ku worker
```

### `g3ku status`

查看当前 G3KU 状态，包括配置文件、工作区、记忆相关路径等关键入口。这个命令非常适合作为“启动前第一检”。

常用 demo：

```bash
g3ku status
```

### `g3ku memory`

管理长期记忆运行时。

常用子命令：

- `g3ku memory current`
  查看当前提交态记忆。
- `g3ku memory queue -n 20`
  查看队列中的记忆请求，`-n/--limit` 控制返回数量。
- `g3ku memory flush`
  触发一次队列处理。
- `g3ku memory doctor`
  做一次只读健康检查。

常用参数：

- `g3ku memory queue -n, --limit`
  控制展示的队列条数，默认 `20`。
- `g3ku memory doctor --now-iso`
  覆盖当前时间，适合做确定性排查。
- `g3ku memory doctor --stuck-after-seconds`
  把超过指定秒数仍在 `processing` 的队列头视为可疑卡住，默认 `300` 秒。

常用 demo：

```bash
g3ku memory current
g3ku memory queue -n 10
g3ku memory flush
g3ku memory doctor
g3ku memory doctor --stuck-after-seconds 600
```

### `g3ku cron`

管理计划任务。

常用子命令：

- `g3ku cron list`
- `g3ku cron add`
- `g3ku cron remove <job_id>`
- `g3ku cron enable <job_id>`
- `g3ku cron run <job_id>`

`g3ku cron add` 常用参数：

- `-n`, `--name`
  任务名称，必填。
- `-m`, `--message`
  发给 Agent 的消息，必填。
- `--stop-condition`
  循环任务的退出条件。
- `-e`, `--every`
  每隔 N 秒运行一次。
- `-c`, `--cron`
  Cron 表达式，例如 `0 9 * * *`。
- `--tz`
  时区，例如 `Asia/Shanghai`。
- `--at`
  只执行一次的时间点，ISO 格式。
- `-d`, `--deliver`
  是否将结果投递到渠道。
- `--to`
  投递目标。
- `--channel`
  投递渠道。

`g3ku cron run` 常用参数：

- `-f`, `--force`
  即使任务被禁用也强制执行。

常用 demo：

```bash
g3ku cron list
g3ku cron add -n "daily-report" -m "生成今天的日报" --cron "0 18 * * *" --tz Asia/Shanghai
g3ku cron add -n "reminder" -m "提醒我检查 memory 队列" --every 3600 --stop-condition "当队列连续三次为空时停止"
g3ku cron run <job_id>
g3ku cron run <job_id> --force
```

### `g3ku china-bridge`

管理中国渠道桥接子系统。

常用子命令：

- `g3ku china-bridge status`
- `g3ku china-bridge doctor`
- `g3ku china-bridge restart`

常用 demo：

```bash
g3ku china-bridge status
g3ku china-bridge doctor
g3ku china-bridge restart
```

启用 China bridge 前，请确认：

- 已安装 Node.js `>=20`
- 已准备好 `pnpm` 或 `npm`
- `.g3ku/config.json` 中 `chinaBridge` 段已按实际渠道填写

### `g3ku resource`

检查和重载根级资源目录中的 skills / tools。

常用子命令：

- `g3ku resource list`
- `g3ku resource reload`
- `g3ku resource validate`
- `g3ku resource status`

常用 demo：

```bash
g3ku resource list
g3ku resource validate
g3ku resource reload
```

## 推荐的运行顺序

如果你是第一次接手仓库，建议按下面顺序启动：

1. 创建虚拟环境并安装依赖。
2. 运行 `g3ku onboard --project` 生成 `.g3ku/config.json`。
3. 把 `.g3ku/config.json` 里的模型配置改成真实可用的 provider/model/apiKey。
4. 运行 `g3ku status` 做基础自检。
5. 运行 `g3ku agent -m "test"` 验证 CLI 对话路径。
6. 再运行 `g3ku web` 验证 Web UI。
7. 如果任务链路要单独部署，再运行 `g3ku worker`。
8. 如果启用了中国渠道，再运行 `g3ku china-bridge doctor`。

## 仓库目录速览

| 路径 | 作用 |
| --- | --- |
| `g3ku/` | Python 主应用，包含 CLI、runtime、web、config、provider、bridge 等代码 |
| `main/` | 异步任务运行时与任务树执行主线 |
| `docs/architecture/` | 维护者优先阅读的架构文档 |
| `docs/superpowers/` | 方案、计划、设计文档沉淀 |
| `skills/` | 项目级技能资源 |
| `tools/` | 工具资源定义 |
| `subsystems/china_channels_host/` | China bridge 的 Node 宿主 |
| `tests/` | 测试用例 |
| `.g3ku/` | 项目本地配置、运行时状态、日志、数据库、资源状态等 |
| `memory/` | 长期记忆快照、记忆队列和处理记录 |

## 给维护者的阅读入口

如果你要改代码，不要先随机看文件。建议从这些文档开始：

- [docs/architecture/README.md](docs/architecture/README.md)
- [docs/architecture/runtime-overview.md](docs/architecture/runtime-overview.md)
- [docs/architecture/tool-and-skill-system.md](docs/architecture/tool-and-skill-system.md)
- [docs/architecture/web-and-admin.md](docs/architecture/web-and-admin.md)
- [docs/architecture/config-and-models.md](docs/architecture/config-and-models.md)
- [docs/architecture/heartbeat-system.md](docs/architecture/heartbeat-system.md)
- [docs/architecture/china-channels.md](docs/architecture/china-channels.md)
- [docs/architecture/operations-and-maintenance.md](docs/architecture/operations-and-maintenance.md)

建议的阅读方式：

1. 先看整体架构入口。
2. 再按你要修改的子系统挑对应主题文档。
3. 最后再进入具体源文件。

## 开发与验证

安装开发依赖后，最小验证动作建议至少跑下面这些：

```bash
g3ku status
g3ku agent -m "test"
g3ku web
```

如果你要跑测试：

```bash
pytest
```

如果改动涉及中国渠道，再补一条：

```bash
g3ku china-bridge doctor
```

## 许可证

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。
