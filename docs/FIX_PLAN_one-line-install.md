# Fix Plan: 一行指令安装（`install.ps1` / `install.sh` / `.python-version` / tag `v1.0.0`）+ 升级与版本识别

> Origin: operator request, 2026-09-23 —— 「应用可在一个新设备上，输入一行指令完成安装」。追问过是否要发 release，裁决为：**只打 tag 固定安装入口，不发 release 资产、不上 PyPI**。同日追加第二问：「安装后如何升级、默认装在哪、能否自动识别新版本」⇒ 本文覆盖 P1–P5（安装，已上线 `v1.0.0`）与 P6–P9（升级与版本识别）。
>
> Status: P1–P5 已落地并推送（tag `v1.0.0`）。P6–P9 设计已定，形状在 §2.6–§2.8。
>
> Scope: 仓库根的 `install.ps1` / `install.sh`、`.python-version`、`.gitattributes`、`README.md` §1–§2、`docs/architecture/operations-and-maintenance.md` §1；P6 起新增运行时刻线：`g3ku/update_check.py` 与 `g3ku status` 的一行输出（安装阶段本身不碰运行时代码）。

---

## 0. Executive Summary

四个决定性事实，它们决定了方案形状：

| # | 测量 | 结果 | 决定 |
| --- | --- | --- | --- |
| M1 | 干净机器的断点 | `g3ku.ps1` 找不到 `.venv` 时退到 `py -3`/`python`，都没有就停在 `Python not found`；`g3ku_bootstrap.py:102` `_ensure_host_python_supported()` 对宿主解释器 `<3.11` 直接 `SystemExit`（`:92-98`） | 安装入口**不能依赖用户预装 Python**。必须自己提供解释器 |
| M2 | uv 能否自带解释器 | 本机 `uv 0.12.9` 已在 `%APPDATA%\uv\python\` 托管 `cpython-3.11.15` 与 `3.12.13` | 一条 `uv python install` 即满足 M1，不需要嵌入 Python 或做大包 |
| M3 | wheel/sdist 是否可用 | `uv build` 成功但 wheel 只含 `g3ku/`（275 文件）；sdist 无 `main/`、`runtime/`、`tools/`；而 `g3ku/` 内有 **42 处 `from main...`**（如 `g3ku/shells/web.py:51`） | `pip install g3ku-ai` / PyPI / `uvx` 现在**必然** `ModuleNotFoundError: main`。分发形态只能是完整 checkout ⇒ 一行指令的目标是"取到 checkout 并把环境建好" |
| M4 | 运行时是否依赖外网 | 前端字体与图标已是本地 vendor（`g3ku/web/frontend/vendor/fonts/google-fonts.css`、`vendor/lucide-manifest.json`），文件里的 `fonts.googleapis.com` / `registry.npmjs.org` 只是 manifest 的 `source_url` 出处字段 | 安装完成后**离线可用**，不需要为离线场景造自包含大包 |

一处**已核查后排除的风险**：GitHub 源码包只含 git 跟踪文件，而本机工作树里 `skills/` 有 1184 个文件、跟踪的只有 102 个。逐目录核对后确认差集是本地安装的 marketplace skill（`sn-*` / `boss-*` / `job-*`）、`externaltools/`（`.gitignore:51`）、`.g3ku/`（`:47`）—— 全部**本就不该随安装分发**。跟踪集含 15 个精选 skill、`main/` 87、`tools/` 96、`bridges/` 10。根目录 `runtime/`（0 跟踪 / 2 落盘）无人 import，是残留。

---

## 1. Current State (verified at HEAD `ab979af4`)

### 1.1 现有入口链条

`start-g3ku.ps1`（端口预检 + 孤儿进程优雅退出）→ `g3ku.ps1`（选解释器）→ `g3ku_bootstrap.py`（建 `.venv` → 装依赖 → 起 `python -m g3ku web` → 轮询 `/api/bootstrap/status` → 打印可点击 URL，`:208-220`、`:258-278`）。

依赖安装本身已经是三档回退（`:127-144`）：有 `uv` 且有 `uv.lock` ⇒ `uv sync --frozen`（`:134-135`）；否则有 pip ⇒ `pip install -e .`（`:136-138`）；都没有 ⇒ 报错并给出 uv 安装地址（`:139-143`）。

**结论：安装引擎已经足够好，缺的只是它上游的两件事——取代码、取解释器。**

### 1.2 新设备上的两个缺口

| 缺口 | 证据 |
| --- | --- |
| 必须先 `git clone` | `README.md:52-57` 的第一步就是 clone；裸 Windows 常无 git |
| 必须已有宿主 Python ≥ 3.11 | `g3ku.ps1` 末行 `Write-Error "[g3ku] Python not found..."`；`g3ku_bootstrap.py:102` |

### 1.3 版本与分发面现状

- 远端 **0 个 tag**（`git ls-remote --tags origin` 空返回），本地仅一个 `backup/pre-reword-ac670b5b` ⇒ 今天没有任何 release 可发。
- 版本号两处已经是同一个值：`pyproject.toml:3` 与 `g3ku/__init__.py:5` 都是 `1.0.0` ⇒ 打 `v1.0.0` 无需改代码。
- `.python-version` 未被跟踪 ⇒ uv 选哪个解释器不确定，这是安装可复现性的唯一漏洞。
- 仓库公开（匿名 `git ls-remote` 成功）⇒ `raw.githubusercontent.com` 上的一行指令可被任意设备取到。

---

## 2. Design

### 2.1 用户侧：一行

```powershell
# Windows PowerShell
iwr https://raw.githubusercontent.com/ZXY39/G3KU-Agent/v1.0.0/install.ps1 | iex
```

```bash
# macOS / Linux
curl -LsSf https://raw.githubusercontent.com/ZXY39/G3KU-Agent/v1.0.0/install.sh | sh
```

`v1.0.0` 是钉住的 ref：一行指令必须指向不可变引用，否则"昨天能装今天不能装"无法归因。

### 2.2 脚本内部：五步，全部复用现成机制

1. **确保 uv**：探测 PATH，缺失则用官方 astral 安装脚本装，并把 `%USERPROFILE%\.local\bin`（unix：`~/.local/bin`）补进当前会话 PATH。
2. **确保解释器**：`uv python install`（读 `.python-version`）⇒ 满足 M1，用户机器上没有 Python 也能继续。
3. **取代码**：有 `git` ⇒ `git clone --depth 1 --branch v1.0.0`；无 `git` ⇒ 直接下 `archive/refs/tags/v1.0.0.zip` 并剥掉顶层目录。目标目录已存在则**跳过取码**（幂等，不覆盖用户数据）。
4. **建环境**：`uv sync --frozen`（运行时依赖，不带 dev extra）。
5. **启动**：`<venv>/python g3ku_bootstrap.py web`。

第 5 步的形状是关键：用 **venv 解释器**跑 bootstrap，`sys.version_info` 就是刚装好的 3.12，`:102` 的检查自然通过 —— 因此**不需要修改 `g3ku_bootstrap.py`**。随后 `:232` 的 `os.chdir(PROJECT_ROOT)` 与 `:252` 的 `-m g3ku` 一起，让根目录 `main/` 在 `sys.path` 上可导入（M3 依赖的正是这个，而不是包安装）。

### 2.3 参数面（刻意做小）

| 参数 | 用途 |
| --- | --- |
| `-Dir` / `--dir` | 安装位置，默认 `%USERPROFILE%\G3KU-Agent` / `$HOME/G3KU-Agent` |
| `-NoStart` / `--no-start` | 装到第 4 步为止，不拉 Web —— 也是本计划 §4 的验证入口 |
| `-Ref` / `--ref` | 覆盖钉住的 ref，用于回滚或试装预发布 |
| `-Upgrade` / `--upgrade` | 显式升级已装好的设备（§2.6） |

网络镜像**不加参数**：uv 自身读 `UV_DEFAULT_INDEX` 与 `UV_PYTHON_INSTALL_MIRROR`，环境变量天然穿透，脚本再包一层就是重复配置面。国内弱网时在指令前 `set`/`export` 即可。

### 2.4 边界：一行指令做不到的那一步

安装完成的终点是**口令设置页**，不是可用系统。缺 `.g3ku/llm-config/master.key` 时除 `/api/bootstrap` 外全部 `/api/*` 返回 `423 project_locked`（合同见 `config-and-models.md`「Deployment Unlock Contract」）。这是设计上的安全边界，脚本只能打印 URL 后停在那里。

### 2.5 升级：`-Upgrade` 的两条取码路

默认（不带 `-Upgrade`）对已存在的目录是**幂等不动代码**，只补环境与启动 —— 这一条是刻意的：一行指令既是安装也是启动入口，它不能顺手覆盖用户已经在用的代码树。升级因此是显式动作：

| 安装形态 | 升级动作 | 保护 |
| --- | --- | --- |
| git 检出 | `git fetch --depth 1 origin <ref>` + `git checkout --detach FETCH_HEAD` | 先跑 `git status --porcelain`，非空即**拒绝**，不静默覆盖用户改动 |
| 无 git（源码包） | 重新下载归档，逐顶层条目覆盖 | 跳过 `.venv` / `.g3ku` / `.git`；用户数据与环境原地保留 |

两条实测约束：

- **混用有代价**。把源码包盖在 git 检出上，autocrlf 会让整棵树在 `git status` 里变成永久"脏"（内容其实等价，`git diff` 无 hunk），下一次升级就被自己的脏检查挡住 —— 本机验证时真踩到（873 个文件全标 ` M`）。所以"有 `.git` 但 git 不可用"直接报错，不退化覆盖。
- **源码包升级不回收删除文件**。上一版存在、新版没有的文件会留在原地。可接受，README 与运维文档都写明；在意就删目录重装。

### 2.6 版本识别：只读一条通道

| 通道 | 判定 |
| --- | --- |
| `git ls-remote --tags origin` | **采用**。分发形态本就是 checkout（M3），不吃 API 配额，离线/失败可静默 |
| GitHub API `/repos/.../tags` | 未鉴权 60 次/小时/IP，不适合常规调用；`/releases/latest` 更要求先发 release，与既定裁决冲突 |
| raw 上的 `VERSION` 文件 | 只在无 git 的设备上才需要，会多出"发版要同步第三处"的维护面 —— 暂不做 |

形状规则：只接受 `refs/tags/vX.Y.Z`，按形状过滤掉路径型标签（`refs/tags/backup/...`）与 peeled 的 `^{}` 重复行，取最高 semver 而非文件序最后一行。本地侧与 `g3ku/__init__.py` 的 `__version__` 比对。

三条硬约束：**只出不进**（不上传任何本地信息，版本号也不外发）、**超时 2 秒**、**失败即静默**（离线设备不得显示"已是最新"，宁可不出现）。

呈现面只有一个：`g3ku status` 尾行 `Release:`。不做启动时自动弹窗、不做 Web 界面横幅 —— 检查更新的主动权留在操作员手上，避免把一次网络往返塞进启动路径。

---

## 3. Phases

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| P1 | 写 `install.ps1` / `install.sh`（同一状态机，两种 shell 方言） | 2 个仓库根文件 |
| P2 | 新增 `.python-version` = `3.12`（与本机 `.venv` 实测版本一致） | 1 个文件 |
| P3 | 验证（§4） | 证据，不落文件 |
| P4 | `README.md` §1–§2 与 `docs/architecture/operations-and-maintenance.md` §1 就地改写 | 文档 |
| P5 | 提交 + 标签 `v1.0.0` | 已推送 |
| P6 | `g3ku/update_check.py` + `g3ku status` 的 `Release:` 行 + `tests/test_update_check.py` | 运行时刻线 |
| P7 | 两个安装脚本加 `-Upgrade` / `--upgrade`（§2.5），含混用拒绝 | 安装器 |
| P8 | 验证（§4 的 6–9 项） | 证据 |
| P9 | README「升级与版本检查」+ 运维文档同节就地改写 | 文档 |

---

## 4. 验收办法

1. 语法：`bash -n install.sh`；PowerShell 用 `Parser::ParseFile` 取 AST 错误。
2. **真装**：`install.ps1 -Dir <临时目录> -NoStart` 端到端跑完 1–4 步（uv 缓存已热，代价是一次 `uv sync`）。
3. 装完的树里做导入冒烟：`<临时>/.venv/Scripts/python.exe -c "import main.protocol, g3ku.cli.commands"` ⇒ 证明跟踪集自身完备（覆盖 M3 的 42 处 `from main`）。
4. 全程不启 Web，避免和在跑的 18790 主实例抢单实例锁（`.g3ku/start.lock`）。
5. 清理临时目录。
6. 版本识别的纯函数用单测覆盖（最高 semver、形状过滤、空输出、无 origin 时返回 None），不依赖网络。
7. 脏树守卫：在临时安装里改一个跟踪文件 → `-Upgrade` 必须非零退出并指名目录；还原后必须成功。
8. 无 git 形态：把 PATH 收窄到"只有 uv 和 Windows 系统目录"（`Get-Command git` 为假），先跑一次全新归档安装（断言目录里没有 `.git`），再跑 `-Upgrade`，断言 `.g3ku/`、`.venv/` 里的哨兵文件仍在、被删的跟踪文件被恢复。
9. `Release:` 行走真实 origin：`g3ku status` 应打出当前版本与最新标签；把 `fetch_latest_release_tag` 打桩成更高标签，应改走"有新版"文案。

未列入本计划、但需要操作员实盘确认的：**在一台真的什么都没有的机器上跑一次**。本机验证只能覆盖"已有 uv 与热缓存"的路径。

---

## 5. 明确不做

| 不做 | 原因 |
| --- | --- |
| 发 GitHub Release 资产 | 一行指令取的是 `raw.githubusercontent.com` 的脚本 + 源码归档，release 资产对这个流程零增益（M3） |
| 上 PyPI / `uvx g3ku-ai` | wheel 缺 `main/`，要先做包结构重构；收益不抵代价 |
| 离线自包含大包（嵌入 Python + wheels 矩阵） | M4 证明运行时不需要外网，弱网问题用镜像环境变量解决即可 |
| 默认安装 `playwright install chromium` | 只有浏览器类工具需要，几百 MB，不进默认路径 |
| 改 `g3ku_bootstrap.py` 的宿主 Python 检查 | §2.2 已说明不需要；放宽它等于删掉一条真实的前置校验 |
| 不带参数的隐式升级 | 一行指令同时是"再启动一次"的入口，隐式换代码会把用户的运行现场掀掉 |
| 启动时自动检查更新 / Web 上的版本横幅 | §2.6：检查留在操作员主动跑的 `g3ku status` 里 |
| 自动回滚上一次升级 | 需要留副本与状态机，代价远高于"再跑一次 `-Ref <旧 tag>`" |
