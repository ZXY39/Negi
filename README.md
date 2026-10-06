<p align="center">
  <img src="assets/banner.png" alt="G3KU-Agent" width="100%">
</p>

# Negi

目录：[项目介绍](#项目介绍) | [1. 配置环境](#1-配置环境) | [2. 如何启动项目](#2-如何启动项目) | [3. 配置模型](#3-配置模型) | [4. 通信与对外接口（可选）](#4-通信与对外接口可选) | [5. 功能介绍](#5-功能介绍) | [6. 面向开发者和-agent-的补充说明](#6-面向开发者和-agent-的补充说明) | [部署（Docker / Compose）](#部署docker--compose) | [7. 致谢与参考](#7-致谢与参考) | [8. 许可证](#8-许可证)

## 项目介绍

[![Demo](assets/cover.png)](https://github.com/user-attachments/assets/e6ba8afb-d88e-44db-b049-122c86cdaad3)

**Negi 是一套面向复杂工作流的 Harness。一个能自主进化、长期运行、扩展能力、对外通信，同时支持 Web 管理界面的智能工作系统。**

它让 Agent 真正具备了可长期使用的能力：能记住重要信息、能按需调用工具、能拆解复杂任务、能在长会话里保持稳定、能在高并发下运行，也能在真实环境中把风险控制住。

这个项目的特点，可以从下面 7 个方面来理解：

1. 🧠 **自进化体系**
   长期记忆、用户偏好与经验由一条独立的记忆 Agent 车道沉淀进会话，不是每次都从零开始理解你。
2. 🧩  **渐进式加载**
   33 个工具资源与 15 个自带 Skill 不一次性给模型；模型按名单点名 `load_tool_context` / `load_skill_context`，下一回合才成为可调用项。
3. 👥  **多 Agent 架构**
   复杂任务派生成节点树，执行节点与检验节点分开；检验节点可以打回执行节点，父节点按阶段预算推进而不是无限对话。
4. 🗺️  **混合执行模式**
   主会话（前门）负责快速响应与规整，后台任务运行时负责分阶段推进、消息分发、结果验收，两条车道共享同一份配置与资源。
5. 🗜️  **上下文自持**
   只有两条收缩边界（回合内 token 压缩、阶段收口压缩），超长工具输出外置成 `artifact:` 引用按需回读，静态契约钉进请求头部以保住前缀缓存。
6. ⚡  **准入与调速**
   节点回合闸按事件循环延迟、写队列、上游限流与磁盘水位实时决定放行多少并发；模型链按槽位轮换，429 与断流会真的换槽而不是原槽重试。
7. 🛡️  **安全机制**
   项目口令派生主密钥，密钥类字段落盘即加密；工具与技能按岗位角色控可见性，命令执行走一次性批准 / 白名单 / 拒绝三态审批。

### 这套系统区别于普通 Agent 应用的地方

一个"前端页面 + 聊天后端 + 工具调用"的应用不需要处理下面任何一条。Negi 需要，所以它们都是实装的功能而不是设计意图：

1. **岗位模型链与容灾**：主 / 执行 / 检验 / 记忆四条有序链，链上顺序就是 fallback 优先级；执行与检验的链位可以是负载均衡组，由准入层为每个节点绑定具体成员，改链在迭代/重试边界生效，不中途热切已在飞的请求。契约见 `docs/architecture/config-and-models.md`。
2. **节点回合闸**：执行器存在本身就是成本。闸位由监控按节拍发布的余量目标决定，积压轴或上游限流越过 critical 才逐格退让，磁盘紧急态是唯一能一次踩到 0 的硬闸。界面上的「限」读的是活闸位。
3. **前缀缓存工程**：静态契约块钉进请求头部、追加内容先摘再拼、裁掉在飞轮会让整轮重复派生——这些都有实测判据与排障入口，见 `docs/architecture/context-and-cache-troubleshooting.md`。
4. **阶段与验收语义**：阶段由模型点名推进（`submit_next_stage`）、收口才有总结；验收节点判决读的是解析后的结构而非模型自述；打回正文带未完成清单。见 `docs/architecture/main-task-runtime.md`。
5. **停机排水与启动恢复**：优雅退出先排空在飞回合，落暂停的任务在重启后由恢复分类器按"这一轮有没有已发生的副作用"决定续跑、重放还是交回模型判断。
6. **渠道解耦但身份不降级**：平台协议归桥接层，一个桥一个身份一份会话命名空间；唯一内置例外是官方 QQ 适配器，它同样只经 `/api/v1` 消费自家能力。
7. **把 Negi 自己接给别的 Agent 用**：同一套契约之上另有 OpenAI 兼容端点与 MCP stdio 网关，见 `docs/architecture/agent-gateway.md`。
8. **本机语音输入不外传**：composer 麦克风与渠道入站语音走本地 whisper.cpp 转写，音频不出机器。见 `docs/architecture/speech-to-text.md`。
9. **观测面是运维入口而不是彩蛋**：8 个导航视图 + token 统计 + 15 秒采样的性能面板 + 节点详情完整上下文，排障按锚点走，见 `docs/architecture/operations-and-maintenance.md`。


## 1. 配置环境

### 支持的系统环境

建议至少准备下面这些环境：

- Python `3.11` 或更高版本（一行安装按仓库的 `.python-version` 装，当前是 `3.12`）
- Windows PowerShell、Linux 或 macOS
- 现代浏览器，用于访问 Web 界面

对话主流程只需要 Python 环境。接入 QQ 官方机器人不需要额外装依赖——`qq-botpy` 已在核心依赖里，且只有 `g3ku/qq_official/bridge.py` 一个模块 import 它；接入其他平台由独立桥接进程通过 External Agent API 完成，见「4. 通信与对外接口（可选）」。

### 一行指令安装（新设备）

新设备上输入一行指令即可完成安装并启动：脚本自己装 uv、装 Python、拉代码、按 `uv.lock` 建环境，最后拉起 Web。

Windows PowerShell:

```powershell
iwr https://raw.githubusercontent.com/ZXY39/G3KU-Agent/v1.0.15/install.ps1 | iex
```

Linux / macOS:

```bash
curl -LsSf https://raw.githubusercontent.com/ZXY39/G3KU-Agent/v1.0.15/install.sh | bash
```

默认装到 `~/G3KU-Agent`，不需要预装 Python；没有 git 时改走源码包下载（需要 curl 或 wget，以及 unzip）。

可选参数：

- `-Dir PATH` / `--dir PATH`：换安装目录
- `-NoStart` / `--no-start`：只准备环境，不启动 Web
- `-Ref TAG` / `--ref TAG`：换要安装的版本，用于回滚或试装
- `-Upgrade` / `--upgrade`：把已装好的设备更新到 `-Ref` 指定的版本

`raw.githubusercontent.com` 解析不了时（国内常见），换一条通道取同一个脚本即可，仓库地址与参数都不变：

```powershell
# 镜像代理前缀（把原 URL 整个拼在后面）
iwr https://cdn.jsdelivr.net/gh/ZXY39/G3KU-Agent@v1.0.15/install.ps1 | iex
iwr https://ghfast.top/https://raw.githubusercontent.com/ZXY39/G3KU-Agent/v1.0.15/install.ps1 | iex
```

自 `v1.0.2` 起每个发布版本都带 release 资产，可绕开 raw 域名：`https://github.com/ZXY39/G3KU-Agent/releases/download/v1.0.15/install.ps1`。第三方代理等于把"执行什么代码"交给它，稳妥做法是先下载再看内容：首行应为 `#requires -Version 5.1` / `#!/usr/bin/env bash`，文件里钉住的版本应与 URL 里的 tag 一致；字节数以该 release 页面列出的资产大小为准。

国内网络取 PyPI 或 Python 发行包慢时，给 uv 传镜像环境变量即可，脚本不另设开关：

```bash
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
export UV_PYTHON_INSTALL_MIRROR=<可用的 python-build-standalone 镜像>
```

两条边界要说清楚：

- 安装完成的终点是**项目口令设置页**。首次启动会在浏览器里要求创建项目口令，设完才进入配置页面。
- 安装得到的是 git 跟踪集：项目自带的 skills/tools 在里面，本地通过市场安装的 skills、`externaltools/` 与 `.g3ku/` 不在里面，装完按需在 Web 里重新添加。

### 升级与版本检查

默认安装位置：Windows `%USERPROFILE%\G3KU-Agent`，Linux / macOS `~/G3KU-Agent`。环境（`.venv/`）和数据（`.g3ku/`）都在这个目录里，升级不碰它们。

- 有没有新版：在项目目录里跑 `g3ku status`，最后一行 `Release:` 报当前版本与远端最新标签。离线、没有 git 或远端不是本仓库时这一行直接不出现，不会给你假的"已是最新"。
- 自动检查：Web 服务运行期间每 5 小时查一次，启动时也会查一次；间隔与开关在 `config.json` 的 `update_check`（`enabled` / `interval_hours`）。检查只读远端标签列表，不外发任何本地信息；项目还锁着时不检查也不提醒。
- 有新版本时：侧栏「设置」按钮左上角出现红点，`g3ku` 启动的命令行也会提示一行。点进设置能看到「当前版本 · 最新标签 · 检查于」，可以手动「检查更新」，也可以点「重启并更新」——它会先暂停正在进行的对话与任务，更新完成后自动重启服务。代码不会被自动替换，这一步一定要你点。
- 升级：`.\install.ps1 -Upgrade`（Linux / macOS `./install.sh --upgrade`）。默认升到脚本里钉住的 ref，要指定版本加 `-Ref v1.0.15` / `--ref v1.0.15`。
- 过渡一次：`v1.0.1` 之前的安装不认识 `-Upgrade`，先在该目录跑一次 `git pull`（或删目录重装）拿到新脚本，此后都走 `-Upgrade`。
- 改过仓库自带文件时 `-Upgrade` 拒绝执行，先提交或丢弃；你新装的 skill、桥接产物这类未跟踪文件不算改动，不影响升级。
- 「重启并更新」的全过程写在 `.g3ku/logs/update-apply.log` 里，安装脚本的进度会实时续写进去；超过 30 秒没有新输出时会另落一行"仍在跑"，慢网络下你能看出它是在等还是在死。不会再弹一个空白的命令行窗口。
- 已知不做：源码包方式的升级只覆盖新版带来的文件，上一版里被删掉的不会回收；在意就用 git 安装，或删目录重装。

下面的「环境配置步骤」是手动路径，供开发者或想自己控制环境的人使用。

### 环境配置步骤

下面是手动路径，适合开发者或要指定 fork / 自定义目录的场景。用上面「一行指令安装」的用户可以直接跳到「如何启动项目」。

1. 克隆项目并进入仓库目录。

```bash
git clone <your-repo-url>
cd G3KU-Agent
```

2. 普通用户通常不需要手动创建虚拟环境。

推荐直接使用仓库提供的一键启动脚本。启动脚本会自动：

- 创建或复用本地 `.venv`
- 在缺少运行时依赖时自动安装项目依赖
- 自动生成缺失的基础配置
- 启动 Web，并在非 reload 模式下自动托管 worker

如果你只是正常使用项目，可以跳过下面的手动依赖安装步骤，直接看下一节“如何启动项目”。

3. 如果你是开发者，或你想手动控制环境，再按下面方式创建虚拟环境并安装依赖。

Windows PowerShell:

```powershell
# 首选：uv 按锁文件同步（仓库已用 uv.lock 锁定依赖）
uv sync --frozen --extra dev

# 或手动 pip 环境（bootstrap 检测到 pip 时同样支持）
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Linux / macOS:

```bash
# 首选：uv 按锁文件同步
uv sync --frozen --extra dev

# 或手动 pip 环境
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

4. 如果你是开发者，且希望提前显式生成项目本地配置，也可以手动执行：

```bash
g3ku onboard --project
```

执行后，当前仓库下会生成项目自己的本地运行目录和配置文件，最重要的是：

- `.g3ku/config.json`

后续模型、Web、通信、运行时等配置都会围绕这个文件和前端配置页面展开。

## 2. 如何启动项目

### 默认启动方式：一键启动脚本 `start-g3ku`

对普通用户来说，推荐直接使用仓库根目录下的一键启动脚本。它会自动准备运行环境，并拉起你进入模型配置和通信配置最需要的 Web 界面。

Windows PowerShell:

```powershell
.\start-g3ku.ps1
```

Linux / macOS:

```bash
./start-g3ku.sh
```

如果你的系统第一次 clone 下来还没有执行权限，可以先运行：

```bash
chmod +x ./start-g3ku.sh
```

或者临时这样执行：

```bash
sh ./start-g3ku.sh
```

启动脚本默认会完成这些事情：

- 清理当前仓库下已经存在的 Web / worker 托管进程
- 检查端口占用
- 自动创建或复用 `.venv`
- 自动安装运行项目所需依赖
- 启动 Web
- 在非 reload 模式下自动让 Web 托管 worker

默认情况下，Web 会按项目配置启动。模板配置里的默认端口是 `18790`，通常可以直接在浏览器打开：

```text
http://127.0.0.1:18790
```

常用参数：

- Windows: `-BindHost`
  指定 Web 监听地址
- Windows: `-Port`
  指定 Web 端口
- Windows: `-OpenBrowser`
  启动后自动打开浏览器
- Windows: `-PromptLog`
  开启 prompt 日志
- Windows: `-Reload`
  开发时启用自动重载
- Windows: `-KeepWorker`
  Web 退出时保留托管 worker
- Linux / macOS: `--host`
  指定 Web 监听地址
- Linux / macOS: `--port`
  指定 Web 端口
- Linux / macOS: `--open-browser`
  启动后自动打开浏览器
- Linux / macOS: `--prompt-log`
  开启 prompt 日志
- Linux / macOS: `--reload`
  开发时启用自动重载
- Linux / macOS: `--keep-worker`
  Web 退出时保留托管 worker

快速示例：

Windows:

```powershell
.\start-g3ku.ps1 -OpenBrowser
.\start-g3ku.ps1 -BindHost 127.0.0.1 -Port 18790
.\start-g3ku.ps1 -Reload
```

Linux / macOS:

```bash
./start-g3ku.sh --open-browser
./start-g3ku.sh --host 127.0.0.1 --port 18790
./start-g3ku.sh --reload
```

## 3. 配置模型

模型配置推荐直接在前端完成，不需要先手动编辑大量 JSON 文件。

### 基本操作流程

1. 启动 Web 后，进入“模型配置”页面。
2. 先点击“添加模型”。
3. 新建好模型配置后，保存模型。
4. 点击右上角编辑模型链，再把左侧模型拖动到右侧对应 Agent 的模型链中。
5. 完成后点击“保存模型链”。

为了实现对话功能，至少需要先给主 Agent 配置一个模型。但是为了确保所有功能正常运行，需要为：

- 执行Agent
- 检验Agent
- 记忆Agent

配置各自的模型链。

### 模型链是用来容灾和分摊的

- 一条角色链是**有序列表**，顺序就是 fallback 优先级：只有链首失败，请求才前进到下一个模型。
- 执行与检验的链位可以放**负载均衡组**，组内成员平级、由准入层为每个节点绑定具体成员；主 Agent 与记忆 Agent 不支持组（前者有会话固定模型与前缀缓存键约束，后者有固定单并发）。
- 改链不需要重启：一次配置保存就打到活着的运行时，但只作用于边界处重建的下一次请求，不会把一个已经在飞的 provider 请求中途换掉。
- 单个会话也可以把模型固定成某一个（composer 的模型面板），被固定的模型被删除或停用时自动回退模型链，不会带着不可用模型继续发。

### 新建模型时怎么选协议

添加模型时“协议”默认选择 `OpenAI Chat`。当前只支持两种 OpenAI 兼容协议：

- `OpenAI Chat`（`/chat/completions`）
- `OpenAI Responses`（`/responses`）

协议由 provider 模板决定，切换协议等于换模板而不是改一个独立字段；模板同时决定可填参数的字段集合。`OpenAI Responses` 的请求体只带各家 `/responses` 共同支持的字段，扩展字段（如 `text.*`、`instructions`）会被部分后端整单拒绝——保存时的连接探测会先发一次真实形状的推理，字段级不兼容在保存前就暴露。遇到这类拒绝就改用 `OpenAI Chat`。

请求地址填写 API root 或完整端点（如以 `/chat/completions` 结尾）均可，系统会自动归一为 API root；切换协议时已填写的请求地址、Apikey 等内容会保留。然后修改默认模板后保存。

### JSON 配置里要填什么

在模型详情页里，你会看到 JSON 配置区域。常见需要填写的字段包括：

- `api_key`
- `base_url`
- `default_model`

如果你的服务还需要额外头信息或参数，也可以继续补充在对应 JSON 中。

### 图片多模态怎么开

如果某个模型本身支持多模态识图，保存该模型后，打开它的详情页，可以勾选：

- `是否为图像多模态`

启用后，Agent 才会具备更完整的识图能力。没有开启时，系统不会把图片相关能力按多模态路线开放给该模型。

### 长期记忆要不要额外配模型

不需要额外的检索模型。长期记忆由一条独立的记忆车道维护：写入走 `memory_write` / `memory_note` / `memory_delete` 这组工具，落库前由记忆 Agent 做校验与批量应用，审阅面和队列在「记忆」页；读回的是会话上下文本身。系统内没有向量库、Embedding 或 Rerank 配置项，你只需要给「记忆 Agent」配一条模型链。

超长工具输出与历史收缩由上下文机制自己处理（见「5. 功能介绍」），不依赖检索模型。

## 4. 通信与对外接口（可选）

Negi 的对外面有三条车道，按需选：

```
① IM 平台 ⇄ 内置官方 QQ 适配器（进程内，Web「外部」页配置）
② IM 平台 ⇄ 独立桥接进程（第三方协议） ⇄ External Agent API（/api/v1）
③ 其他 Agent / 客户端 ⇄ OpenAI 兼容端点、MCP stdio 网关 ⇄ 同一套 /api/v1 契约
```

判别准则：**换一个 IM 平台就要改的代码不属于核心**。平台协议、消息分段、频控、触发词识别全部在桥接层。

### ① 内置官方 QQ 适配器

`g3ku/qq_official/` 是唯一内置渠道桥，以 in-process 桥身份消费自家 `/api/v1`，QQ 协议收敛在这一个模块内。

- 配置：Web「外部」页的官方 QQ 机器人面板，按账号列行。配置段是 `config.json` 的 `qqBot`，总开关 `enabled` 加 `accounts`（以 AppID 为键，每行 `appSecret / sandbox / enabled / label`）。`appSecret` 与桥接 token 同走加密覆盖层。
- 多账号：一个 QQ 号 = 一个 AppID = 一条桥身份 = 一份独立的会话命名空间与 token，号与号之间不共享句柄；面板按号给服务态，单号异常不阻断其余号建桥。
- 能力：图片/文件入站与出站、入站语音就地本地转写、主动提醒（heartbeat / cron / 任务终态）。
- 平台约束要提前知道：主动消息受 QQ 开放平台的额度与回复时间窗限制，网页在渠道会话里发起的回合同样受它约束——表现为"网页有完整回复、QQ 端什么都没有"。加第二个号时旧会话键不迁移，指向旧会话的 cron / 心跳目标要改指新会话键。
- 契约与排障：`docs/architecture/external-agent-api.md`「内置官方 QQ 适配器」。

### ② 外部桥接（其他平台）

Negi 本体只暴露一套渠道无关的 headless agent API，平台协议由独立桥接应用承担：

- 外部桥接把平台消息推进 Negi 会话，订阅流式事件（增量 / 里程碑 / 最终回复），把回复发回平台；
- heartbeat 提醒、cron 定时、任务终态等主动推送经 `outbound.created` 事件送达桥接，桥 ack 才销账；
- 每个桥一个 `bridge_id` + token，会话命名空间互相隔离，可多桥并发。

启用与发 token：

```json
{
  "externalApi": {
    "enabled": true,
    "tokens": {
      "my-bridge": { "token": "<随机长串>", "label": "我的桥", "enabled": true }
    }
  }
}
```

token 明文保存时会自动剥离进加密覆盖层（与模型密钥同一机制）。启动你的桥接应用，指向 Negi 的 Web 地址并携带该 token（`Authorization: Bearer <token>`）。

仓库自带参考实现：`bridges/qq-onebot/`（QQ / OneBot 11，如 NapCat），配置与运行方式见其 `README.md`。

### ③ 把 Negi 接给别的 Agent 用

同一套契约之上另有两个开箱即用的对接面，都随 `externalApi.enabled` 打开：

- **OpenAI 兼容端点** `POST /api/v1/chat/completions`：给任何只会调 OpenAI 协议的客户端用，含会话映射、等待/超时与流式语义。
- **MCP stdio 网关** `g3ku mcp serve`：把 Negi 的能力作为 MCP 工具暴露给本机 Agent；`g3ku mcp check` 自检。

契约详见 `docs/architecture/agent-gateway.md`。

### 了解更多

- API 契约（端点、回合终态不变量、事件映射、出站路由）：`docs/architecture/external-agent-api.md`
- 旧版内置渠道子系统已移除；存量 `china:*` 会话转录在 Web 目录中保持只读可见。

对普通用户来说，可以把它理解成：

**通信配置就是把 Negi 从“本地可用”扩展到“能在外部平台和你聊天”，以及“能被别的东西调用”——Negi 本体保持渠道无关。**

## 5. 功能介绍

### 界面在哪看什么

侧栏 8 个视图：**会话**（主对话与阶段轨道）、**任务**（节点树、分发状态、验收与打回、性能条）、**Skill**、**Tool**（含权限与审批设置）、**记忆**（记忆队列与审阅面）、**模型**（模型链与协议）、**外部**（渠道与桥接）、**日志**（审计与运行日志，带未读红点）。侧栏底部的「设置」里是版本信息与「重启并更新」。

### 最容易上手的三类用法

1. **直接问 Agent“你能做什么？有哪些技能和工具？”**
   这是最快的入门方式之一。你可以直接让 Agent 列出当前可用的能力范围，快速了解它现在能处理哪些任务、能调用哪些技能、又有哪些工具可以配合使用。
2. **在 Skill 管理和 Tool 管理页面里自定义管理能力**
   你可以在前端页面里按需启用、停用、查看和调整 Skill 与 Tool，让系统能力更贴近自己的场景。资源目录热重载默认开着（轮询 + 去抖），改一个 `resource.yaml` 或 `SKILL.md` 通常分钟级内就打到活着的运行时，不需要重启。
3. **把一件事作为后台任务派出去**
   会话里直接说"建一个异步任务做 X"，执行节点与检验节点分开推进，任务页能看到每个节点在哪个阶段、派了什么工具、验收判决是什么。

### 能力面（开箱即有）

- **内置工具**：`tools/` 下 33 个资源，按类分组是文件读写与补丁 7 个、命令执行 1 个、网页抓取 1 个、外置内容回读 4 个、长期记忆 4 个、任务与性能观测 11 个、渐进式加载与 Skill 安装 3 个、定时任务 1 个、模型配置 1 个。另有若干固定内置执行器（阶段收口、最终结果交付、子节点派生、静默、消息分发）不经资源面下发。
- **自带 Skill**：随仓库带 15 个（架构维护、桥联接入门、Skill/Tool 的创建与修复、联网检索、GitHub、tmux、任务复盘等）；更多 Skill 可以在会话里让 Agent 从市场安装，或在 Skill 页管理。
- **浏览器页面操作**：由自带的 `web-access` Skill 经 CDP 直连你日常使用的 Chrome（天然带登录态）。它需要你在本机 Chrome 勾选 remote debugging、并装 Node.js 22+，不是内置工具，也不需要一个独立浏览器进程。
- **联网访问与定时任务**：`web_fetch` 是不起浏览器的抓取车道；周期任务由 `cron` 工具或 `g3ku cron` 命令建立，触发时以内部回合唤醒目标会话，界面上按普通活跃回合展示。
- **语音输入**：composer 的麦克风按钮默认就在，权限默认开，但**不会**因为默认值就下载任何东西——第一次点击才拉取官方 whisper.cpp 二进制与模型（约 157 MB），之后全本地转写。渠道入站语音同样就地转写，转写文本进上下文、原始语音画成可回放的语音条。

### 上下文与长会话

- 历史只在两条边界收缩：回合内的 token 压缩、阶段收口时的阶段压缩；其余一律追加，因此前缀稳定、缓存能命中。
- 超长工具输出不进上下文，落成 `artifact:` 引用，模型想看时按 `content_describe` / `content_open` / `content_search` 回读。
- 静态契约（候选名单、执行策略、临时目录口径等）钉进请求头部，避免每回合重印。
- 手动压缩在 composer 的上下文指示块上：按住不走满就不会触发。压缩在途时有入站闸门，不会把新到的消息挤掉。

### 性能与稳定性

- 节点回合闸按积压、上游限流、内存余量、磁盘水位实时决定放行量，界面上的「限」读的是活闸位。
- 模型链按槽位轮换，429 与断流会换槽；主对话的 token 统计、任务页的性能条、`perf_inspect` 工具的 15 秒采样读数构成同一条观测线。
- 优雅退出先排空在飞回合再停，落回暂停的任务在重启后由恢复分类器逐个判断是否已发生副作用，不靠重放整轮。

### 安全

- 首次启动设置项目口令，主密钥由它派生；模型密钥、桥接 token、QQ AppSecret 这类字段明文保存即剥离进加密覆盖层，落盘只留占位。锁定状态下写接口返回 423。
- 工具与 Skill 按岗位角色（主 / 执行 / 检验）控可见性，Tool 页可直接编辑每个动作的角色；没有"默认全放行"的兜底。
- 命令执行走审批：一次性批准 / 加入白名单 / 拒绝，等待时长可调；CEO 监管模式下批量动作先进审阅面。删除类动作强制预览再确认。
- 磁盘水位越线时，节点回合闸直接踩到 0——它是唯一能一次踩死的硬闸，其余压力轴都只能逐格退让。

### 如何新增 Skill 和 Tool

如果你想给当前项目新增能力，通常不需要先手动拷目录、改很多配置，最简单的方式就是**直接在会话里把 GitHub 地址、子目录地址、现成 `SKILL.md`，或者你的需求说明贴给 Agent**。

你可以直接这样说：

- `把这个 GitHub skill 接入当前项目：<url>`
- `参考这个仓库，给项目新增一个用于 XXX 的 tool：<url>`
- `帮我做一个 skill，用来规范 XXX 工作流`
- `把这个 CLI / API / 脚本封装成 Negi tool，要求支持 XXX`

一般情况下，Agent 会按资源类型自动处理：

- `skill`
  优先导入或创建到 `skills/`
- `tool`
  优先在 `tools/` 下注册；如果是第三方项目，通常会按项目约定接入到 `externaltools/`

为了让 Agent 一次做对，建议你顺手补充这些信息：

- 这个能力是给谁用的
- 想解决什么问题
- 输入和输出大概是什么
- 依赖哪个仓库、脚本、CLI 或 API
- 是否需要额外环境变量、密钥或系统依赖

如果你只是想快速开始，很多时候一句话加一个地址就够了，例如：

```text
把这个 GitHub skill 接入当前 Negi：<url>
```

```text
参考这个项目，帮我新增一个 Negi tool：<url>
```

完成后，你还可以在前端的 Skill 管理和 Tool 管理页面里继续查看、启用、停用或微调这些能力。

## 6. 面向开发者和 Agent 的补充说明

这一节主要给二次开发者、维护者，以及接手本仓库的 Agent 使用。

### 项目文档位置

架构文档在 `docs/architecture/`，入口是 `docs/architecture/README.md`（含阅读顺序、主题指南、排障入口）。按你要改的东西选：

| 文档 | 管什么 |
| --- | --- |
| `runtime-overview.md` | 会话生命周期、前门与运行时流、工具执行流、节点模型路由 |
| `main-task-runtime.md` | 任务侧：消息分发轮与屏障、验收交接与派生评审、节点暂停/恢复/取消、停机暂停与启动自动恢复 |
| `context-and-cache-troubleshooting.md` | 缓存未命中、上下文收缩与续跑、请求体留档取证 |
| `heartbeat-system.md` | 心跳回合、终态/停滞/分发错误唤醒、心跳与内联工具提醒的边界 |
| `tool-and-skill-system.md` | 工具/技能四概念、固定内置契约、候选池、RBAC 与治理 |
| `tool-hydration-and-callable-chain.md` | 一次 `load_tool_context` 如何变成下一回合可调用项、参数错误车道、通用超时、阶段门控 |
| `web-and-admin.md` | WebSocket 契约、前后端职责边界、运维可见的 UI 行为 |
| `config-and-models.md` | 配置真相源、运行时刷新规则、角色到模型的解析与绑定 |
| `external-agent-api.md` | `/api/v1` 鉴权、外部会话注册表、回合终态不变量、事件映射、出站路由、内置官方 QQ 适配器 |
| `agent-gateway.md` | OpenAI 兼容端点与 MCP stdio 网关 |
| `speech-to-text.md` | 本地语音转写：whisper.cpp 子进程、音频规格、二进制与模型供给 |
| `operations-and-maintenance.md` | 启动流程、排障顺序、高风险改动类型、Docker 部署 |

### 给 Agent 的建议阅读顺序

如果是仓库内 Agent 接手任务，建议按下面顺序建立上下文：

1. 先看 `AGENTS.md`
2. 再看 `docs/architecture/README.md`
3. 然后按任务涉及范围继续读对应架构文档

### 开发工作流

- lint：`scripts/lint.sh`（Linux / macOS）或 `scripts/lint.ps1`（Windows），也可以直接 `python -m ruff check .`（规则见 `pyproject.toml` 的 ruff 配置）
- pre-commit：仓库自带 `.pre-commit-config.yaml`，执行 `pre-commit install` 后提交阶段自动运行 lint 类检查
- 冒烟测试：`python -m pytest tests/resources/test_resource_runtime_smoke.py -q`
- 提交门禁是 **lint 通过 + 与本次改动相关的测试通过 + 冒烟子集通过**。全量 `python -m pytest` 有一批存量红（含跨文件环境相互影响的用例），别把"全量绿"当成前提，也别拿它当结论
- 前端 JS 测试在 pytest 之外，用 `node --test tests/resources/*.test.js`
- 新增测试直接放 `tests/` 下，该目录默认被跟踪
- 架构文档有结构自检：`python scripts/check_architecture_docs.py`（叶子章节字节上限、索引登记）

### 其他专业启动方式

除了默认的一键启动脚本，这个项目也支持几种更偏开发、验证和排障的手动启动路径。

### 手动环境准备

如果你想完全手动地理解启动链路，可以先自己准备虚拟环境：

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

如果你还想提前生成本地配置：

```bash
g3ku onboard --project
```

### 手动启动与排障方式

- CLI 对话：

```bash
g3ku agent
```

- CLI 单条消息测试：

```bash
g3ku agent -m "你好，介绍一下你自己"
```

- 手动启动 Web：

```bash
g3ku web
```

- 手动启动 Web 并指定地址端口：

```bash
g3ku web --host 127.0.0.1 --port 18790
```

- 后台任务 Worker：Web 在非 reload 模式下自己托管；只有你开了 `--reload`（`g3ku web --reload`）或想让 worker 独立跑，才需要单独起：

```bash
g3ku worker
```

- 状态检查：

```bash
g3ku status
```

- 其余命令组（各有 `--help`）：`g3ku cron`、`g3ku memory`、`g3ku provider`、`g3ku resource`、`g3ku external`、`g3ku stt`、`g3ku mcp`。

### 对开发者的简单理解

从工程结构上看：

- `g3ku/` 是主应用代码：CLI、Web、前门运行时、配置与安全、对外 API、`qq_official/`（唯一内置渠道桥）、`stt/`、`mcp_gateway/`
- `main/` 是异步任务与节点执行主线，含治理（角色矩阵、审批）与管理面 API
- `bridges/` 是独立的外部渠道桥接应用（如 `bridges/qq-onebot`），零 `g3ku` import，经 `/api/v1` 与本体通信
- `tools/` 与 `skills/` 是资源目录（可热重载、可 RBAC 控可见性），`externaltools/` 是第三方工具载荷
- `memory/`、`sessions/`、`temp/` 是运行数据目录，`.g3ku/` 是配置与状态根

也就是说，Negi 不只是一个前端页面加一个聊天后端，而是一整套可以长期运行、可扩展、可运维的 Agent 基础设施。

## 部署（Docker / Compose）

如果你希望把 Negi 作为长期运行的服务部署，而不是只在本机直接启动，可以使用仓库内置的 Docker / Compose 部署方式。

### 如何启动

1. 复制 `.env.docker.example` 为 `.env`
2. 处理 `G3KU_BOOTSTRAP_PASSWORD`：它是容器启动时用来自动解锁已初始化项目的口令（另有 `G3KU_BOOTSTRAP_MASTER_KEY` 可直接给主密钥）。首次安装仍然要在浏览器里创建项目口令，这个环境变量不会替你把口令设好；示例值 `change-me` 会以明文出现在容器环境里，换掉它再上生产
3. 在仓库根目录执行：

```bash
docker compose up --build
```

镜像基于 `python:3.11-slim`，用 `uv sync --frozen --no-dev` 装依赖；`skills/` 与 `tools/` 会作为种子目录随镜像带进去。

### 服务分工

- `compose.yaml` 会启动两个核心服务：`web` 和 `worker`
- `web` 负责 Web 界面、API、heartbeat、cron，以及外部桥接 API（/api/v1）
- `worker` 只负责 detached task worker，也就是后台异步任务执行

这种拆分方式的重点不是把功能切碎，而是让 Web 入口和后台任务执行各自独立运行，同时共享同一份项目状态与资源目录。

### 必须持久化的目录

如果你希望容器重启后，会话、任务、模型配置、skills、tools 和任务临时文件继续保留，下面这些目录必须一起挂载到持久化卷：

- `.g3ku/`
- `memory/`
- `sessions/`
- `temp/`
- `skills/`
- `tools/`
- `externaltools/`

这组目录共同覆盖了项目配置、模型绑定和密钥状态、会话历史、任务运行时数据、长期记忆、任务临时文件、可变技能资源、工具资源以及第三方工具载荷。只保留其中一部分通常是不够的；如果缺少其中任意关键目录，系统虽然可能还能启动，但重启后会出现状态丢失。

### 适用场景

- Docker 部署是一条独立路径，不会替代 `start-g3ku.ps1` 和 `start-g3ku.sh`
- 本地开发、调试和单机直接运行，仍然推荐继续使用现有启动脚本
- Docker / Compose 更适合长期运行、进程隔离和统一管理
- 依赖本机环境的扩展（`web-access` 的 CDP 页面操作、composer 麦克风）在容器里没有对应宿主设备或浏览器，仍然按本机方式使用

如果你在 Docker 部署后发现 Web 正常，但 worker 重启后丢失模型、会话或任务状态，优先检查这些目录是否都已经正确持久化，而不是先怀疑业务逻辑本身。

## 7. 致谢与参考

Negi 在设计和迭代过程中，参考了部分优秀开源项目的工程实践与产品思路。在此对相关项目与作者表示感谢。

- [OpenClaw](https://github.com/openclaw/openclaw.git)
  为 Negi 的整体项目开发方向和 Agent 工程化实践产生了启发。
- [Hermes Agent](https://github.com/NousResearch/hermes-agent.git)
  为 Negi 的自主维护记忆、长期记忆沉淀与持续协作能力提供了灵感。
- [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent.git)
  为 Negi 的多 Agent 编排、任务拆解与协同推进提供了启发。
- [OpenViking](https://github.com/volcengine/OpenViking.git)
  为 Negi 的分层渐进式加载、能力暴露控制与上下文组织方式提供了灵感。

说明：Negi 为结合自身目标、运行时设计与使用场景的独立项目。历史版本曾整合 `openclaw-china`（MIT）上游运行时代码，已随内置渠道子系统一并移除；`skills/web-access`、`bridges/qq-onebot` 等随仓库带的第三方来源各自保留自己的许可声明。

## 8. 许可证

本项目整体采用 MIT 许可证，详见 [LICENSE](LICENSE)。第三方来源说明见上一节；架构文档的维护规则见 `docs/architecture/README.md`「Maintenance Rules」。
