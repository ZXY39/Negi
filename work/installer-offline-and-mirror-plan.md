# 计划：exe 内置源码与 uv，Python / wheels 自动换源

## 1. 目标

1. 新设备双击 `Negi-Setup.exe` 时，**取代码不再访问网络**：tag 的源码快照随 exe 分发。
2. `uv` 随 exe 分发并持久化，不再依赖 astral.sh（升级时 exe 临时目录已删，因此必须落到长期位置）。
3. `uv python install` 与 `uv sync`：直连失败时自动换镜像重试；可显式强制或禁用。
4. 升级车道（`install.ps1 -Upgrade`，含网页「重启并更新」）共用同一套换源逻辑。
5. 明确**不做**：不打包 wheelhouse（`.venv` 实测 275MB，exe 会到 150–200MB）；不引入 Gitee / 不改 origin 语义；不做断网「整树替换」式升级（见 §7 后续项）。

## 2. 已核实事实（写计划前实测）

- exe 载荷目前只有两个脚本，代码是安装时联网取的（[packaging/negi.iss:45](file:///D:/Project/Negi/packaging/negi.iss#L45)–52）。
- 取码两条车道都直连 GitHub，没有镜像、没有重试（[install.ps1:26](file:///D:/Project/Negi/install.ps1#L26)–27、[install.ps1:142](file:///D:/Project/Negi/install.ps1#L142)–172）；升级是 `git fetch --depth 1 origin`（[install.ps1:263](file:///D:/Project/Negi/install.ps1#L263)–285）；网页升级调用同一个脚本（[g3ku/update_apply.py:161](file:///D:/Project/Negi/g3ku/update_apply.py#L161)–189）。
- 更新检查依赖 `.git` + `origin`，超时 2 秒（[g3ku/update_check.py:57](file:///D:/Project/Negi/g3ku/update_check.py#L57)–80）。没有 `.git` 的设备只是「查不到」，不会报错——也就是说载荷必须保留 `.git`，否则新装设备会静默失去更新提示。
- **浅克隆目录可直接分发**：实测 `git clone --depth 1 --branch v2.7.0` 的整目录（含 `.git/shallow`）复制后，`git log`、`git status`、`git fetch --depth 1 origin v2.6.0`、`git checkout --detach FETCH_HEAD` 全部正常。
- **浅克隆打 bundle 不可行**：实测 `git bundle create` 成功、`git clone <bundle>` 成功，但克隆出来的仓库 `git log` 报 `Failed to traverse parents`（shallow 边界没有携带）。所以载荷用「整目录 zip」，不用 bundle。
- 体积：HEAD 树 24.1MB；`git archive` zip 8.2MB；浅克隆目录 35MB，含 `.git` 压缩后约 14.6MB（gzip 估计，Inno lzma 后更小）。
- **CRLF 陷阱**：CI 默认 `core.autocrlf=true` 时克隆出的 `install.ps1` 是 CRLF，索引里是 LF；设备若 `core.autocrlf=false`，`git status` 会显示全树改动。载荷必须用 `git -c core.autocrlf=false` 构建，并把 `core.autocrlf=false` 写进载荷 `.git/config`（仓库本地配置优先于设备全局配置）。
- 版本载体：`install.ps1` 的 `$Ref`（[install.ps1:13](file:///D:/Project/Negi/install.ps1#L13)）、`g3ku/__init__.py` 的 `__version__`、exe 产物名三者必须一致；现在只有 release.yml 检查后两者（[.github/workflows/release.yml:54](file:///D:/Project/Negi/.github/workflows/release.yml#L54)–60）。
- `install.ps1` 必须纯 ASCII、无 BOM（[tests/resources/test_start_scripts_and_readme.py:123](file:///D:/Project/Negi/tests/resources/test_start_scripts_and_readme.py#L123)–145），新代码与新增日志一律英文。
- 包管理镜像目前没有安装器入口，只靠用户自己导出环境变量（[README.md:92](file:///D:/Project/Negi/README.md#L92)–96），而 exe 是隐藏窗口运行，用户没有入口。

## 3. 方案

### 3.1 载荷构成（发版时构建）

新增 `packaging/uv-version.txt`，钉一个 uv 版本（形如 `0.8.22`）。`packaging/build-setup.ps1` 增加三步：

1. **源码载荷 `negi-source.zip`**
   - `git -c core.autocrlf=false clone --depth 1 --branch $TAG <repo> <stage>`；
   - `git remote set-url origin https://github.com/ZXY39/Negi.git`（防止本地构建时残留 file:// 地址）；
   - `git config core.autocrlf false` 写进仓库本地配置；
   - 用 `packaging/distribution-excludes.txt` 应用现有 sparse 规则（`/*` + `!/<name>/`），不抄第二份清单；
   - 校验 `refs/tags/$TAG^{commit}` 存在且等于 `HEAD`；
   - 用 .NET `ZipFile::CreateFromDirectory` 打包整个目录（含 `.git`），避免 `Compress-Archive` 对大量小文件过慢。
2. **`uv.exe`**：按 `uv-version.txt` 从 astral-sh/uv release 下载 win-x64 资产，按发布页 `.sha256` 校验；下载或校验失败即构建失败（fail-closed）。另提供 `-UvZipPath` 供离线/慢网本地构建时指定已校验的包。
3. **版本断言**（构建期）：`-Version` == `install.ps1` 的 `$Ref.TrimStart('v')` == `g3ku.__version__` == payload 的 tag。任一不符直接失败，避免做出「exe 名是新版、载荷是旧版」的产物。

`packaging/negi.iss`：
- `[Files]` 增加 `negi-source.zip`、`uv.exe`（均 `deleteafterinstall`，落到 `{tmp}`）；
- `[Run]` 的 setup-run 参数追加 `-SourceArchive "{tmp}\negi-source.zip" -UvPath "{tmp}\uv.exe"`；
- 更新文件头注释：载荷不再只是两个脚本。

`.github/workflows/release.yml` 的 `windows-installer` job 不需要新步骤（构建脚本自带下载与校验），但要确认 runner 能访问 GitHub Releases。

### 3.2 install.ps1 消费载荷

新增参数（默认空 = 现行为不变）：

- `-SourceArchive <path>`：本地源码载荷。
- `-UvPath <path>`：本地 uv 可执行文件。
- `-Mirror` / `-NoMirror`：强制镜像起步 / 禁用自动换源（见 §3.3）。

**取码**（`Get-CheckoutStage`）：
- 全新安装且 `-SourceArchive` 存在 → 解压到暂存目录，校验 HEAD 对应 `refs/tags/$Ref^{commit}`；不一致则报错并回退联网车道（日志说明原因，不装错版本）。
- 机器没有 git → 合并前删掉暂存载荷里的 `.git`，保持现有「无 git = 归档车道」的行为。否则 `Update-Code` 会因为看到 `.git` 又找不到 git 而拒绝升级（[install.ps1:294](file:///D:/Project/Negi/install.ps1#L294)–300）。
- 无载荷 / 校验失败 → 现有 clone、zip 两条车道原样保留（回滚面）。

**uv**（`Ensure-Uv`）顺序改为：PATH / 已知安装位（现状）→ `-UvPath` 复制到 `%USERPROFILE%\.local\bin\uv.exe` 并使用 → astral.sh 兜底。复制而不是临时使用，是因为 exe 的 `{tmp}` 会被删，而升级（含网页升级）还要再跑 `uv sync`。

**升级**：
- git 车道不变：`fetch --depth 1 origin $Ref` + `checkout --detach FETCH_HEAD`。
- 无 git 的旧安装 + 带 `-SourceArchive` → 直接合并本地载荷，不再下载 zip。
- 有 git 但网络失败 → 维持现状（报错、服务按旧版本拉回）。断网升级方案见 §7「后续项」。

### 3.3 Python 与 wheels 换源

优先级：显式参数（`-Mirror` / `-NoMirror`）> 环境变量（`UV_DEFAULT_INDEX` / `UV_PYTHON_INSTALL_MIRROR`）> 自动。

自动策略是**失败触发重试，不做测速**（测速要真下东西才准，且「能连但很慢」永远测不出来）：

1. `uv python install <pin>` 先直连；失败 → 设置 `UV_PYTHON_INSTALL_MIRROR` 重试一次；
2. `uv sync --frozen` 先直连；失败 → 设置 `UV_DEFAULT_INDEX` 重试一次；
3. 每次尝试与最终选源写进输出；两次都失败才中止，并打印两个候选与手工设置方法。

镜像清单集中在脚本内一处数组，**只放实测可用的**。候选（实现时逐个实测后定稿，不先写死）：
- PyPI：`https://pypi.tuna.tsinghua.edu.cn/simple`（README 已在用）；
- python-build-standalone：NJU / SJTU 的 GitHub release 镜像，或 `ghfast.top` 代理前缀；
- 所有源必须 HTTPS。

`-Mirror` 供「直连能通但很慢」的网络直接从镜像起步；`-NoMirror` 供企业环境或复现故障时只走直连。双击 exe 的用户仍可通过系统环境变量预设，exe 继承用户环境。

`install.sh` 同步同一套重试与开关（不涉及载荷）。

### 3.4 日志

exe 的 `[Run]` 是 `runhidden`，当前失败只有向导一句报错。`packaging/setup-run.ps1` 把 `install.ps1` 的输出 Tee 到 `%LOCALAPPDATA%\Negi\logs\setup.log`，失败时在控制台与向导错误里给出该路径；换源决策同在这份日志里。

## 4. 阶段划分

| 阶段 | 内容 | 主要文件 |
| --- | --- | --- |
| A | 载荷构建与版本断言 | `packaging/build-setup.ps1`、`packaging/negi.iss`、`packaging/uv-version.txt`、`.github/workflows/release.yml`（如需要） |
| B | install.ps1 消费载荷、uv 持久化、无 git 处理 | `install.ps1`、`packaging/setup-run.ps1` |
| C | 换源重试与开关（两个安装脚本） | `install.ps1`、`install.sh` |
| D | 测试与文档 | `tests/resources/*`、`README.md`、`docs/architecture/operations-and-maintenance.md` |

A 单独合入不改变任何现有行为（没人传新参数就走旧路），B/C 依赖 A 产出的载荷格式。每阶段独立提交、可回滚。

## 5. 验收

### 5.1 静态与单测

- 新增 `tests/resources/test_installer_payload_contract.py`：
  - `negi.iss` 的 `[Files]` 含两个新载荷，`[Run]` 传 `-SourceArchive` / `-UvPath`；
  - `install.ps1` 声明三个新参数，且 `-UvPath` 在 astral.sh 兜底之前被使用；
  - `packaging/uv-version.txt` 存在且是完整版本号；
  - 构建脚本读的是 `packaging/distribution-excludes.txt` 同一份清单；
  - 两个安装脚本都包含 `UV_DEFAULT_INDEX` / `UV_PYTHON_INSTALL_MIRROR` 的换源路径。
- 扩展 `tests/resources/test_setup_exe_lane.py`：桩 install.ps1 增加新参数，断言全新安装与升级都收到载荷路径，且路径真实存在。
- 保持绿：`test_distribution_excludes_lane.py`、`test_start_scripts_and_readme.py`（ASCII / BOM 规则）、`test_bootstrap_start_lane.py`（uv 兜底位一致）。
- `python -m ruff check .` + `python -m pytest tests/resources/test_resource_runtime_smoke.py -q` + 新增测试。

### 5.2 构建验收

- `.\packaging\build-setup.ps1 -Version 2.7.0` 产出 exe；记录体积（预计 30–50MB）。
- 从 exe 解出 `negi-source.zip`，校验：`git -C <payload> log -1` 正常、`refs/tags/v2.7.0^{commit}` 与 HEAD 一致、`git status` 干净、`core.autocrlf=false` 在仓库本地配置里。

### 5.3 Windows 干净机矩阵（Windows 11）

1. hosts 屏蔽 `github.com` / `codeload.github.com` / `raw.githubusercontent.com` / `astral.sh` → exe 安装成功；日志显示 bundled source / bundled uv；`.git` 存在且 `origin` 指回 GitHub；`negi` 能启动。
2. 全直连 → 仍使用载荷取码（日志可见），不联网 clone。
3. 屏蔽 `pypi.org` → `uv sync` 走镜像重试成功，日志有两次尝试记录。
4. 已有脚本安装（git 车道）→ exe 升级走 `fetch origin`，`.venv` / `.g3ku` 保留。
5. 已有无 git 安装 → exe 升级使用本地载荷，不下载 zip。
6. 设备全局 `core.autocrlf=true` → 安装后 `git status` 干净、`-Upgrade` 不被「跟踪文件有改动」拦住。
7. 传入版本不匹配的载荷 → 拒绝并回退联网车道，不装错版本。
8. 卸载保留数据后重装 → `.g3ku` 原样，口令与任务仍在。

## 6. 风险与回退

- **浅克隆可升级性**：已实测 git 2.x 的 log / fetch / checkout；更老的 git 未验。失败时安装脚本回退联网车道，不会留下半安装状态（现有暂存目录机制不变）。
- **版本漂移**：构建期断言 + 安装期 HEAD 校验，双保险。
- **CRLF**：载荷用 LF 构建并在 `.git/config` 固定 `core.autocrlf=false`；矩阵用例 6 专门验。
- **uv 供应链**：固定版本 + sha256；校验失败拒绝构建；运行时仍有 astral.sh 兜底。
- **镜像完整性**：wheels 由 `uv.lock` 哈希校验，python-build-standalone 由 uv 元数据校验；仅允许 HTTPS。
- **exe 体积**：预计增加 20–30MB，属可接受代价；不引入 wheelhouse。
- **已知未解决**：升级仍走 `git fetch origin`，断网设备的「重启并更新」会失败（与现状一致）；更新检查 2 秒超时不改（慢网无红点，与现状一致）。
- **回退**：去掉 `negi.iss` 的两个 `[Files]` 与 `[Run]` 参数即可恢复原行为；`install.ps1` 新参数默认空。

## 7. 后续项（本轮不做，等你决定）

1. **断网升级兜底**：exe 升级时若 `fetch` 失败且无跟踪改动，用载荷整树替换代码与 `.git`（保留 `.venv` / `.g3ku`）。会让本地 `.git` 历史重置为浅克隆；有本地改动时仍拒绝。需要单独确认。
2. **Gitee / 对象存储镜像**：作为 clone/fetch 的候选源，需解决 tag 同步与 origin 语义（详见上一轮讨论）。
3. **更新检查换源**：`git ls-remote` 失败时尝试镜像/proxy，改善慢网下的更新提示。

## 8. 交接

- 改动文件：`packaging/build-setup.ps1`、`packaging/negi.iss`、`packaging/setup-run.ps1`、`packaging/uv-version.txt`（新增）、`install.ps1`、`install.sh`、`.github/workflows/release.yml`（按需）、`tests/resources/test_installer_payload_contract.py`（新增）、`tests/resources/test_setup_exe_lane.py`、`README.md`、`docs/architecture/operations-and-maintenance.md`。
- 构建命令：`.\packaging\build-setup.ps1 -Version <x.y.z>`；本地无网时用 `-UvZipPath <已校验的 uv.zip>`。
- 文档：实现完成后必须按 `skills/negi-architecture-maintenance/SKILL.md` 判断并更新架构文档；至少涉及 `operations-and-maintenance.md` 的「新设备首次安装与升级」（第 20、27、37、40 条附近，其中「镜像源不设安装器参数」一条会被本计划改写）。
- 默认决策（可否决）：载荷用浅克隆整目录；首次安装一律用载荷；换源用失败重试而非测速；uv 持久化到 `~/.local/bin`；断网升级与 Gitee 不进本轮。
