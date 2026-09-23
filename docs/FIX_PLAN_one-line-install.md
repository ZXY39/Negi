# Fix Plan: 一行指令安装（`install.ps1` / `install.sh` / `.python-version` / tag `v1.0.0`）

> Origin: operator request, 2026-09-23 —— 「应用可在一个新设备上，输入一行指令完成安装」。追问过是否要发 release，裁决为：**只打 tag 固定安装入口，不发 release 资产、不上 PyPI**。
>
> Status: 设计已定，四个阶段（P1 脚本本体 → P2 版本钉 → P3 验证 → P4 文档 → P5 提交打标签）。
>
> Scope: 仓库根的 `install.ps1` / `install.sh`、新增 `.python-version`、`README.md` §1–§2、`docs/architecture/operations-and-maintenance.md` §1。**运行时代码一字不改**（见 §2.2：绕开宿主 Python 检查的方式不需要动 `g3ku_bootstrap.py`）。

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
| `-Dir` | 安装位置，默认 `%USERPROFILE%\G3KU-Agent` / `$HOME/G3KU-Agent` |
| `-NoStart` | 装到第 4 步为止，不拉 Web —— 也是本计划 §4 的验证入口 |
| `-Ref` | 覆盖钉住的 ref，用于回滚或试装预发布 |

网络镜像**不加参数**：uv 自身读 `UV_DEFAULT_INDEX` 与 `UV_PYTHON_INSTALL_MIRROR`，环境变量天然穿透，脚本再包一层就是重复配置面。国内弱网时在指令前 `set`/`export` 即可。

### 2.4 边界：一行指令做不到的那一步

安装完成的终点是**口令设置页**，不是可用系统。缺 `.g3ku/llm-config/master.key` 时除 `/api/bootstrap` 外全部 `/api/*` 返回 `423 project_locked`（合同见 `config-and-models.md`「Deployment Unlock Contract」）。这是设计上的安全边界，脚本只能打印 URL 后停在那里。

---

## 3. Phases

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| P1 | 写 `install.ps1` / `install.sh`（同一状态机，两种 shell 方言） | 2 个仓库根文件 |
| P2 | 新增 `.python-version` = `3.12`（与本机 `.venv` 实测版本一致） | 1 个文件 |
| P3 | 验证（§4） | 证据，不落文件 |
| P4 | `README.md` §1–§2 与 `docs/architecture/operations-and-maintenance.md` §1 就地改写 | 文档 |
| P5 | 提交 + 轻量标签 `v1.0.0`（推送需操作员确认） | 1 commit + 1 tag |

---

## 4. 验收办法

1. 语法：`bash -n install.sh`；PowerShell 用 `Parser::ParseFile` 取 AST 错误。
2. **真装**：`install.ps1 -Dir <临时目录> -NoStart` 端到端跑完 1–4 步（uv 缓存已热，代价是一次 `uv sync`）。
3. 装完的树里做导入冒烟：`<临时>/.venv/Scripts/python.exe -c "import main.protocol, g3ku.cli.commands"` ⇒ 证明跟踪集自身完备（覆盖 M3 的 42 处 `from main`）。
4. 全程不启 Web，避免和在跑的 18790 主实例抢单实例锁（`.g3ku/start.lock`）。
5. 清理临时目录。

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
