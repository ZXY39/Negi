# Negi 运维与维护建议

本文档从“接手项目后怎么跑、怎么验证、怎么排障”的角度总结维护要点。

如果问题与 prompt cache 命中、跨 turn 上下文连续性、append-only request growth、或 actual request artifact 对账有关，请同时阅读：

- `runtime-overview.md`
- `context-and-cache-troubleshooting.md`

## 1. 基本启动方式

产品显示名是 **Negi**，仓库路径 `ZXY39/Negi`，操作者敲的命令与入口脚本也叫 Negi（`negi web`、`negi status`、`start-negi.ps1` / `start-negi.sh` / `start-negi.cmd`、包装 `negi.ps1` / `negi.sh` / `negi.cmd`、引导 `negi_bootstrap.py`）。Python 包目录 `g3ku/` 与 `python -m g3ku` 形态、数据根 `.g3ku`、`G3KU_*` 环境变量仍是旧拼写，属冻结项（清单见 `AGENTS.md`「Brand Name vs Frozen Identifiers」）。写进模型上下文的协议块前缀已经去品牌（`[TOKEN_COMPACT_V2]` / `[NOTICE_TAIL]` / `[SKILL_ROSTER_NOTICE]`），运行时只是继续**读**存量数据里的少数旧拼写（`[G3KU_TOKEN_COMPACT_V2]`、`[G3KU_APPEND_NOTICE_TAIL_V1]`、`[SKILL_ROSTER_NOTICE_V1]` 与 `[G3KU_SILENT]`）——排障时在老会话的 sidecar 或账本里看到它们属于历史形态，不是缺陷。排障时看到的 `.g3ku/config.json`、进程 cmdline 里的 `-m g3ku web` 都是正常的当前形态——入口名换了不代表进程形状换了，按进程找实例仍然要看 `-m g3ku`。

### 新设备首次安装与升级

用户侧一行指令（Windows `install.ps1`，Linux / macOS `install.sh`，仓库根），随后进入一键启动脚本同一条链路。

维护上要记住：

- 安装器只负责补齐它上游的两件事：**取解释器**（缺 uv 就装 uv，再由 uv 按 `.python-version` 提供 Python）与**取代码**（有 git 走 `git clone --branch <ref>`，没有 git 退化成 GitHub 源码包下载；两条都先落到临时目录再合并进目标）。依赖安装与环境复用全部交给既有的 `negi_bootstrap.py`，安装器不实现第二套
- 最后一步用 **venv 里的 python** 跑 `negi_bootstrap.py web`。这既让 `negi_bootstrap.py` 的宿主 Python 版本检查落在刚装好的解释器上，也保持了分发形态的前提：本项目安装的是**完整 checkout 就地运行**（bootstrap 会 `chdir` 到仓库根，`main/` 从工作目录导入），不是一个自包含的 Python 包 —— wheel/sdist 目前不含 `main/`，所以安装器与 `uv sync --frozen` / `pip install -e .` 才是唯一可用路径
- 安装得到的资源集是 **git 跟踪集去掉 `packaging/distribution-excludes.txt` 点名的顶层**（现为 `tests`、`work`、`.github`），再减去本来就不在跟踪集里的东西：操作员本地通过市场安装的 skills、`externaltools/`、`.g3ku/`。仓库自带 skills/tools 照旧在内。"新设备上少了一批 skill/tools" 属预期，不是安装失败
- 那份清单**只存在于负载里这一份**，两个安装脚本都从自己刚取到的树中读它，不存在第二处需要同步：git 车道在暂存 clone 上 `--no-checkout` → 读 `HEAD:packaging/distribution-excludes.txt` → `sparse-checkout set --no-cone '/*' '!/<顶层>/'` → `checkout`；源码包车道在逐顶层合并时跳过同名顶层。标签里没有这份清单时不设规则，整树照旧
- **排除只能是 sparse 规则，不能是删文件**：checkout 之后再删，每个被删的跟踪文件都被报成"跟踪文件已修改"，而 `-Upgrade` 的第一道门就是"跟踪文件没动过"，删一次就把这台设备永久挡在升级门外。规则写在 `.git` 里并随拷贝走，之后的 `fetch --depth 1` + `checkout --detach` 继续照规则出树。已被排除的顶层若在设备上原本存在，升级不动它——只不再取新内容。判据由 `tests/resources/test_distribution_excludes_lane.py` 钉住：清单里的每个顶层必须零读者（注释里提一句不算读者）、两条车道都必须真的消费清单、以及"排除后 `git diff --quiet` 与 `git status --porcelain --untracked-files=no` 都干净"
- 一行指令的地址与版本无关（`https://github.com/ZXY39/Negi/releases/latest/download/install.ps1`，Linux / macOS 换成 `install.sh`），所以引导文档不需要随发版改写；GitHub 的 `latest` 取最新的非草稿非预览发布。真正决定装哪一版的是脚本内的 `$Ref` / `REF` 钉值，而这份钉值随发布固化在该版本的资产里，于是"命令不变"与"每次装最新发布版"两条同时成立
- 安装脚本的参数：`-Dir` / `--dir` 换安装目录（默认 `%USERPROFILE%\Negi`，Linux / macOS `~/Negi`），`-NoStart` / `--no-start` 只建环境不启动，`-Ref` / `--ref` 指定版本，`-Upgrade` / `--upgrade` 换代码，`-NoPath` / `--no-path` 写命令桩但不改 PATH，`-NoShortcut` / `--no-shortcut` 不建图标。脚本按 `$Dir` 决议目标目录，`-Upgrade` 也一样，所以装在非默认目录的设备上手动升级要显式带上目录，否则会对着默认目录动手
- 双击安装的另一条通道是 `Negi-Setup-<version>.exe`（`packaging/negi.iss`，由 `packaging/build-setup.ps1` 用 Inno Setup 编译：`winget install JRSoftware.Inno.Setup`，版本号取自 `g3ku/__init__.py` 并同时喂给 `/DVersion` 与产物名，所以 exe 名与 `install.ps1` 的 `$Ref` 不会各说各话；发版时 `.github/workflows/release.yml` 的 `windows-installer` job 从**标签那份 checkout** 编出它并挂成资产，未签名是决定过的：有效证书在信誉攒够之前照样弹 SmartScreen，买不到东西）。这个 exe **只拥有向导**：目录选择页、进度页、它自己那条 ARP 行；安装本体仍是同一份 `install.ps1`，向导把选中目录交给 `packaging/setup-run.ps1 -Dir <dir> -Quiet -NoUninstallEntry`。载荷是两个脚本而不是程序：checkout 照旧按标签联网取，与一行指令完全同源
- **exe 同时是已装设备的认领通道**，语义与"向导只是前端"配套：`setup-run.ps1` 先认根再看开关——所选目录里有 `pyproject.toml` 就按升级处理；否则读 `%LOCALAPPDATA%\Negi\install-root.json`（与命令桩同一份指针），指针下的根真有代码就用它并说明"未使用所选目录"。目录页因此是**默认值不是投票**：一台装在别处的设备点"下一步"不会在旁边长出第二棵树（两棵树、一个端口、一份指针）。指针存在但下面没代码＝保留数据卸载过，按全新安装走。页面上的默认路径由 `{code:SuggestRoot}` 读注册表给出（先本 exe 自己的 `_is1`，再 `Uninstall\Negi` 的 `InstallLocation`，都没有才回落 `%USERPROFILE%\Negi`），所以进 `install.ps1` 之前目录就已经对上了。带 `-Upgrade` 不是可选：不带时 `install.ps1` 会"代码不动"却照样重写快捷方式，于是**新参数 `start --window` 指向不认识它的旧 bootstrap**，图标点了没反应——这条由 `tests/resources/test_setup_exe_lane.py` 用桩 install.ps1 逐条钉住
- 卸载行归属：`install.ps1` 写 ARP 前先扫 HKCU 里 `*_is1` 键的 `InstallLocation` 是否等于本根（Inno 在 `[Run]` 之前就把自己的键写好了，且它写的值**带尾反斜杠**，比较两侧都要 `TrimEnd('\')`）。被 exe 认领时它不写自己那行，并把上一版 `install.ps1` 为同一根留下的 `Uninstall\Negi` 删掉；指向别的根的那行不动（一台机器可以同时有一台脚本装的副本）。`-NoUninstallEntry` 保留成显式覆盖。卸载侧 `[UninstallRun]` 调 `{app}\uninstall.ps1` 且**刻意不带数据开关**：保留/清除是卸载时刻的决定，归 `uninstall.ps1` 自己的弹窗，放进度页勾选项等于把决定提前到装的时候
- Inno 侧的编码判据与 PowerShell 侧相反：`.iss` 里的中文消息安全，Inno Setup 6.7 即使无 BOM 也按 UTF-8 解码脚本（编译期用 ISPP `SaveStringToFile` 把中文字面量落盘、逐字节对过 UTF-8 输出）。合法 flag 集与 `[Tasks]` 默认勾选这些细节报错时只有一行 `Parameter "Flags" includes an unknown flag`，改一处编一次是唯一稳的排法。`PrivilegesRequired=lowest` 全程不提权；排障用 `/VERYSILENT /DIR="<目录>" /LOG=<文件>`，日志里 `Run entry` / `Parameters` / `Process exit code` 三行就是向导交给 powershell 的确切命令行。注意 setup.exe 会把自己复制成 `<name>.tmp` 再跑向导，父进程先退出，所以"双击没反应"要看的是 `.tmp` 那个进程
- 装完的入口面由安装器落四样：`~/.local/bin` 里的命令桩（该目录不在 PATH 上时补这一条，并把"是我们加的"记进指针）、安装根指针文件、桌面与开始菜单图标、`HKCU\...\CurrentVersion\Uninstall\Negi` 卸载项。桩不硬编码安装根，按 `G3KU_HOME` → 指针文件解析，因为 `--Dir` 是任意位置；每次安装无条件覆写桩与指针，桩体不含版本号，所以升级不需要重写它
- 图标与命令桩都不自己启动服务：`.lnk` 的 TargetPath 是 `.venv\Scripts\pythonw.exe`（不分配控制台，所以没有黑窗闪现），参数是 `negi_bootstrap.py start`（`start` 本身就 detach）。**bootstrap 自己起的每一个探测子进程都必须带 `CREATE_NO_WINDOW`**：pythonw 没有控制台可继承，解释器版本探测、依赖探测、`uv sync` 每个都会另开一个黑窗，用户看到的就是连弹几下才出浏览器；真终端下加这个标记不改变行为（子进程仍继承父控制台）。`negi` / 手敲命令的窗口因此保持原样——它本来就是用户自己的终端，bootstrap 只是打印完就退出，不隐藏它。**这条约束不止于 bootstrap**：图标起的 web 进程同样没有控制台可继承，它自己 spawn 的任何东西（版本探测的 `git ls-remote`、托管 worker、工具子进程）不带 `CREATE_NO_WINDOW` 就会在服务起来后另开一个黑窗，看起来像"静默启动没做到"。快捷方式图标用仓库里已跟踪的 `g3ku/web/frontend/favicon.ico` 复制一份到 `%LOCALAPPDATA%\Negi`，安装期不做图像转换；不放安装根是因为 `-Upgrade` 按顶层覆盖那棵树
- 取码一律先检出到临时目录再逐顶层合并。`git clone` 对着非空目录直接 fatal，而"保留数据"的卸载留下的正是非空目录，所以认领不是可选项；目标里已有 `.g3ku/config.json` 时报一句认领（配置、会话与密钥沿用），不重新初始化项目
- 卸载入口是 `uninstall.ps1` / `uninstall.sh`（随发布 checkout 分发，注册表 `UninstallString` 指向它）。顺序固定：先请求优雅退出并确认进程真的退干净，退不干净就中止、一个文件都不删（`.venv` 在跑时文件也删不动）；然后询问保留还是全部删除，默认保留。删除判据是两个独立事实——数据根里要有 Negi 的配置或密钥信封，还要有运行时目录之一——主目录、盘根与读不出标记的路径一律拒绝。uv 与它代管的 Python 属共享工具链，永不删
- 隐藏窗口的启动没有控制台所有者：停止服务只有网页「设置」里的退出按钮（`org_graph.html` → `POST /api/bootstrap/exit`，见下节与 `web-and-admin.md`「Local Startup And Launcher Contract」）。界面进不去时的兜底仍是 `start-negi.*` 的"先优雅退出再强杀"
- `install.ps1` **不带 UTF-8 BOM，且整份文件纯 ASCII**。一行指令是 `iwr ... | iex`：PowerShell 5.1 把下载到的字节按客户端码页解成文本再交给 `iex`，BOM 于是变成 `#requires` 之前的垃圾字符，`param(` 不是首条语句，安装会在第一个参数行报 `InvalidLeftHandSide`。而**无 BOM 的中文注释同样致命**：PS 5.1 按机器 ANSI 码页读脚本文件，中文解码后可能以反引号（续行符）结尾，把下一条语句静默吞进注释——实测 `Install-CodeInto` 的 `$stageRoot` 赋值就这么没了，报 `VariableIsUndefined`。注意这条**零个解析错误**，只有真跑一遍或保持 ASCII 才拦得住。`uninstall.ps1` 相反——它只经 `-File` 执行且正文里有中文对话框，必须带 BOM，否则 PS 5.1 按 ANSI 解会把字符串解析打断。这几条由 `tests/resources/test_start_scripts_and_readme.py` 钉住
- 镜像源不设安装器参数：uv 直接读 `UV_DEFAULT_INDEX` 与 `UV_PYTHON_INSTALL_MIRROR` 环境变量
- uv 的定位在三个载体里是同一判据：先看 PATH（`Get-Command uv` / `command -v uv` / `shutil.which("uv")`），落空再按 `~/.local/bin/uv[.exe]`、`~/.cargo/bin/uv[.exe]` 两个安装位兜底。兜底不是可选：astral 安装器把 uv 写进 `~/.local/bin` 并追加的是**用户 PATH**，而 PATH 变更对已经开着的终端不可见，首启恰好落在这个窗口里；只认 PATH 会让新装设备停在 `No usable package installer`——`negi_bootstrap.py` 的 `_ensure_project_installed` 这时想走 pip 兜底也走不通，因为 `.venv` 正是 uv 建的、里面没有 pip。marker（`.venv/.g3ku_bootstrap_complete`）记的是 `pyproject.toml` 指纹，所以每次抬版本号后的第一次启动都会再进这条一次
- 安装完成的终点是项目口令设置页，不是可用系统（解锁合同见 `config-and-models.md`「Deployment Unlock Contract」）
- 不带 `-Upgrade` 时对已存在的目录是**幂等不动代码**（只补环境与启动）。升级是显式动作：git 检出走 `fetch --depth 1` + `checkout --detach FETCH_HEAD`，无 git 的源码包安装走"下归档 + 逐顶层覆盖"，两条路都只换代码，`.venv/` 与 `.g3ku/` 保留
- 升级前置校验读的是**内容差异**：`git diff --quiet --ignore-cr-at-eol`（工作树对索引）与 `git diff --cached --quiet --ignore-cr-at-eol`（索引对 HEAD）。判出内容改动时**先 `git stash push` 再换代码、换完 `git stash pop` 放回**：跟踪文件的本地改动既不被新版本覆盖，也不会把升级永久拒死——skills 的 `trigger.keywords` 这类自定义只能改跟踪文件（没有 untracked 覆盖机制），落在这条校验上就是"点多少次都升不动"。stash 会写提交对象、需要身份，而刚装好的设备往往没配过 `user.name/user.email`，所以脚本内联给一个 `negi-upgrade` 身份：升级不卡在 git 配置上。pop 冲突（新版本动过同一批行）时丢弃这次合并结果、改动留在 stash 里，代码树必须保持干净——带冲突标记的文件交给服务就是起不来；取码失败同样先把改动放回去再报错，失败路径不把改动藏进 stash。判据不落在 `git status --porcelain` 上：autocrlf 设备上 git 按 CRLF 检出而运行期写回 LF，`status` 会把这种"字节相同、行尾不同"报成已修改，一台没人碰过的设备就此永久升不动。未跟踪项不算脏 —— 装过 skill、桥接产物的设备必然有未跟踪文件，把它们算进去会让每次升级都白跑一次 stash。运行期状态一律落在数据根，不改跟踪文件——前端第三方资产的探测状态就在 `<data root>/vendor-updates.json`，见 `web-and-admin.md`「Frontend Vendor Asset Update Contract」
- 两种取码方式**不可混用**：把源码包盖在 git 检出上会按归档的行尾重刷整棵树，此后 `git checkout` 与 `git diff` 的读数都不反映 git 自己写下的形态，升级前置校验也就失去依据。因此"有 `.git` 但 git 不可用"时报错，而不是退化成覆盖
- 版本识别通道是 `git ls-remote --tags origin`，只接受 `refs/tags/vX.Y.Z` 形状（`backup/*` 这类路径标签与 peeled `^{}` 行都按形状过滤掉），与 `g3ku/__init__.py` 的 `__version__` 比对，结果只落在 `negi status` 的 `Release:` 行。约束：只读不外发、超时 2 秒、失败即整行不出现（离线设备不得显示"已是最新"）
- 发版动作 = 打**附注**标签（`git tag -a`，正文写这一版发出去的内容）+ 同步 `pyproject.toml` 与 `g3ku/__init__.py` 两处版本号 + 跑 `uv lock`（`uv.lock` 里钉着 `g3ku-ai` 自身版本，漏这一步会让所有 `uv sync --frozen` 的安装与升级直接失败）+ 抬 `install.ps1` / `install.sh` 里的 `$Ref` / `REF` 钉值到新标签（README 的一行命令不含标签号，不在此列）。标签推上去后 `.github/workflows/release.yml` 自动建 release 并把 `install.ps1` / `install.sh` 挂成资产，不需要手工建；发布页正文取自**标签附注**，工作流按 `/git/ref/tags/<name>` 拿到对象再读 `/git/tags/<sha>` 的 message，末尾补一句英文说明这些资产是什么。走 API 而不是 `git tag -l --format=%(contents)` 的前提是：runner 本地那个 tag 引用可能被剥成 commit，届时 git 侧读到的正文是提交消息而不是发布说明；轻量标签按提交消息回落。该工作流以**标签所在提交**里的版本运行，所以改 `release.yml` 的效果落在下一个标签；重复推同一标签会同时刷新资产与正文。一行命令与发布页通道读的都是这两个脚本资产：**标签没推、或 CI 还没挂上资产时，`releases/latest` 给的就是上一个发布**——这是一行命令不会因发版中途而失效的原因，也是排障"用户装到的是旧版"时的第一个读数点。
- 取码与查版本只认**当前克隆的 `origin`**：脚本里的仓库名常量只服务首次 `git clone` 与无 git 的源码包兜底，而 `ls-remote` / `fetch` 打的是设备自己 `.git/config` 里的 remote。仓库改名之后已装设备照常检查与升级，不需要通知使用者；这条通道的前提是旧仓库名不被重新占用——GitHub 在名字被建走的那一刻停止重定向，而失败的表现是 `Release:` 行不出现、设置行不点亮，不是一句报错，设备会静默停在旧版本上

### 自动检查新版本与「重启并更新」

已安装的设备会自己查新版本，但**永不自动换代码**：换代码永远是用户在网页上点一次的动作。

- 通道仍是一条：`git ls-remote --tags origin` 取最高 `refs/tags/vX.Y.Z`（`g3ku/update_check.py`）。节拍借 web 进程已有的 60 秒对账循环（`g3ku/shells/web.py` 的 `UPDATE_CHECK_EVERY_N_CYCLES = 300`），间隔由台账 `.g3ku/update-check.json` 的 `checked_at` 把关，所以循环每轮都被调、真正联网每 5 小时一次。开关与间隔在 `config.update_check`（`enabled` / `interval_hours`）。
- 项目锁定态不检查也不提醒：这条循环要运行时的消息总线存在才起，因此没解锁的设备不会往外发请求。
- 台账三态必须分清：**没有文件 = 从没查过**、`error` 非空 = 查失败（`remote_unreachable`）、只有 `newer=true` 才允许点亮。把前两种渲染成"已是最新"是要防的缺陷形态——离线设备会变得和最新版本无法区分。命令行横幅与网页红点都遵守这一条。
- 「重启并更新」的顺序不可调换：`POST /api/update/apply` 只负责踢起一个脱离子进程（`g3ku/update_apply.py`），执行体走现成的 `POST /api/bootstrap/exit` 请求优雅退出（"有在跑的活未确认"的 409 因此只有一份实现），等端口释放后才跑 `install -Upgrade`，完事再重新拉起；**升级失败也要把旧版本拉回来**，绝不把设备留在无服务状态。原因：运行中的解释器在源码被替换的窗口里 import 到半截文件会造成阶段死锁。没点名版本时，升级目标取自**这一次现场查到的 tag**，不取台账里的旧值（重启后台账要等满检查间隔才刷新，照旧值动手会把设备升到比当前最新发布更旧的版本上）；契约见 `web-and-admin.md`「Update Notification And Restart-And-Upgrade Contract」。
- 执行体的两个参数都是安全边界，改动前先读：端口由调用方显式传入、猜不到就中止（回落到默认端口会去关同机另一个实例）；`install` 必须带 `-Dir/--dir` 指向本项目根（漏了会退回脚本默认路径，结果是"升级了另一个目录、重启未变的代码"，这条是彩排时实测出来的）。
- 释放等待只在端口真的空下来之后才动代码；任何一次 `exit_refused_*` 或 `port still busy` 都是**不碰代码**直接退出。降级安装（新 config 配旧代码）会撞上 `Config` 的 `extra=forbid`，服务起不来属预期，不是 apply 车道的问题。
- 全程留痕在 `.g3ku/logs/update-apply.log`，**这个文件里只有执行体自己的行**：安装脚本的进度实时续写进来，而升级完重新拉起的那个 web 进程接到 `.g3ku/logs/console.log`（与 bootstrap 给 web 选的落点一致）——两个流各走各的文件，否则几万行访问日志会把下面的判读锚点埋掉。判读锚点：`exit_refused_409` = 用户没确认暂停；`port still busy` = 服务没退干净、代码未动；`upgrade still running at Ns` = 子进程还在跑（多半在下载），`upgrade timed out after Ns; killed` = 超过 20 分钟被杀；`relaunching the previous version` = 升级失败但服务已恢复；`exit_unreachable` = 关停请求无人处理、服务事件循环已停摆（判停摆：端口仍 LISTENING 但 `/api/*` 全超时），QQ 官方桥即已知成因，见 `external-agent-api.md`「运行入口是硬约束」。安装脚本续写进这份日志的 `stashing them as negi-upgrade-<utc>` = 跟踪文件有本地改动、已存进 stash 并在换码后放回；`local edits restored` = 放回成功；`local edits conflict with <ref>; kept in git stash as negi-upgrade-<utc>` = 新版本动过同一批行，改动仍在 stash 里（按标签 `git stash list` 找），代码已按新版本跑起来；`could not stash the local edits` = stash 本身失败，代码未动、服务已按原版本拉回。重复点击会被 `409 apply_in_flight` 拒掉（闸门与成功侧的 toast/自动刷新归 `web-and-admin.md`「Update Notification And Restart-And-Upgrade Contract」）；两个执行体重叠跑过一次，代价是服务被顶两次、空窗约 2 分钟，且两份缓冲写句柄会在本文件里互相盖行。
- 排障时先看 `.g3ku/update-apply-result.json`，再看日志：执行体每走一步都重写它（`stage` 依次 `requesting_exit / waiting_exit / upgrading / relaunching`，带 `elapsed_seconds`），中间步骤的 `outcome` 恒为 `started`（单飞闸门判的就是这个值，把阶段写成新终态会让用户在升级途中再踢一次执行体）。用户报"点了没反应"时，这份文件一句话就能说清停在哪一步、多久之前；日志要跨两个文件对时间戳。
- 一个模型都没配的设备上 `get_agent()` 构造不出运行时，`_running_work_snapshot` 因此**按空快照回答**并带 `runtime_unavailable` 溯源键，而不是抛 500 —— 退出、`start-negi` 的优雅重启与「重启并更新」共用这个端点，500 会让这类设备既关不掉自己也升不了级。锁状态判定不变，未解锁仍然 423。
- 执行体收子进程输出统一按 UTF-8 解，不看系统 ANSI 码页：中文 Windows 上 gbk 解不开安装脚本写出的中文进度，读线程抛 `UnicodeDecodeError` 会让整段升级输出丢失，判据随之消失。
- 前端侧的端点与三态渲染契约归 `web-and-admin.md`「Update Notification And Restart-And-Upgrade Contract」。

### 首选一键启动脚本

入口按"越靠前越不需要知道细节"排：任意目录的 `negi`（= `negi start`）→ 桌面/开始菜单图标（同一条 `start --window`，开原生窗口）→ 安装目录里的 `start-negi.*`（带参数的显式启动）→ `negi web` / `negi worker`（拆开验证某一环节时用）。

桌面窗口壳（`g3ku/shells/desktop.py`，命令 `negi window`）只是**指向已启动服务的一扇 WebView2 窗**：它不承载服务、不拥有服务生命周期，关窗不停服务，再点一次图标就重开一扇。承载用系统自带的 WebView2 运行时（Win11 预装），不打包浏览器内核；依赖 `pywebview` 带 `sys_platform == 'win32'` 标记，Linux / macOS 的安装不会被它和它的 .NET 互操作依赖拖进去。两个必须知道的点：`http://127.0.0.1` 在 WebView2 里是安全上下文，所以 `navigator.mediaDevices` 存在（实测 `secure:true / mediaDevices:true`）；但 pywebview 不注册 `PermissionRequested`，麦克风要在 `loaded` 回调里补挂——该回调跑在 UI 线程，而 `CoreWebView2` 只允许在 UI 线程访问，在别的线程上取它会抛 `InvalidOperationException`。界面自己的媒体（图片、附件、签名正文）不在这里开新窗：它们在程序内显示或下载，合同归 `web-and-admin.md`「In-App Media Viewer And Download Lane」，只有真外链经 pywebview 的 `NewWindowRequested` 交给系统浏览器。下载必须由壳打开：`ALLOW_DOWNLOADS` 默认是关的，`DownloadStarting` 会被直接 `args.Cancel`，而那次取消不会传回页面——界面里唯一那颗下载（导出配置包）因此既没落点也照样弹成功 toast，看起来就是"程序静默"。`run_desktop_shell` 在建窗前把它置真，之后每次下载都是这个窗口自己的「另存为」对话框。判读：窗口里点下载没反应＝这枚开关没了，先别去怀疑后端没出文件。窗口按 75% 打开（`DEFAULT_ZOOM_FACTOR`，走 WebView2 的 `ZoomFactor`，每次 `loaded` 重设一次，所以刷新后仍是 75%），界面里的字号档位是另一个旋钮，只动阅读面（合同见 `web-and-admin.md`「Theme Persistence」）；启动行里那句「窗口缩放 75%」就是它接上回执，若写成「窗口缩放未生效」说明没拿到 WebView2 宿主控件，窗口照常能开但按 100% 显示。开不了壳（非 Windows、依赖缺失、运行时异常）时 `start --window` 一律回落到浏览器，不会点了没反应。

- Windows: `start-negi.cmd`（不经过 PowerShell）；`.\start-negi.ps1` 是同一语义的糖
- Linux / macOS: `./start-negi.sh`

适合：

- 普通用户快速启动项目
- 本地单机直接拉起 Web 与托管 worker
- 让脚本自动处理 `.venv`、依赖安装、基础配置兜底与已有托管进程重启

维护上要记住：

- 这些入口脚本是普通用户首选入口
- 回收既有进程由**解释器**执行：`negi_bootstrap.reap_existing_runtime_processes`，`start-negi.cmd` / `start-negi.ps1` / `negi web` 共用这一处；`start-negi.sh` 保留自己的 POSIX 实现（同一端点、同一超时、同样先优雅后强杀）。`negi.ps1` / `negi.sh` / `install.ps1` / `install.sh` 自己不杀进程。回收不能挂在 `.ps1` 上：`.ps1` 可能在执行前就被整份拒掉（实测：带低完整性标签的未签名脚本，即使策略是 RemoteSigned 也报 `UnauthorizedAccess`），入口一被拦，回收逻辑也跟着消失；`.cmd` 直接 exec `negi_bootstrap.py`，不经过 PowerShell，所以它在这类环境里仍然起得来。三个必须知道的判据：
  - **不能把启动器自己算进名单**：`.cmd` 用绝对路径调 `negi_bootstrap.py`，启动器自己的命令行同时命中「checkout 根」与 `negi_bootstrap.py web`。要排除的是一对 PID（venv 的 `python.exe` 是重定向器，cmdline 命中的是父进程，真正执行代码的是它的子进程）；漏掉任何一个，强杀那一步都会打死回收者本身——入口以 exit 1 静默消失、一个字都不打印，看起来像"脚本起不来"。
  - **优雅退出的端口要按候选找**：回收按 checkout 收、不按端口收。操作者这次换了 `-Port` 时旧实例还听在旧端口上，只打目标端口等于没发请求、回收会退化成强杀（异常中断）。候选顺序是目标端口 → 旧实例记下的端口（`.g3ku/internal-callback.json` 里的回调地址，web 启动时由 launcher 写入）→ 配置端口 → 默认 `18790`。
  - **强杀以"杀完还在不在"判定**：`taskkill /T` 收父进程时子进程已经跟着走了，排在后面的子 PID 再杀一次会报"找不到进程"；只有杀完仍活着才值得报一行失败。
  端口是否被占的权威判据仍是 `Get-NetTCPConnection -LocalPort 18790`：`netstat` 里状态列在地址之后，用 `LISTENING.*<port>` 这种模式永远匹配不到。
- 它们会在启动前默认清理当前仓库下已有的 negi web / worker 进程，但**先请求优雅退出**：向 `POST /api/bootstrap/exit`（`pause_running_work=true`）发起请求并等待运行时把全部会话与任务持久化暂停、自行退出；只有在接口不可达、返回失败或等待超时（约 40 秒）时才回退到强制杀进程。因此“重跑启动脚本”对运行中的工作是一次优雅暂停而不是异常中断，下次启动会自动恢复（生命周期合同见 `main-task-runtime.md`「Graceful Shutdown Pause and Startup Auto-Resume」）
- 强制回退路径（强杀）对应的是异常中断：下次启动任务走恢复清洗，任务卡片会以 toast 提示「本任务遇到异常停止」；toast 可点击关闭（UI 合同见 `web-and-admin.md`「Task Recovery Notice UI Contract」）
- 它们最终都进入 `negi_bootstrap.py` 的 web 分支，由它起 `python -m g3ku web`
- 当脚本使用 reload 模式时，Web 侧自动托管 worker 会关闭；这时要单独运行 `negi worker`
- `negi.cmd` / `negi.ps1` / `negi.sh` 是 CLI 透传包装；无参调用走 `start`：先探端口，已在跑就只打印地址并打开浏览器（**不打断正在工作的实例**），没在跑才启动服务，等就绪后打开浏览器。`start` **一律后台化**（`CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`，与「重启并更新」的重拉起同一形态），所以起完关掉那个终端窗口项目照跑，图标与手敲 `negi` 是同一条语义。**后台化不许用 `DETACHED_PROCESS`**：uv 托管的 venv 里 `.venv\Scripts\python.exe` 是重定向器，真解释器是它的**子进程**（实测启动 PID ≠ 运行 PID）；父进程完全没有控制台时 Windows 会给那个子进程新建一个**可见**控制台，而它就是服务本身——用户关掉"多出来的黑窗"等于杀掉 Negi。`CREATE_NO_WINDOW` 下真解释器根本没有控制台（实测 `GetConsoleWindow()` 返回 0），既看不见也关不掉。想用自己的 Ctrl+C 停服务走显式 `web`（前台、控制台附着；实时输出本来就一直在 `console.log`，终端不滚日志）。三条路最终都是 `python -m g3ku web`，所以进程判据与单实例锁的归属判断都不变。任何入口的 web 启动都会在终端报告结果：成功横幅带 URL，失败横幅带子进程退出码和 `.g3ku/logs/console.log` 指引。横幅的"成功"只代表监听端口已生效；运行时是否还在预热看 `/api/bootstrap/status` 的 `runtime_bootstrapping`（合同见 `web-and-admin.md`「Local Startup And Launcher Contract」）。探针盯的端口是"配置端口与 `--port` 合并后的那一个"，只读配置会让非默认端口在服务正常时打印假失败
- web 启动在拿单实例锁（`.g3ku/start.lock`）之前会先自愈：杀掉本工作区残留的 negi web 服务进程（`-m g3ku web` 或 `-c ...run_web_server_entrypoint...` 形态，按 venv python 路径或进程 cwd 归属本工作区）。因此“端口/锁被占”不是永久失败：再次启动会替换残留实例；若报错里 `pid=unknown`（持有者在加锁与写元数据之间被杀），用 `netstat` 查 web 端口定位占用者

### CLI

- `negi agent -m "Hello"`

适合验证：

- 配置是否正确
- CEO 模型是否可用
- 同步会话路径是否正常

### Web

- `negi web`

适合验证：

- Web UI
- main task runtime
- heartbeat

### Worker

- `negi worker`

适合验证：

- 后台 task worker 路径
- Web/runtime 分离执行场景

## 2. 新接手后的第一轮检查

建议按下面顺序做环境确认：

1. 检查 `.g3ku/config.json`
2. 运行 `negi status`
3. 运行一次 `negi agent -m "test"`
4. 在任意目录输入 `negi` 启动项目（开发机上装过就有这条命令；没有时回退到安装目录里的 `start-negi.cmd` / `.\start-negi.ps1` / `./start-negi.sh`）
5. 确认 Web API、任务面板、模型配置页能正常返回

如果你要拆开验证启动链路，或排查“是脚本包装层问题还是 Web/runtime 本身问题”，再回退到手动运行 `negi web` / `negi worker`。

当前 `negi status` 的记忆区块应按 queued Markdown runtime 理解：

- 它会显示 `Memory Notebook`、`Memory Notes Dir`、`Memory Queue`、`Memory Ops Log`
- `negi status` 的 `Memory Notebook` 一行只说明笔记文件在不在，不是长期记忆健康指标；健康判断走 `negi memory`，它的命令面是 `current` / `queue` / `flush` / `uncompress` / `doctor` / `reconcile-notes` / `reformat` / `import-legacy` / `cleanup-legacy`

## 3. 关键状态文件与目录

日常排障时，先熟悉这些目录：

目录分两个根：

- **安装根**：进程 cwd（`negi web` 启动时固定为代码检出目录）。承载代码与 `skills/`、`tools/`、`externaltools/`，配置和密钥材料——`.g3ku/config.json`、`.g3ku/llm-config/`（主密钥信封与 `auto-unlock.key`）、`.g3ku/secret-realms/`、`resources.state.json`、`resource-locks/`、`start.lock`、`internal-callback.json`——以及按 `config.workspace_path`（`agents.defaults.workspace`，默认 `.`）或 `Path.cwd()` 解析的其余状态：`memory/`、`sessions/`、`.g3ku/cron/`、`.g3ku/errors/`、`.g3ku/audit.jsonl`、`.g3ku/memory-requests/`、`.g3ku/cache/`、`.g3ku/logs/`、`.g3ku/external-sessions/`、`.g3ku/external-uploads/`、`.g3ku/external-outbox/`。换数据根不带动这些。密钥材料的滚动副本在 `.g3ku/llm-config-backups/`（按 UTC 时间戳分代，保留最近 12 代），凭据动作的留证流水在 `.g3ku/security-events.jsonl`——两者都不进 git，也不随数据根迁移。
- **数据根**：只收体积数据与临时内容，判据是解析入口——经 `g3ku/deployment/data_root.py` 的 `data_root()` / `data_g3ku_path()` / `data_work_path()` / `resolve_data_path()` 落盘的那几类：`.g3ku/main-runtime/`（任务库、artifacts、deliverables、governance 库、`managed-worker.log`）、其余 `.g3ku/web-ceo-*` sidecar、`.g3ku/stt/`、`.g3ku/tmp/`（运行时自身的 scratch，如提示词留痕）、`temp/`（任务级 `temp/tasks/`、会话级 `temp/ceo/`、写失败正文的 `landing-park/`，以及 `exec` 交给子进程的 `TMP`/`TEMP`/`TMPDIR`）。新增挂载点不走这四个函数之一就会静默留在安装根。

两半都写成 `.g3ku/<相对路径>` 的形状，但根不同，因此**自定义过数据目录的安装在盘上有两个 `.g3ku`**：`<安装根>/.g3ku/` 与 `<数据根>/.g3ku/`。数据根那一侧同样带 `.g3ku` 这一层（`<数据根>/.g3ku/main-runtime/`，不是 `<数据根>/main-runtime/`）；`temp/` 则直接挂数据根。未配置时数据根等于安装根，两个 `.g3ku` 合一，也就是历史布局本身，换锚因此不产生迁移、也不改变相对布局。

命名陷阱：`g3ku.utils.helpers.get_data_path()` 与 CLI 侧的 `get_data_dir()` 返回 `Path.cwd() / ".g3ku"`，属**安装根**，跟数据根无关（`.g3ku/cron/jobs.json` 与 `.g3ku/update-check.json` 因此留在安装根）。数据根只有 `g3ku/deployment/data_root.py` 一个来源。

数据根由 `g3ku/deployment/data_root.py` 解析，进程内缓存一次，顺序是：

1. 环境变量 `G3KU_DATA_DIR`
2. 安装根下的 `.g3ku/data-root.json` 的 `data_dir` 字段（首次初始化时操作员选定的目录）
3. 进程 cwd

指针文件刻意留在安装根一侧：它要在解锁之前就读得到，而主密钥信封、配置与导出/导入合同都不随数据根移动——`config_bundle` 的包内路径仍以 `.g3ku/` 为相对根。

想知道一台机器上真正的数据目录，按可得性取一条：

- 进程在跑：`GET /api/bootstrap/status` 的 `data_root` 段（`data_root`、`source`、`default_root`、`pointer_path`），锁屏状态下同样可读。
- 进程没跑：读 `<安装根>/.g3ku/data-root.json` 的 `data_dir`；该文件缺失再看 `G3KU_DATA_DIR`；两者都空即 cwd。
- `negi status` 不打印这一项。

日志因此分两半，别在数据根下等 `console.log`：`.g3ku/logs/console.log` 与 `.g3ku/logs/update-apply.log` 由启动器按代码检出目录锚定（`negi_bootstrap.py`、`g3ku/update_apply.py` 用 `PROJECT_ROOT`），与数据根无关；`.g3ku/main-runtime/managed-worker.log` 跟数据根。

`console.log` 的大小由两道机制分别管，别指望其中一道代替另一道。启动时 `negi_bootstrap._rotate_runtime_console_log()` 把超过 `RUNTIME_CONSOLE_LOG_MAX_BYTES`（50 MB）的当前代改名成 `console.log.<UTC 时间戳>`，并清掉 `RUNTIME_CONSOLE_LOG_RETENTION_SECONDS`（7 天）以外的旧代。长跑期间由小时级维护循环挂载的 `console_log_cap`（`claim_maintenance_run` 跨进程卡权）在超过上限时就地保留尾部，并落一行 `[log-cap]` 标明丢了多少字节。

改名只在启动那一刻能使上劲：正文写在 bootstrap 交给 web 子进程的 append 句柄上，句柄活着就换不掉这个文件（Windows 上被占用的文件 rename 直接失败），所以运行内只能就地截尾、不能归档。截尾把 seek 落点推到下一个换行，绝不留下半行；随后子进程仍按 append 语义写在新 EOF 之后。loguru 的 `rotation=` 对这份文件不起作用——它按"下一条记录"检查尺寸，而 bootstrap 进程自己不发 loguru 记录，所以只在文件上挂着这句参数等于没有轮转。

其它架构文档里的 `.g3ku/...` 路径不重复标根，一律按本节的两半判读；需要新增挂载点时，先确认它该由哪一侧解析。

- 改数据目录只在 `mode=setup` 的首次初始化入口生效（`POST /api/bootstrap/setup` 的 `data_dir` 字段，非绝对路径、落在 `.g3ku/` 内、包住安装根的候选一律拒绝）。已经建好口令的安装要换根，走人工迁移：停 web 与 worker、搬目录、写指针、再起两个进程。
- 换根只影响此后按数据根重新解析的读写：库里已写死的绝对路径（memory processed 行的 `request_artifact_paths`、节点 artifact 与 `messages_ref` 指向的文件）继续指向旧根。旧根保持原结构在盘上即可读取，改名或清掉会让历史取证失配。
- 数据根解析变化后 web 与托管 worker 都要重启才一致：worker 继承 web 的 cwd 与环境，指针则在两边各自首次解析时读取。
- 指针丢失、被改名或 JSON 损坏都按未配置处理（静默回退 cwd），于是旧数据看起来像空库；`data_root_source()` 的 `default` 就是这个形态，判读方法见上。
- 磁盘水位与自动暂停按数据根所在盘判定（契约见 `runtime-overview.md`「磁盘写保护与治理」），把数据搬到大容量盘后紧急线随之按新盘计算。

以下条目逐项标出所属根：

- `.g3ku/config.json`（安装根）
  项目配置

- `.g3ku/onboarding.json`（安装根）
  初次引导状态：`completed` / `persona_step_done` / 暂存描述 / 照片名 / `persona_request_id` / `photo_session_started_at` / `photo_session_error`。半途引导靠它续跑；`POST /api/onboarding/reset` 写回一份未完成状态，不删 `my_image/` 与人设 note。判据与前端流程见 `web-and-admin.md`「First-Run Onboarding And Persona Contract」。

- `.g3ku/llm-config/`（安装根）
  模型配置仓库（provider/binding record）

- `.g3ku/main-runtime/`（数据根）
  任务运行时 SQLite、artifacts、event history

  其中 SQLite（`runtime.sqlite3` 或配置指定的 store 路径）里除任务/节点/帧等表外还有 `shutdown_pause_registry` 台账表：记录上次优雅关闭时被暂停的会话与任务，重启自动恢复后逐行删除。排查“重启后任务没有自动恢复”先查这张表（语义见 `main-task-runtime.md`「Graceful Shutdown Pause and Startup Auto-Resume」）。

  通过 `negi web` 启动并启用 auto worker 时，这里还应重点关注两份日志：

  - `.g3ku/main-runtime/manual-web-run.log`
    Web 主进程日志
  - `.g3ku/main-runtime/managed-worker.log`
    自动拉起的后台 task worker 日志

- `.g3ku/web-ceo-continuity/`（数据根）
  completed Web CEO session continuity sidecars。重启后继续 completed session、manual pause terminal stop、以及异常中断后的最新 authoritative frontdoor baseline 恢复都先看这里。

- `.g3ku/web-ceo-turn-boundaries/`（数据根）
  每轮连续性边界快照（`<session>/<turn_id>.json.gz`，每会话保留最近 12 份，只由用户轮写入）。用户消息编辑重发/Fork 的唯一截断数据源；契约见 `web-and-admin.md`「Message Edit-Resend And Session Fork」。会话删除时随其它 sidecar 一并清理。

- `.g3ku/external-outbox/`（安装根，与 `.g3ku/external-sessions/`、`.g3ku/external-uploads/` 同侧）
  外部渠道出站消息（主动推送与回合回复）的持久账本（append-only jsonl，msg 记录 + ack tombstone），没有年龄出口：记录 pending 到被某个消费方 ack 或被判不可达为止。排查「渠道端收不到推送或回复」先看这里有无 pending 滞留、以及它属于哪个 `event`；契约、销账方与重放语义详见 `external-agent-api.md`「持久 outbox」。

- `memory/`（安装根）
  记忆目录；排障入口详见本文档「Memory Queue Workflow」章节。

- `memory/notes/note_persona.md`（安装根，位于 `memory/notes/`）
  人设正文的保留 note。它不是 memories 行，不进常驻字数、压缩、退场与行号；孤儿回收、`reconcile-notes` 与 `doctor_report` 都跳过它。改人设走会话里的记忆 agent（`note_edits` / `note_upserts`），也可以在记忆页的 note 编辑面直接改；没有删除入口。

- `my_image/`（工作区根，默认安装根）
  人设照片目录。引导页上传直接写入；聊天里用户发来的「你的照片」由 CEO 用 `filesystem_copy` 复制进来；设置页只读画廊读这里。它不被任何记忆回收清理，删照片就是删文件。载体与通知规则见 `runtime-overview.md`「人设块、人设 note 与身份」。

- `sessions/`（安装根）
  会话持久化数据

- `temp/tasks/`（数据根）
  任务临时目录，每个任务一个 `task_<id>` 子目录；根目录解析与隔离规则见 `runtime-overview.md`「任务侧」。任务进入终态（success/failed）后该目录**默认原样保留**；仅当开启 `main_runtime.disk_guard.terminal_temp_dir_cleanup_enabled`（环境变量 `G3KU_TERMINAL_TEMP_DIR_CLEANUP_ENABLED=1`）时才随终态清理硬删（保留清单与行为契约见 `runtime-overview.md`「磁盘写保护与治理」）。孤儿目录（`runtime.sqlite3` 的 tasks 表中已无对应任务却残留的 `task_*` 目录，含从未走到终态的卡死任务遗留）用 `scripts/cleanup_orphan_task_temp_dirs.py` 清理：默认 dry-run 只报数；`--apply` 删除空孤儿；非空孤儿要么 `--apply --move-non-empty` 移入 `temp/tasks_orphan_backup/`（可逆），要么 `--apply --purge-non-empty` 直接删除（不可逆）。清理脚本以 runtime 库的 `tasks` 表为唯一判据，因此判据缺失即停：库文件不存在、`tasks` 表读不出、或 `--apply` 时 `--tasks-root` 不在 `--runtime-db` 所属安装根之下，脚本一律拒绝执行，不按空任务集继续——空集不等于「没有任务」，按空集判定会把在库任务目录当成孤儿。`--purge-non-empty` 没有交互确认步骤，它的护栏是 `--apply` 与该开关都必须显式给出、缺省 dry-run，以及上面那条拒绝判据。由于终态后目录保留，`temp/tasks/` 会随任务累积增长，磁盘紧张时优先按该脚本处置而非手删。

- `temp/ceo/`（数据根）  CEO/frontdoor 会话级临时目录，每个会话一个 `<safe_session_key>` 子目录（如 `web_ceo-xxxx`）。作为 CEO 会话工具 runtime 的 `task_temp_dir`：`exec` 缺省 cwd 与临时文件规范落点，避免临时产物散落到工作区根目录。它与 `temp/tasks/` 同根，因此设了自定义数据目录的安装里两条 temp 车道一起离开安装根；`temp` 也不在启动建目录的名单里，安装根不会再长出一个空 `temp/`。解析与惰性创建规则见 `runtime-overview.md`「任务侧」。该目录没有任何清理车道：删除会话带走的是连续性快照、请求取证、回合边界与上传目录，不含这里，所以它会随会话数累积增长，处置靠手动清目录。正式交付物禁止以此为最终落点，落点合同见 `tool-and-skill-system.md`「Model Path Anchoring Contract」。

- `temp/persona-thumbs/`（数据根）
  设置页与引导页照片集的缩略图缓存，文件名按源文件路径、大小与 mtime 派生。可随时删除，下次打开画廊重建；原图在 `my_image/`。

- `output/`（数据根）  正式交付物的默认落点，一个交付物一个子目录或文件，由写入方创建（不在启动建目录的名单里）。用户或任务指定了别的位置时按指定的绝对路径写。这条判据的来源是 `tool-and-skill-system.md`「Model Path Anchoring Contract」，模型侧只在 `path_anchor_rule` 一处看到它。

- `.g3ku/tmp/`（数据根）
  运行时自身的 scratch（提示词留痕等），由 `AgentRuntimeEngine` 在构造时按数据根创建。它同时在工具的路径监禁名单里——模型要写临时内容仍被引导到 `temp/` 那一个规范根，这个目录只给运行时自己用。

- `landing-park/`（在上述两个临时根之下）
  写/改的落地校验不通过时，被拒正文在回滚前复制到这里（`park-XXXX/<原名>`），工具结果仍报 error 并给出该绝对路径。它不是新的存储层：随所属临时根一起保留或清理，磁盘紧张时按父目录的既有口径处置即可。行为契约见 `tool-and-skill-system.md`「Model Path Anchoring Contract」。

- 模型相对路径的锚（`agents.defaults.workspace`）
  出厂值 `.` 意味着锚按**启动时的工作目录**确定，并在首次读取时定死：进程之后再 chdir（web 启动就会 `os.chdir(PROJECT_ROOT)`）也不会移动它。因此从二级 worktree 起 worker，模型写的相对路径就落在那棵 worktree 里；同一份配置在主树与 worktree 分别启动会得到两个互不相通的落点面。要跨目录稳定，就在配置里写绝对项目根。合同两条车道都从同一个渲染点出这句话（节点道 `runtime_environment.path_policy`，CEO 前门 `path_anchor` / `path_anchor_rule`），规则本体归 `tool-and-skill-system.md`「Model Path Anchoring Contract」。

## 4. 测试结构

测试以 `tests/` 为主，很多测试按资源/运行时主题分布在：

- `tests/resources/`

从现有命名看，测试重点覆盖：

- main runtime
- CEO/frontdoor
- tool registry 与 hydration
- web runtime
- heartbeat prompt lane
- memory runtime

新增运行时测试时，`MainRuntimeService` 构造要显式传 `workspace_root=tmp_path`，把任务临时目录隔离进 pytest 临时目录；`tests/resources/conftest.py` 的 autouse fixture 对漏传的用例兜底替换 cwd 回退。判断测试是否泄漏了真实工作区的快速办法：跑完测试后 `.venv/Scripts/python.exe scripts/cleanup_orphan_task_temp_dirs.py` 只看空孤儿数量是否增长。

## 5. 推荐的排障顺序

### 会话无回复

先看：

- `g3ku/runtime/session_agent.py`
- `g3ku/runtime/bridge.py`
- Web 场景下再看 `g3ku/heartbeat/session_service.py`

静默相关的三种不同症状分岔查（合同见 `runtime-overview.md`「3.3 静默回复（`silent` 工具）」）：

- **会话回复了本不该再说的旧结果**（典型为"项目一启动就自动回一条几小时前的任务汇报"）：`main-runtime/runtime.sqlite3` 的 `task_terminal_outbox` 里有滞留行被启动或 60s 节拍重放。按 `created_at` 与 `delivered_at` 的差值、以及 `attempts` / `last_error` 判读；`delivery_state='abandoned'` 表示投递上限耗尽后留痕，不被拾取。
- **该静默却把话发出去了**：转录里那一轮有没有 `silent` 的 tool_call 行。文本不参与静默判定，所以模型写出的任何"看起来像哨兵"的句子都只会作为正文投递；唯一例外是整行就是旧哨兵 `[G3KU_SILENT]`，它会被清洗成空正文并落进"无正文即静默"那条道。
- **该回复却整轮没出声**：同一轮的 `silent_reply` 行是否被写进了转录，以及它的 `silent_reason` 是哪一种——工具静默带的是模型写的理由，"模型未给出可见正文" / "旧静默哨兵剥除后无正文" 则是收尾兜底，后者说明模型还在沿用已删除的文本出口（多半是压缩摘要里残留了旧契约措辞）。再查它是否被两条压缩车道裁掉（豁免规则见 `runtime-overview.md`「Frontdoor Context Compression」）。

### 任务没创建或没推进

先看：

- `main/service/runtime_service.py`
- `main/runtime/node_runner.py`

如果问题是“一次性 cron / 定时提醒为什么没有真正创建或没有后续触发”，还要先区分两类情况：

- job 已成功落进 `.g3ku/cron/jobs.json`，但后续没有被消费：再继续查 scheduler / Web 主进程 / session dispatch。按 state 与日志分诊：
  - `lastStatus` 是调度器自己写的分诊码：`running` 只应短暂存在（dispatch 进行中；进程活着时任何退出路径都会收尾，残留 `running` 说明进程在 claim 与 finalize 之间死掉，重启后会自愈为 `interrupted`）；`timeout` = 投递看门狗杀掉了挂起的 dispatch（同一条 ERROR 日志里带挂起任务的 await 链 dump，`dispatch watchdog timeout` 可 grep，这是定位“回合完成后 session.prompt 不返回”类挂起的第一手证据）；`interrupted` = 取消/停机收尾；`error` = handler 异常（`lastError` 带原文）。一次性 `at` job 的 `timeout`/`interrupted` 会按 at-most-once 抑制（禁用而非重发），契约详见 `heartbeat-system.md`「Cron Reminder Contract」
  - 到点没触发且无看门狗日志：grep `still in flight; skipping this tick`（同一 job 上一次 dispatch 仍未了结，后续 tick 主动跳过）与 `timer task died unexpectedly`（定时器任务意外死亡，调度器会延迟自愈重臂；这条出现说明 tick 路径本身炸了，看同段异常栈）
  - 回合慢但没挂死：`session prompt still awaiting`（bridge 慢回合看门狗，同样带 await 链）与 `turn finalize tail slow`（finalize 尾部分阶段耗时）用于区分“真的在干活”与“楔死”
- `cron add` 本身在创建阶段就失败，有两种已知拒绝：
  - 尤其是 `at` 单次提醒，如果真正执行 `add_job()` 时目标时间已经过去，服务会直接拒绝创建并提示 `任务定时已过期，当前时间为<service-local time>，请立即执行或视情况废弃而不要创建过期任务`；这时应优先排查前门/tool 调用延迟、重试、参数错误，而不是先怀疑 scheduler 没触发
  - 同一 session 在同一个 `at` 时间点已存在启用的一次性提醒时，重复注册会被拒绝并提示 `同一会话在 <time> 已存在一次性提醒 (id: …)`（按 `(session_key, at_ms)` 结构化匹配，不看 message 文案）。这是有意的防双触发约束而非 bug：如需改期，先 `remove` 旧 job 再重新创建，或改用其他时间；不要为了绕过它去禁用或删改冲突检查

### 节点投递与冻结/复活排查

- 节点之间的投递只有一条道：信箱 `task_node_notifications`（`send_message`、`manage_child_nodes`、验收驳回、异常暂停升级都写这张表），停在等待档的节点由落到信箱的消息唤醒。节点"迟迟不动"时先读该节点行的通知三态（pending / injected / consumed），不要去翻 `runtime_meta.distribution`——现行代码里它没有读写方，旧库行只是历史数据（解释见 `main-task-runtime.md`「节点信箱与唤醒（现行）」）。
- 单个节点长期 `in_progress` 却既无模型调用也无帧更新，按这个顺序分诊：先确认它是否停在 `waiting_children` / `waiting_explicit` / `waiting_acceptance` 这三档设计态空闲（看门狗刻意把它们排除在停摆判据外），再判父侧有没有重放路径——有则收尸道直接重派，没有才是需要人看的那一类。
- 冻结/复活/收尸类异常在 worker 日志有固定签名，全部落在 `main/runtime/task_actor_service.py`，先 grep 再看 DB：`orphan node re-dispatched at task pickup (recoverable via parent replay)`（任务拾取时认定父侧重放路径可用，就地重派）；`orphan node parked as candidate`（无人驱动又无重放路径 ⇒ **可见地暂停**并写一条 `pause_reason='error'` 的错误行；收尸器不签发终态，错误行本身正好是 `task_node_error` 心跳道的选取条件，所以这里没有第二套通报机制）；`release: dispatch future was cancelled while node non-terminal (rebuilding entry)` / `release: dispatch future already failed with stranded exception (rebuilding entry)`（释放路径发现执行体缺席或搁浅，重建入口）；`release verification: node still frozen after release, re-resuming once` 紧跟 `release verification failed: node wedged after re-resume (needs attention)`（释放后未复活的二段核对，第二条 ERROR 就是要人介入的信号）；`node entry crashed; exception parked on dispatch future`（执行体异常留在 future 上，由下一次释放核对接手）；`node resume triage deferred (future resolved while coroutine still runs)`（future 已结但协程仍在收尾，resume 决断推迟一拍）；`final acceptance round re-dispatched after interruption`（验收轮被中断后按持久状态重派，不依赖父进程还活着）；`wait-for cycle broken: task={} cycle={}`（等待图成环被拆开）。
- 父节点被看门狗判停不等于崩溃：三条结构性判据 `repeated_identical_tool_call` / `tool_error_streak` / `no_new_events`（`main/runtime/node_watchdog.py`）只把节点标成异常暂停，不杀正在进行的回合。在飞的模型回合（等闸、重试、流式返回）与正在跑的工具一样不算失速；只有请求层报告 `waiting_upstream` 且静默已满一个失速窗口才落 `no_new_events`。非根节点的异常暂停只经父节点信箱升级（`child_abnormal_pause`，high 优先级、必唤醒），只有根节点的异常暂停惊动会话 agent；父节点不处理时，前端「暂停·异常」是唯一可见面。
- 信箱投递、看门狗判据与恢复重放的合同字段详见 `main-task-runtime.md`「节点信箱与唤醒（现行）」与「Node-Level Pause and Recovery」。

如果任务已经创建，但表现为“响应明显变慢”“长时间停在 `model.chat.await_response`”或“前端只看到 task-event 在刷”，优先同时对照：

- `.g3ku/main-runtime/manual-web-run.log`
- `.g3ku/main-runtime/managed-worker.log`

其中 worker 日志更关键，因为 provider 请求、SSE 诊断和模型超时通常发生在独立的 worker 进程里。

排查时优先搜索：

- `responses stream diagnostics`
- `openai_chat stream diagnostics` / `responses stream diagnostics`（前缀就是 provider 车道名，只有这两档）
- `Error calling Responses API`
- `model attempt timeout`

Provider retry troubleshooting note:

- 节点 `react_loop` 与 CEO/frontdoor `call_model` 的外层 provider-exhaustion 自动重试是有限次：在底层 model/key/fallback chain 已经 exhausted 之后，当前 round 最多再做 3 次外层重试。
- 因此如果你看到 `Error calling Responses API` 连续刷屏，但任务/会话迟迟不结束，不要先假设“它还在无限自动重试”。先确认这些日志是否真的属于同一个 task/session。
- 当前 task 如果已经落到 `is_paused=true` / `pause_requested=true`，那说明另一个控制动作已经介入了；这和 provider retry 本身是两条不同的因果链。排查时应同时看 `task_commands` 是否出现 `pause_task`，而不是只盯着 provider 日志。

并结合 provider 超时边界判断“慢”是不是异常；超时语义详见 `runtime-overview.md`「Chat provider 超时与重试边界」。

日志里优先看这些字段：

- `first_chunk_received_ms`
- `first_text_delta_received_ms`
- `chunk_count`
- `last_chunk_kind`
- `stream_completed_ms` / `stream_failed_ms`

### spawn 轮次过早完成或子节点被意外 supersede

如果事件流里出现：父节点在验收节点仍为 `in_progress` 时提前进入 `before_model`；同一轮内出现第二次 `spawn_child_nodes` 且旧轮子节点被打上 `superseded by newer spawn round`；或帧里出现 `entry.status=success + acceptance.status=in_progress` 这种无效组合——按下面顺序排查。

先看帧诊断字段（`_resume_waiting_children_turn_if_needed` 在恢复前写入当前帧）：

- `spawn_recovery_mode`：`wait_existing_pipeline`（有存活子/验收节点，应等待现有管线）、`rematerialize_round`、`replay_completed_result`、`no_active_round`；
- `spawn_recovery_round_id` / `spawn_recovery_round_ids` / `spawn_recovery_active_entry_indexes` / `spawn_recovery_active_node_ids`：定位仍活跃的 round、entry 与绑定节点。

再在 worker 日志里搜 `[g3ku spawn diagnostic]` 开头的警告（列在这里的标题都是故障形状；`title=spawn_round_created_without_waiting` 是 info，它只标记"批次已创建、按设计不阻塞"，每个派发批次固定一行）：

- `title=spawn_blocked_review_over_materialized_round`：已物化轮被重新评审且判为 blocked，但运行时拒绝让其终结仍存活的子/验收节点（只写 `review_decision=blocked`，不把管线状态写成 `success`）；
- `title=spawn_entry_terminal_with_live_node`：entry 状态已是 terminal/blocked，但绑定节点仍非终态，属无效状态组合信号。
- `title=spawn_pause_reached_settlement_lane`：某条车道把子节点的暂停当成可结清的异常送到结算面（正常形态是子节点派发 future 保持 pending、父管线原地停等）。命中说明该轮会被父节点读成"子节点失败"，而节点其实可被 resume；核对是哪个调用点绕开了派发 entry。
- `title=spawn_supersede_left_to_owner`：新轮清扫时本进程没有这些节点的派发 entry，于是只上报、不落终态——`detail` 带 `left_node_id` / `kind` / `depth` / 清扫前的 `status` / 取代原因。读法：这条不是"节点失败了"，是"本进程说不出谁在驱动它"。要判它到底在别处跑着还是已经停了，拿该节点在 `task_model_calls` 的最后一格时间戳与 `task_commands` 里的 `resume_node` 行对照。处置由持有判决权的一方签发（`resume_node`、或按 `main-task-runtime.md`「Node-Level Pause and Recovery」落 `canceled`），运行时不代签。子树里真有执行体的那批由取消方按同一条 reason 落定，不会出现在这里。
- `title=spawn_round_fault_holds_for_live_nodes`：派生轮自身抛异常（典型是记账写库撞上存储故障），而本轮仍绑着未收敛的节点。运行时把这条轮次留在等待上而不是给模型一条 `Error executing spawn_child_nodes`。`detail` 的三个计数就是判据本身：`driven`＝派发器说有人在跑、`converged`＝节点行已终态、`unknown`＝读不到行，或持有者已停止驱动而行仍非终态；再带 `fault=`（异常因果链，不只是最后一跳）。看到它就去看那几个节点在做什么，而不是去看父节点为什么"派生失败"——父节点没有失败，它在等。
- `title=spawn_round_fault_hold_still_waiting`：上一条的滞留仍在持续，按固定节拍复报同样的三个计数，外加 `held_seconds` / `stuck_seconds` 与 `unknown_nodes=` 前列节点 id。读法：`driven>0` 就是还有进度，等下去有依据；`driven=0` 且 `unknown>0` 是"没有任何可观测的推进"，`stuck_seconds` 满一个复报周期后本道会转成下面那条暂停。
- `title=spawn_round_parked_for_operator`：滞留在"没有任何可观测推进"上停满一个复报周期后，父节点被**可见地**停下——错误行进 `task_error_logs`、节点落 `pause_reason='error'` 并带 remark（正文自限到 900 字，头尾就是结论与观测值，不经外置），然后走节点暂停的既有心跳车道请会话 agent／操作者核对。它不改任何节点状态，也不代签判决。`detail` 的 `persisted=no` 表示暂停没能落库（正是同一次存储故障），此时本道继续等、下一拍重投，而不是把没登记成功的事说成已停。恢复入口：确认这批节点归属后 `resume_node` 本父节点（按同一 call id 重入），或用 `manage_task_nodes` 把这一批落 `canceled`。
- `title=spawn_round_fault_hold_converged` / `spawn_round_hold_released_by_stop_reason`：滞留的另两类出口——绑定节点全部落终态后交付真实结论，或任务/父节点显式终态后按该理由收尾。后者才说明这一轮的子节点结果不会被采纳。
- `title=spawn_round_converged_results_unreadable`：这一轮**已经收敛、成果都在库里**，只是把结论交给模型这一跳读不出来（entry 表、节点行、交接件、错误行任一处）。运行时不交付一句 `Error executing spawn_child_nodes`，而是照旧把父节点可见地停下：`detail` 里 `entry_count=unreadable` 表示连 entry 表都没读到（不是"零条"），`persisted=no` 表示暂停没落库、一手故障原样上抛。看到它去修存储/读口，然后 `resume_node` 同一个 call id 重放就取回真实结论——**不要**重发同一批，重发开局会把这一轮的 entry 当旧轮清扫。
- `title=spawn_round_result_reread_failed`：批后重读轮次记账失败，就地回退到本进程已经持有的那一份同源 payload，交付照常。它是降级痕迹，不是缺陷本身。
- `title=spawn_round_hold_probe_failed` / `spawn_round_park_write_failed`：滞留道与交付这一跳自己的读、或停车落库被就地降级。前者按 `stage=` 分（`entries` / `stop_reason` / `cancel_check_task` / `cancel_check_parent`），后者按 `stage=` 分两笔写（`error_log` / `node_pause`），紧随其后的 `spawn_round_parked_for_operator` 或 `spawn_round_converged_results_unreadable` 用 `persisted=no` 说明现场没登记成功——滞停那条继续等、下一拍重投，交付那条把一手故障原样上抛而不是谎报一次已落库的暂停。这两条都不是缺陷本身，是"仪表在坏但没有把工具打死"的痕迹。判据在 `main-task-runtime.md`「Node-Level Pause and Recovery」。

- `title=spawn_settlement_refused_live_node`：某条子管线的执行被异常或取消打断，但它绑定的子/验收节点仍非终态，于是运行时**拒绝**把这条 entry 结清成失败并整轮转交滞留道。这是期望行为：结清即交付，父节点会据此认为整批已定、批后帧写随即清掉 `pending_tool_calls`、同 id 重放通道关闭，而孩子其实没被取消——下一次取任务时它们会被收尸器照这句假话判成"无重放路径"杀掉。`detail` 带是哪一侧非终态与异常正文。
- `title=tool_watchdog_poll_read_failed`（`carrier=task` / `carrier=node_flag`）：看门狗每次轮询的暂停检查读不到库，这一拍没有信号、工具继续执行。它不是故障本身，是故障被挡在取消链之外的痕迹；与同时刻的 `disk I/O error` 成对出现即说明存储抖动正被按观测旁路降级处理，而不是被误读成"该取消这个工具"。
- `title=tool_watchdog_observation_failed`（`carrier=snapshot_supplier` / `carrier=on_poll`）：观测通道里抛出了**不属于**声明过的控制信号的异常，被 `_safe_observe` 就地降级（快照回退上一份、轮询跳过这一拍），工具照常执行。计数口径与上一条相同：按 `title=` 数，别按正文措辞正则。它持续出现说明仪表读在坏而工具仍然健康。
- `title=tool_watchdog_cancel_requested`：看门狗真的中止了一个仍在执行的工具，带 `tool_name=` 与 `reason=`。`reason` 同时被挂在异常对象上，交付给模型的文本与节点错误行都能读出「谁因为什么中止了它」。出现这条就要对照同任务同时刻的 spawn entry 状态：一次中止会让整批在飞子节点失去父侧等待者。

修复语义详见 `main-task-runtime.md`「Node-Level Pause and Recovery」。这些 warning 只作诊断，不会自行终结节点；真正的修复在恢复逻辑——等待现有绑定节点到终态，而不是重新评审或重放合成结果。

### 残留节点自愈

任务终态仅由根节点 + 最终验收推导，终态流转本身不强制收尾残留节点（见 `main-task-runtime.md`「Node-Level Pause and Recovery」）。真正“不会再被驱动”的残留节点由 worker 启动自愈清理：启动引导对每个终态任务调用 `log_service.sweep_residual_nodes`，把仍 `in_progress` 的节点置为 `failed`，`failure_reason` 带 `task_terminal_cleanup` 前缀并附产物定位（`execution_trace_ref` / `result_payload_ref`），只改状态、不删转录/产物，并发布 node patch 事件。因此终端里“重启后残留节点自动落终态”是预期行为，不是数据丢失；进行中任务不参与清扫。

### 任务执行了全局进程清理 / Web 与 worker 同时退出

如果任务执行了按进程名或宽泛 PID 范围清理的命令（例如 `Get-Process python | Stop-Process`、`taskkill /IM python.exe`、`pkill`），Web 主进程和托管 worker 可能被一起终止；这种退出没有正常 shutdown 日志，任务通常停在 `waiting_tool_results` 或被标记为异常停止。先看任务 artifact 中最后一个 `exec` 调用的 `arguments_text`，再对照 `.g3ku/logs/console.log`、`.g3ku/main-runtime/manual-web-run.log` 与 `.g3ku/main-runtime/managed-worker.log` 的最后时间戳；若三者在同一时刻截断且没有 graceful-exit 记录，优先判定为外部/任务侧强杀，不要先归因于模型或数据库。

当前 `exec` 在执行模式、白名单与审批判定之前增加宿主进程保护：常见 `Stop-Process` / `Spps` / `taskkill` / `pkill` / `killall` / `kill` / WMI-CIM terminate-delete / Python `os.kill` 等命令直接返回 `host-process termination` 错误；`full_access` 也不能绕过，白名单和操作者审批也不能放行。只读进程检查仍允许。保护是命令形态拦截，不是完整 OS 隔离；如果必须运行不可信的任意 native code 或外部二进制，仍应把任务放入独立 worker/container/低权限账户，并避免按进程名清理。

这条边界拦的是**命令形态**，所以它同时会误伤"模型停掉自己起的测试服务"——实盘一条会话里模型起了三个 Node 测试实例，自己停不掉，只能让用户手动结束。回收权因此住在**归属车道**上，这条边界本身不放松：

- `exec background=true`：运行时自己 spawn（POSIX `start_new_session`，Windows `CREATE_NEW_PROCESS_GROUP`），stdout/stderr 直接落 `owned-processes/<scope>/<…>.log`，立即返回 `pid` + `log_path`，并把 `{pid, create_time, cmd, log_path, owner_pid}` 登记进 `g3ku/runtime/owned_processes.py`。进程不继承 exec 的管道，也不受 exec 超时/取消管辖。
- `process_kill(pid)`（候选工具，水合后可用，`allowed_roles` = `ceo` + `execution`）：只认本 scope 的登记条目，校验 `create_time` 后整树终止，回日志尾部；未登记的 pid 一律拒绝并给出本 scope 台账。
- 生命周期三处回收：会话清空（`clear_web_ceo_session_artifacts`）、任务终态（`MainRuntimeService._reap_task_owned_processes`）、运行时启动（`launcher._reap_owned_background_processes`，只收 `owner_pid` 已死的孤儿）。

裸 shell 的 `kill` / `Stop-Process` 仍然拒绝：它是防手滑的护栏，不是沙箱（`powershell -File kill.ps1`、`node -e "process.kill(...)"` 本来就不命中正则）。正确做法是给"该杀的那些"一条正规出口，而不是拆掉护栏——拆掉之后 `exec` 就能杀掉正等着它返回的宿主，任务会停在 `waiting_tool_results`，只剩一条"异常停止"的恢复提示。

恢复后重点确认：托管 worker 看门狗是否重新拉起 worker、`worker_leases` 是否清掉陈旧租约、任务是否出现 `metadata.recovery_notice`。如果是误杀宿主后的遗留任务，不要用全局 `python` 清理；只使用任务级 pause/cancel，或由 `exec` 超时/取消路径清理该次调用自己启动的子进程树。

### 重启后任务未自动恢复 / 出现“异常停止”toast

先分清这次退出是优雅暂停还是异常中断：

- 优雅路径（重启脚本先调 `/api/bootstrap/exit`、或 Ctrl+C 让信号处理器收尾）：所有运行中的任务与会话被暂停并写 `shutdown_pause_registry` 台账，启动时自动恢复、不出现“异常停止”提示。退出前还有一次 ≤10 秒的排水等待（轮询 `pause_task` 命令直到 worker 真正停完 actor），停完才关闭托管 worker。若此时任务仍停在 paused：先分清它是不是「自动恢复跑过、又被上一个进程遗留的暂停命令打回」——这种任务台账已经为空，形态上和用户暂停完全一样，取证口径见 `main-task-runtime.md`「Graceful Shutdown Pause and Startup Auto-Resume」；worker 是否拿到 lease 完成 startup 也要查。直接 `python -m g3ku web` 的启动器在回收旧实例前先请求 `/api/bootstrap/exit` 并等待其暂停/退出，再强杀残留（`g3ku/web/launcher.py`）；`negi web` / `start-negi.*` 仍走 `negi_bootstrap.py` 的同语义回收。
- 异常路径（进程被强杀、worker 单进程被单独杀死）：任务恢复清洗照常执行，`metadata.recovery_notice` 写「本任务遇到异常停止…」，UI 以可关闭 toast 呈现（`web-and-admin.md`「Task Recovery Notice UI Contract」）。这是预期行为，点击关闭即可。托管 worker 被单杀后 Web 会由看门狗自动重启、无需人工拉起（见本节「托管 worker 看门狗」），但该 worker 当时正在跑的任务仍按异常中断走恢复清洗。
- 会话侧的自动恢复走 heartbeat `shutdown_resume` 内部轮（`heartbeat-system.md`「Shutdown Resume Wake」）：会话尾气泡会再现一条由系统恢复产生的回复；若没有出现，查启动日志里 `resume_shutdown_paused_sessions` / `auto-resumed` 与 heartbeat 事件投递日志。

### 界面停在初始化向导（看着像新项目）但 `memory/` 与 `sessions/` 都有东西

先分清"没数据"与"打不开数据"：`GET /api/bootstrap/status` 的 `mode` 是 `setup` 而 `key_store.state` 是 `lost` 时，丢的是凭据而不是项目——没有主密钥就没人能解锁记录，运行时不启动，会话列表因此空着，形态上和从没配过一模一样。

- 时间点与缺失清单：`.g3ku/security-events.jsonl` 里 `key_store_missing_material` 那行带 `missing` 与当时的备份代数；它上一行的 `key_store_backed_up` 说明最后一次完好是什么时候。
- 回滚：`negi security status` 看代际，`negi security restore --from <代际>` 先看 dry run 的文件表，确认后加 `--apply`。现场仍有 `master.key` 时 restore 直接拒绝，要人工把现件改名再执行。
- 没有可用代际（本次故障发生在留证上线之前）时走配置包导入：`negi security` 不补密钥，只有 `.g3ku/config-bundles/*.g3kucb` 能带回信封与 records，且需要该包的口令；导入后 worker 需重启才换链。
- 别把这条读成"数据被删了"：`sessions/`、`memory/MEMORY.md`、`main-runtime/` 都在，恢复凭据即可见。

### 已解锁但 `runtime.ready` 一直 false：记忆、任务、会话三屏同时空

`mode=unlocked`、`key_store.state=ok`，而 `runtime.agent_ready` / `main_runtime_ready` / `ready` 全 false 时，读的是**运行时没构造起来**，不是数据没了——三个列表都从 runtime 取值，所以缺一个就整屏空。先看 `.g3ku/logs/console.log` 里 `get_agent()` 的抛点，最常见的一条是 `ConfigNotFoundError: Config not found: <id>`：链上某个模型指向的凭据记录已经不在，而 `models.catalog[]` 那行还留着引用。

- 判据是「哪一位坏了」而不是「有没有模型」：`/api/models` 与 `/api/llm/bindings` 的每个 item 带 `record_missing`，管理面对应卡片显示「凭据记录缺失」。运行时按 `config-and-models.md`「绑定记录丢失时的降级」跳过坏成员继续用链上剩余成员，所以链首坏了不必修也能起来；全链都坏才报错，报错文本点名每个 key。
- 修复只有两条路：把记录本身找回来（配置包导入 / `negi security restore` 代际回滚），或删掉坏行再重新添加配置。管理面的详情保存第一步是按旧 id 更新记录，那次更新要求记录存在，所以就地改字段救不了这种行。删行会连带摘掉链位、绑定名与会话固定，先试找回记录。
- 凭据记录整本丢失（`llm-config/index.json` 是空 `configs`）时先按上一条「界面停在初始化向导」恢复 records，再回到这里看 `record_missing` 是否清零。

### 同步新配置失败 secret_overlay_decrypt_failed，apikey 全是空

钥匙与覆盖层是成对换的，而 `auto-unlock.key` 按设计不在包里。启动顺序 内存已解锁 → `G3KU_BOOTSTRAP_MASTER_KEY` → **auto-unlock 文件** → 口令，所以一份残留的旧 bearer 件会先赢：进程以"已解锁"的身份带着上一任主密钥起来，`overlay_readable=false`，所有密钥读成空、所有写入被守卫拒绝（`g3ku/security/bootstrap.py` 的 `undecryptable … refusing to overwrite`）。导入这一步会在装好新钥匙时把这份文件一起换掉（`install_master_key()`），本机没开免密就不新建；因此今天还能碰到的形态只剩**进程之间不同步**。

- 判据分两层，别混：`/api/bootstrap/status` 的 `overlay_readable` / `overlay_entries` 说的是**这个 web 进程**；worker 那一侧看 `task_commands`——`command_type=refresh_runtime_config` 且 `status=failed`、`error_text=secret_overlay_decrypt_failed` 的行，配上 `worker_id`，就是某个还在用旧钥匙的 worker。web 已换对而 worker 仍旧是常见形态，因为 worker 的主密钥是 web 派生它时用 env 递过去的（`g3ku/web/worker_control.py`）。
- 处置：确认 web 持的是正确钥匙（`overlay_readable=true` 且 `overlay_entries>0`），再重启一次让 worker 由解锁后的 web 重新派生。项目口令在这条道上不变——导入二级确认要输入的本机口令就是导入后继续用的那把。
- 留证：`/api/bootstrap/status` 的 `key_store.backups` 与 `.g3ku/security-events.jsonl`。`negi security status` 只看文件在不在，看不到钥匙配不配——那一位只在进程内读得出。

### 托管 worker 看门狗 / 任务大厅持续显示「worker stale」

本地默认启动（非 `--no-worker` / 容器）下，Web 解锁后托管一个 task worker 子进程（`python -m g3ku worker`）：worker 每 1–2s 写 `worker_status` 心跳并续 `task_worker` 租约（`worker_leases`，TTL 20s）。`/api/tasks/worker-status` 在心跳 `updated_at` 距今超过 15s（有活动任务 60s）时报告 `stale`，前端据此冻结创建/恢复控件并显示条幅。

`g3ku/web/worker_control.py::run_managed_task_worker_watchdog`（周期 5s）在托管进程确实退出后自动重启它，因此单点崩溃不会导致任务大厅永久 stale。三条安全闸门：

- 只在 `managed_worker_pid()` 为空（托管进程已退出）时触发，不看心跳——「卡住但还活着」的 worker 不误判为死亡；
- 重启前探测租约 `holder_pid`：存活则跳过（避免与外部单独启动的 worker 双跑），确认已死才清理陈旧租约以跳过 TTL 等待，未知则不动租约、让新 worker 自行按租约接管；
- 持续失败做指数退避（5s→…→60s 封顶），避免崩溃循环。

排查「一直 stale」按序：`.g3ku/main-runtime/managed-worker.log` 末尾 `worker_lease_unavailable:<holder>:<expires_at>` 是旧租约未到期就被拉起（非根因，等 TTL 即可）；`managed task worker watchdog:` 打头的是看门狗决策日志；仍需确认没有外部 `negi worker` 残留占着租约（查 `worker_leases` 的 `holder_pid` 是否还活着）；worker 反复崩溃时继续按「任务没创建或没推进」查崩溃根因，而非只看门狗兜底。

worker 静默不等于 worker 死亡：空闲 worker 除心跳线程每 1–2s 写库续租外，其余职责全部静默，唯一日志节律是每 10 分钟一行的 `worker heartbeat alive: worker_id=… pid=… active_tasks=… beats=… sqlite_write_failures=…` 存活行。判读：日志超过约 15–20 分钟不滚动而 `worker_leases.heartbeat_at` 仍新鲜（或 `holder_pid` 在 tasklist 中存活）→ 日志输出层异常，继续按本节排查；日志与心跳同时停 → 进程已死，看门狗自动兜底重启，崩溃根因按「任务没创建或没推进」查。

### 缓存命中下降或上下文疑似丢失

先看：

- `docs/architecture/context-and-cache-troubleshooting.md`
- `.g3ku/web-ceo-requests/`
- `.g3ku/web-ceo-continuity/`
- `.g3ku/web-ceo-turn-boundaries/`（编辑重发/Fork 截断相关）
- `sessions/`
- 相关的 paused / inflight snapshot

### 工具调用异常

先看：

- `g3ku/agent/tools/registry.py`
- `g3ku/runtime/context/`
- `main/service/runtime_service.py`
- 如果症状是执行节点/验收节点说“当前没有 candidate skills”或把本应当作 skill 的东西误判成“缺失的 callable 联网工具”，优先同时检查节点 runtime frame 与 `runtime-frame-messages:{node_id}` artifact 里的两组字段：
  - `contract_visible_skill_ids`：回答 `runtime_service._node_context_selection_inputs()` 当轮实际看到了哪些 contract-visible skills
  - `candidate_skill_ids`：回答 selector 最终留下了哪些 canonical skill candidates
- 如果还需要继续往下拆，再看 `skill_visibility_diagnostics`：
  - `registry_skill_ids`：回答 live `resource_registry` 当轮到底列出了哪些 skill
  - `entries[*].allowed_for_actor_role`：回答是不是在 `allowed_roles` 这一层就被挡住了
  - `entries[*].policy_effect`：回答 role policy 当轮给到的效果是不是 `allow`
  - `entries[*].included_in_contract_visible`：回答该 skill 最终有没有进入 `contract_visible_skill_ids`
- 排障顺序应先分层：
  - `contract_visible_skill_ids=[]`：优先怀疑 RBAC / governance / resource visibility 输入层
  - `contract_visible_skill_ids` 非空但 `candidate_skill_ids=[]`：优先怀疑 selector 或 contract/frame 重建链路
  - 两者都非空但请求体里读不到任何技能名单（头部 `## Runtime Contract (pinned)` 没有名单行，历史里也没有 `[RUNTIME_NOTICE]` 那条一次性提示）：优先怀疑钉住路没落上或动态 contract 重建/stale frame 恢复问题，按 `context-and-cache-troubleshooting.md`「静态声明钉在请求体头部」的症状条目查

### 模型配置不生效

先看：

- `g3ku/config/loader.py`
- `g3ku/llm_config/facade.py`

如果问题表现为“前端模型配置页已经显示了新的 API key / 新的 key 数量，但 CEO 或 worker 还像在用旧 key”：

- 先确认对应 runtime refresh 是否真的执行到了目标进程
- Web 托管 worker 路径下，优先看 `task_commands` 里的 `refresh_runtime_config` 是否完成，以及 `.g3ku/main-runtime/managed-worker.log`
- 当前系统约定是：显式 runtime refresh 会同时重载已解锁进程里的 bootstrap security overlay 缓存；如果 refresh 没跑到，老进程可能继续拿旧 secret 快照
- 如果 refresh 已完成但行为仍不对，再考虑 worker 进程是否需要重启，或是否存在多个旧 worker / 旧 Web 进程残留

### 节点模型负载不均 / 一直打同一个模型

`execution` / `inspection` 的车道行为契约见 `runtime-overview.md`「节点模型路由与准入绑定」，配置语义见 `config-and-models.md`「角色路由：有序 fallback 与负载均衡组」。运维判读路径：

- 先看这条链到底有没有组：`negi status` 打印的是 route/group 结构（`Execution Route: lb:g_shared(m_a|m_b)[rounds=1] → model:m_x`），没有「链首 = 执行模型」的读法。纯 direct 链仍按配置顺序 fallback，不存在均衡。
- 看实际分布：`GET /api/models/load-balance/status`（数据来自 worker 心跳，因此需要 worker 在线），按成员读 `running / waiting / reserved / rolling_rpm_60s / penalty_429 / score`。`quota_bucket_count` 小于成员数说明多条绑定共用一份配额，这是预期而不是 bug。
- 看单个节点为什么选了这个成员：`.g3ku/main-runtime/managed-worker.log` 的 `Model route selected` 行带决策时刻的负载读数与 `selection_reason`；换过成员则看 `Model node binding rebound` 的 `rebind_reason`（`filter_changed` / `capacity` / `plan_changed` / `fallback_after_failure`）。429 惩罚不是重绑原因，它只进打分决定新绑定选谁（契约见 `runtime-overview.md`「节点模型路由与准入绑定」）。`Model route lease released` 的 `outcome` 按这次授予的真实终态打标：`success`=换到了模型回应，`cancelled`=授予没换成回应（取消/preflight 失败/chat 抛错），另有 `build_failed` 与 `group_exhausted`。
- 发现某条链首成员被反复 429、而 `model_route_groups` 里它的 `rolling_rpm_60s` 偏低：先确认这些请求出自辅助车道（spawn 送审评审、异步任务重复预检），它们不过准入、不进均衡账。判据是 `Retryable model failure … round x/N` 的 N 落在成员目录 `retryCount` 而不是组的 `maxRetryRounds`（边界与维持现状的理由见 `runtime-overview.md`「节点模型路由与准入绑定」）。
- 同一类"读数偏低"还有第二堆，成因完全不同：**会话（CEO/frontdoor）的那一路结构性进不了这份读数**——它跑在 `web` 角色进程，那里没有 balancer 对象，而桶按进程各算各的。归属判据看日志出处（`ceo` 只出现在 `.g3ku/logs/console.log`，`managed-worker.log` 一条都没有），别把它当成"记账漏了"去补入口；契约见 `runtime-overview.md`「节点模型路由与准入绑定」的进程边界。
- 401、403、密钥被禁这类失败**不留任何运行态记忆**，状态接口里也查不到：它们当场由模型链 fallback 处理，要去 `.g3ku/logs/console.log` grep `MODEL CHAIN: FALLBACK` 或 worker 日志的 `Model load-balance member … exhausted`。只有上游限流（429）会跨请求留一份衰减惩罚。
- 「配额分布未知」看 `unresolved_bucket_count`：非 0 表示这个 worker 解析不到密钥材料（未解锁），此时桶合并与 RPM 归因都不可信，先解锁再判断。
- 要退回旧行为：`mainRuntime.modelRouteLoadBalanceEnabled = false` 把含组的链按配置顺序摊平成 direct 候选（有序链语义），不需要改模型绑定 key，也不影响 token 台账。

### 人设与初次引导

- 人设写入后前门没反应：先看 `memory/notes/note_persona.md` 是否存在，再区分"头部块还没到压缩边界"与"尾部通知没投递"。判读口径见 `context-and-cache-troubleshooting.md`「长期记忆的头部基座与增量」；人设正文不占记忆字数，`negi memory doctor` 的 `persona_note_present` 只是读数，不报 issues。
- 引导弹窗不出现：读 `/api/onboarding/status` 的 `should_run` / `completed`。老实例没有状态文件且已有 chat 绑定会被直接标记 completed；要在已有模型上重跑，用 `POST /api/onboarding/reset`（保留 `my_image/` 与人设 note）。
- 保存模型链后没有入队、也没有读图会话：先看 `.g3ku/onboarding.json` 的 `persona_request_id` / `photo_session_started_at` 是否已写，再查 `memory/queue.jsonl` 是否有一条 `trigger_source=persona_onboarding`，以及 `sessions/` 里是否有 `internal_persona_photo` 的转录。`photo_session_error` 非空表示读图会话失败，常见原因是 CEO 链里没有收图成员。
- 隐藏读图会话不出现在网页会话列表是预期：`internal:` 前缀不进 `web:` 目录、不当活动会话兜底，也不参与记忆复核。它出现在列表或复核批次里属于缺陷。
- 照片集为空但 `my_image/` 有文件：画廊只列 raster 图片（png/jpg/jpeg/webp/gif/bmp）；缩略图失败会回退原图 URL，不影响陈列。

### 外部渠道桥接异常

IM 渠道有两条：内置官方 QQ 适配器（`g3ku/qq_official/`，进程内 botpy 桥，见 `external-agent-api.md`）与第三方独立桥接进程（经 `/api/v1` External Agent API 接入）。早期的 china bridge 子系统已下线，配置里出现 `chinaBridge` 会被直接剥掉。先看：

- `docs/architecture/external-agent-api.md`「常见排障入口」
- 桥接应用自身的日志与配置（如 `bridges/qq-onebot/README.md`）

### 磁盘满（Errno 28 / SQLITE_FULL）

症状族：worker 日志或 `.g3ku/errors/` 出现 `OSError [Errno 28] No space left on device` / `OperationalError: database or disk is full`；`.g3ku/errors/` 里的错误日志是 0 字节空文件；多个节点连锁 error-pause；渠道告警发不出去。

**先分辨再进本节**：同一句 `OperationalError: disk I/O error` 有两个成因。结构化码是 `SQLITE_FULL` / `SQLITE_IOERR_FULL` 才走下面的排障；是 `SQLITE_IOERR_READ` 且剩余空间充足时**不是**满盘，而是承诺内存见底，判读见下一节「内存峰值 / 恢复风暴后 RSS 不回落」第 8 条——按本节从"定位空间大户"开始查会一路查空。

排障顺序：

1. 先看 worker 心跳 debug 块里的写失败计数（`worker_leases` 行 `payload_json.status_payload.debug` 下的 `sqlite_write_failures` / `event_write_failures`，`worker_status` 行 payload 与存活日志行同步携带）与 `managed-worker.log` 里的 SQLITE_FULL 行、限流告警 `task_events write failure (rate-limited): total=…`（300s 至多一条）——磁盘满期间错误日志本身可能写不出来，`.g3ku/errors/` 不是唯一证据源（计数契约见 `runtime-overview.md`「磁盘写保护与治理」）。
2. 定位空间大户：`.g3ku/main-runtime/artifacts/`（历史任务产物）、`runtime.sqlite3`、`memory/`、`temp/tasks/`、`.tmp/`。目录统计命令要给足超时——磁盘近满时全量遍历极慢，短超时得到的数字不完整。
3. 运行时自动行为无需干预：可降级写按应急预算自动跳过、error pause 记录失败不连锁、终态任务的中间产物自动清理；磁盘剩余跌破紧急线（max(300MB, 1%)）时运行中任务被自动暂停（新工具调用排队等待、不报错），任务大厅出现红色横幅与性能条「CPU/内存/磁盘」项的紧急着色（磁盘段显示 `0%(剩余10.1G) · 紧急`），空间恢复后紧急态自动解除、**被暂停的任务需手动 resume**。
4. 需要人工的只有两类：回收历史存量（无写入者的死库文件），以及调整 `main_runtime.disk_guard` 配置（字段契约见 `config-and-models.md`「main_runtime」）。磁盘治理没有任何自动任务删除：任务终态即清确定用不到的数据（中间产物 + event-history 单份快照）；任务本身只随用户页面删除或模型删除工具彻底清除（删除前报告类产出自动导出到 `.g3ku/main-runtime/deliverables/<task>/` 永久保留）。磁盘紧张时先在任务大厅用「按大小」排序（或模型工具 `task_stats` 的 `sort=size`）定位大任务：终态大任务可以先用卡片菜单的「清除临时文件」只回收 `temp/tasks/<id>`（任务记录与过程数据全保留，占用大小即时重算），确认不需要回顾时才整任务删除。契约见 `runtime-overview.md`「手动清除任务临时文件」。每小时维护循环按 `detail_retention_days`（默认 0=停用，配置 >0 恢复）裁剪终态任务的五张大行表、经删除台账 sweep 补偿中断的删除并清扫孤儿 event-history 目录（契约见 `runtime-overview.md`「磁盘写保护与治理」）。event-history 每任务只存一份 live.patch 最新快照（latest.json.gz），终态清理时删除。从带 zip 归档/逐事件归档历史的旧版本升级时，先跑一次性迁移 `scripts/migrate_slim_task_storage.py`（停机/排水后，默认 dry-run 报数，`--apply` 做三件事：event-history 收敛单份；存量 zip 先导出判据内产出，产出落盘成功（或判据内本无产出）才删 zip——一条都没导出成功就保留原 zip，`task-archives` 目录也随未处置的 zip 一起保留；清 live.patch DB 行与孤儿记账行）。
5. `runtime.sqlite3` 收缩用 `scripts/compact_task_database.py`（默认 dry-run 报数；`--apply` 裁剪终态任务早于 `--retention-days`（默认 14，0=跳过裁剪）的五张大行表、`--backup` 先镜像、`--vacuum-full` 对存量库做 VACUUM 迁移，脚本自带 1.2× 空间预检）。**必须在服务停机或排水后运行**（VACUUM 需独占连接）。运行时侧新库自动 `auto_vacuum=INCREMENTAL`；运行时行裁剪由对账 loop 每 23h 卡权执行，且仅当 `detail_retention_days>0` 时生效（紧急水位跳过）。两点会让收缩"看起来无效"：`nodes.payload_json` 不在裁剪清单里，而它持有节点正文的唯一一份 `input`（存储形状见 `runtime-overview.md`「投影表的列只承担主键与索引」与「节点当轮正文只有一个家」）；删行本身只把页还给 freelist，不 `--vacuum-full` / `incremental_vacuum` 就不会向操作系统归还空间，而这些页会被后续写入立刻复用。
6. 磁盘接近满时不要手工对大 sqlite 库执行 VACUUM——它需要约一倍库大小的临时空间，会立刻打穿剩余水位（脚本内置同款预检）。

### 内存峰值 / 恢复风暴后 RSS 不回落

症状族：暂停→重启→恢复后 worker 内存在分钟级涨到 GB 级，同期事件循环滞后放大（性能条 lag 秒级、`task_model_calls` 一段时间不落行）；风暴过后工作集回落，但承诺内存不还给系统。

判读顺序：

1. 三个量分开看，别混成"泄漏"：`WorkingSet`（OS 可随时换出，看着小）、`PrivateMemorySize`（进程攥着的承诺内存）、`PeakWorkingSet`（到过最高点的那一刻）。峰值只说明某一瞬同时驻留过多少，之后不回落是分配器保留；稳态下三个数都在小幅抖动而不同时上爬，就不是泄漏。取数：`Get-Process -Id <pid> | Select WorkingSet64,PrivateMemorySize64,PeakWorkingSet64`。
2. CPU 热点回答不了内存问题。`py-spy dump/record` 给的是时间去哪（热路径的 O(N²) 形状在那里找），要答"那一刻同时驻留的是谁"只能用进程内分配探针——离线把可疑函数单独量一遍（`tracemalloc` 逐次分配 + RSS 轨迹）同样能排除候选：一条只分配几十 MB 的道撑不起 GB 级峰。
3. 分配探针：在 `<数据根>/.g3ku/main-runtime/` 下建标记文件 `mem-probe.on`，重启 worker 即开始采样，每拍覆盖写一行 JSONL 到同目录 `mem-probe/mem-probe-<pid>-<启动时刻>.jsonl`。行内字段：`rss_mb` / `private_mb` / `traced_mb`（当前被跟踪的 Python 分配）/ `traced_peak_mb` / `growth_mb`（相对上一拍）/ `top`（按 `文件:行` 排名的驻留榜）/ `growth`（相对上一拍增量榜）。间隔与条数可用 `G3KU_MEM_PROBE_INTERVAL_SECONDS`（默认 10，<1 回落默认）与 `G3KU_MEM_PROBE_TOP_LIMIT`（默认 12）覆盖，**也可以直接写进标记正文**（`interval=`/`frames=`/`top=`/`dump_mb=`，逗号或换行分隔；环境变量优先）。只在 `execution_mode='worker'` 且标记存在时启动；`tracemalloc` 全程挂在分配路径上，采完删掉标记。启动时会往 `managed-worker.log` 记一行生效参数，别凭标记内容猜。
4. 探针自己要记账，别把它的开销算成被观测者的：实盘一次恢复批次开着 tracemalloc 峰值到 3038 MB，摘掉标记重启后同一批恢复峰值 2077 MB（≈1 GB 是 trace 记录本身），且那一轮事件循环滞后实测 337,144 ms。⇒ 归因时先扣这份开销，并且别在一次恢复风暴上开着超过两三拍。
5. 站点榜只回答"在哪申请"，不回答"谁在申请"。要调用链：`G3KU_MEM_PROBE_FRAMES`（默认 1）抬起 tracemalloc 栈深（>1 才有链），`G3KU_MEM_PROBE_DUMP_MB`>0 时被跟踪驻留**第一次**越过该阈值就落一份 `mem-probe-<pid>-<启动时刻>-dump.txt`（每进程一次，按完整链排名）。成本按活块数走：几百个对象的小堆上 `statistics('traceback')` 已 122ms，实盘 600 万块级别按秒计，所以只在需要那一次开。
6. 探针的边界：它统计走 Python 分配器的对象，C 侧缓冲（zlib 压缩、sqlite 读入的大 blob、socket 内核缓冲）不进 `top`，所以 `traced_mb` 明显低于 `rss_mb` 时先想这条口径差，别当成统计漏。实盘一次读数就是这形状：`rss` 3038 MB 而 `traced` 605 MB ⇒ 约八成峰值不在 Python 对象上，Python 侧的两个大头是 `json/decoder.py:raw_decode`（整任务 `payload_json` 反复解析后的驻留）与每帧的技能可见性诊断。站点行号指向申请处，不一定是持有者——找持有者用上一条的链。
7. 为什么是标记文件而不是环境变量：托管 worker 的环境来自 web 进程的 `os.environ.copy()`（`g3ku/web/worker_control.py`），要按环境变量给它开闸就得重启 web；标记文件只需重启 worker，与 `.g3ku/llm-config/auto-unlock.key` 同属"盘上一个文件决定一次行为"的运维开关。手动 `negi worker` 同样认这个标记。
8. **承诺内存见底会伪装成磁盘故障**：Windows 在提交额度耗尽时让 `ReadFile` 直接失败，SQLite 报 `OperationalError: disk I/O error`、结构化码 `SQLITE_IOERR_READ`(266)——盘没满、卷没坏、库没损，只是没有内存页可用了。分辨三读数：① `grep -c sqlite_errorname=SQLITE_IOERR_READ` 与 `...=SQLITE_FULL`（`sqlite_store` 的错误行自带这两个字段，写侧一个都没有＝不是满盘）；② `Get-WinEvent` 里 `Microsoft-Windows-Resource-Exhaustion-Detector` id=2004 按小时数，与 ① 的爆发小时对齐即成立；③ `Win32_PageFileUsage` 的 `PeakUsage` 是否等于 `AllocatedBaseSize`（顶满）与 `Win32_ComputerSystem.AutomaticManagedPagefile` / `Win32_PageFileSetting`（手动钉死的小页文件会把提交上限压到 物理内存+该值）。实盘一天三场（05:54 83 秒内 53 条、13:02 同秒 5 条、16:41 192 秒 8 条）全部由 ② 的同刻事件命名的大进程对上。运行时侧的处置是把这类抖动按设计停在"可见停车 + 同 call id 可重放"（判据见 `main-task-runtime.md` 派生车道），环境侧的一刀在页文件与内存配置，代码改的是后果不是频率。

### 任务大厅卡顿 / 冻结（浏览器端）

症状族：从任务详情返回大厅后滚动无响应或「一滑动就卡住」；重者整个 Edge 窗口挂「未响应」数十秒，甚至出现 WerFault 崩溃报告；卡顿有时在用户并未操作浏览器时自行发生。关键辨识：卡顿期间大厅的性能监控数字仍在正常刷新——页面主线程与绘制活着，阻塞在输入/合成层之下，或本质是滚动位置被反复清零。

已确认并修复的原因（契约详见 `web-and-admin.md`「Web Event Loop Contract」）：

1. worker 心跳时间戳进大厅网格渲染签名：每次心跳整网格重建，网格自身是滚动容器，`scrollTop` 归零——表现为「一滑动就卡死而数字照常刷新」。签名只保留影响像素的字段，同页重建还原滚动位置（测试 `tests/resources/org_graph_tasks.hall_scroll_survival.test.js`）。
2. 详情退出路径把状态捕获、大 DOM 拆除、大厅请求挤在同一点击帧：重步骤推迟一个宏任务执行（测试 `tests/resources/org_graph_app.task_detail_exit_defer.test.js`）。
3. 服务端事件循环阻塞族（SQLite 读串行、CEO 目录构建、netstat 子进程）均已卸载或缓存，见同一契约章节。

已排除的因素（含证据）：

- 服务端业务代码阻塞：超时时刻 py-spy 抓栈为事件循环空闲等待；探针监控未见新增服务端超时。
- 返回路径的请求差异：箭头直达与侧栏绕行发起的请求完全相同（`GET /api/tasks`、`/api/ws/tasks`、worker-status），控制台探针日志核对一致。
- 点击帧时序：两跳中转垫片（60ms 与 5s 停留）实测仍卡，垫片已移除。
- 陈旧缓存 JS：静态资源以 no-store 下发，刷新即最新。
- JS 长任务：卡顿窗口内 longtask ≤120ms、无帧缺口、无输入延迟——冻结发生在 JS 层之下（渲染器进程/操作系统）。
- 焦点抢占只解释「点击被吞」，不解释「未响应」：前台窗口监视记录过一次 visibility 闪烁 + 窗口失焦吞点击，与真挂起以此区分。

当前未解决的头号嫌疑：机器级内存超卖。7.7GB 内存空闲长期仅 800–1700MB，卡顿时刻伴随「内存风暴」——空闲内存数秒内跌数百 MB、硬缺页 10–40 万/秒、CPU 逼近满载，Edge 渲染器被裁剪挂起即「未响应」，极端时崩溃。风暴可在用户未操作浏览器时发生。已知大户与风暴嫌疑：抓取类任务周期性派生爬虫/CDP 子进程、重复启动的服务栈（两套 web+worker 并存）、Edge 自身 2–2.5GB/30+ 进程、远控推流、Defender 实时扫描。待验证项：后台驻留标签页（详情视图未关）持续接收 live 事件、DOM 与状态增长，疑似撑大标签页工作集。

排障顺序：

1. 拿到卡顿的本地墙钟时刻（精确到分钟）。
2. 对照四类监控（排查期部署在 `%TEMP%`，失效则按此清单重建）：`os_monitor.csv`（空闲内存/CPU/Edge 工作集/进程数/硬缺页，约 3s 采样）判当时有无风暴；`proc_top.csv`（每 5s 内存前八进程）定风暴起点谁在吃内存；`foreground_log.csv`（每 200ms 前台进程与窗口标题）——dwm 挂「未响应」幽灵标题=窗口真挂起、WerFault=崩溃、前台突变=焦点被抢；`stall_captures/`（200ms 探 worker-status，>0.5s 即 py-spy 抓 web 进程栈）排除服务端阻塞。web 进程自身在 `console.log` 里留一行 `slow web request: <METHOD> <path> -> <status> in <ms> ms`（`/api` 路径超过 1s 才写，契约见 `web-and-admin.md`「Web Event Loop Contract」），拿卡顿时刻的分钟去 grep，能直接判「浏览器在等服务端」还是「服务端根本没事」；WebSocket 车道不在这条覆盖里，live 流仍需浏览器侧探针。同一份日志里存储层的慢读分两档：`new reader lane` 那行是每个 (表, 调用方) 组合**进程内只报一次**的首见记录，时间戳不代表某一次卡顿，别拿它归因；要按时刻对上具体查询，看 `slow read: <section> took <ms> ms from=<调用方>`（≥800 ms 每次都报）。还有一条不用翻日志：`GET /api/tasks/worker-status` 直接回本进程的 `web_long_blocks` / `web_worst_long_blocks`（段名 + 自身毫秒 + `started_at`），用来分清「某一条查询慢」和「这个进程正忙着别的」——只有前者会留下 slow read 行，后者只在账本里显形。
3. 有风暴：按 `proc_top.csv` 的大户处置——暂停对应抓取任务、消除重复服务栈；长期方案是加内存。
4. 无风暴：在被卡标签页重装控制台探针再复现，探针测量项：关键函数耗时、LoAF 阻塞归因（JS/渲染/非 JS）、帧缺口、输入延迟、wheel 事件与 scrollTop 生效对照、JS 堆悬崖、窗口焦点与可见性变化。
5. 滚动归零类的验证口径：滚到中部停留 10 秒位置不被拽回，且 `S.taskHallStats.task_hall_full_render_count` 不增长。

## 6. 维护时的高风险修改类型

### 修改 session turn 逻辑

涉及文件：

- `g3ku/runtime/session_agent.py`

风险：

- 容易同时影响 user turn、heartbeat turn、cron turn、pause/resume。

### 修改 task runtime 总装配

涉及文件：

- `main/service/runtime_service.py`

风险：

- 工具集、治理、日志、worker、内容服务可能一起被带坏。

### 修改工具可见性与候选池

涉及文件：

- `g3ku/runtime/context/`
- `main/service/runtime_service.py`

风险：

- 不一定会报错，但会显著改变 agent 行为，是高隐蔽性回归。

### Provider Bundle Refresh

- provider-facing tool bundle 的刷新时机、排序与压缩边界约定详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」；bundle 变化会直接影响 prompt cache 前缀稳定性，改动后应验证缓存表现。

## 7. 维护建议

### 先判断问题属于哪条主线

不要一上来全文搜索。先判断它属于：

- 会话主线
- 任务主线
- heartbeat
- Web/API
- 配置/模型
- 外部桥接（/api/v1）

### 优先从集成点往下看

很多问题不在最底层，而是在集成处。例如：

- Web runtime 装配失败
- session key 路由错误
- candidate tool 没进 callable set

### 改提示词也要当成代码改动看待

如：

- `g3ku/runtime/prompts/heartbeat_rules.md`
- `g3ku/runtime/prompts/ceo_frontdoor.md`

这些文件会直接改变 agent 行为，回归风险不低。

### 对 `main/service/runtime_service.py` 保持敬畏

它过于 central。每次改动后，最好至少验证：

- 任务创建
- 节点执行
- task detail API
- 工具可见性

## 8. 新功能接入时的建议切入点

### 加新 tool

先看：

- `g3ku/agent/tools/`
- `tools/<tool_name>/`
- `main/service/runtime_service.py`

### 加新 skill

先看：

- `skills/`
- `g3ku/agent/skills.py`
- `ResourceManager` 相关逻辑

### 接入新的外部渠道

先看：

- `docs/architecture/external-agent-api.md`（API 契约）
- `bridges/qq-onebot/`（参考桥实现）
- 新桥接作为独立进程/仓库开发，Negi 本体零改动

## 9. 最小验证清单

做完较大改动后，先做自动化前置检查：

- `scripts/lint.sh` / `scripts/lint.ps1`（等价于 `python -m ruff check g3ku main tools scripts bridges tests negi_bootstrap.py`）
- `python -m pytest --collect-only -q`
- `python -m pytest tests/resources/test_resource_runtime_smoke.py -q`（冒烟子集）

自动化通过后，再至少人工验证下面的清单：

1. CLI 同步会话可用
2. Web 页面可打开
3. Web 会话可发送并返回
4. 至少一个异步任务可创建并完成
5. task detail / node detail API 正常
6. 若改动涉及工具系统，验证候选工具与 callable 工具行为

### Lint 闸门的覆盖面

闸门只负责自有代码：`scripts/lint.sh` / `scripts/lint.ps1` 跑的是
`ruff check g3ku main tools scripts bridges tests negi_bootstrap.py` 这一份目录清单。清单是硬编码的，
新增顶层目录要同时改进脚本，否则新代码不在任何人视野里。

`pyproject.toml` 的 `[tool.ruff]` 另有一层 `extend-exclude`：`work/`、`skills/`、仓库根的 `runtime/`
任务数据、`pytest-of-Administrator/`、`repair_tmp/`、`.g3ku/`。排除项按目录名匹配任意深度，所以根级
`runtime` 写成 `runtime/*`，否则 `g3ku/runtime`、`main/runtime` 两个包也一起被扫不到。仓库根的未入库
散件不在闸门清单里；要看全仓口径就显式跑 `ruff check .`，它会连这些散件一起报。

`skills/**` 是随包分发的脚本载荷，改它属于内容决定而不是格式决定，因此不在闸门内；
需要检查入库那几个 `.py` 时显式传路径（显式路径不受排除项影响）。

命名与导入位置类抑制的落点：`tests/**` 与 `scripts/*.py` 的 E402、Win32/ctypes 面的
N801/N802 走 `per-file-ignores`，控制流异常名（`*Requested` / `*Closed` / `*Interrupted`）
与被延迟导入绑定的类名一律用行级 `# noqa`，因为整片 ignore 会吞掉同一文件里后续的真违规。

代码形状由评审负责，不由工具强制：`line-length = 100` 与 `ignore = ["E501"]` 并存，就是允许长行的
既定约定，而 `ruff format` 会把所有超列的行拆开——自有代码 827 个文件里只有 174 个已符合它的形状，
全量重排约 +6.5 万 / −4.1 万行。把 format 纳入闸门等于替全仓立格式制度，属于单独一次决定，不进日常
闸门。手工跑 format 时若 ruff 0.17.1 报内部错误（退出码 101）而不是列出文件，原因是带 UTF-8 BOM 的
`.py` 让它的差异渲染 panic；BOM 在 `.py` 里只是编码元数据，去掉不改变 Python 的解码结果。

## Memory Queue Workflow

For the queued Markdown memory runtime, the first operator checks should be:

1. Inspect `memory/memory_state.sqlite3` for the authoritative memory rows, `refresh_count`, `passed_count`, `is_compressed`, and `from_user` state.
2. Inspect `memory/MEMORY.md` for the prompt projection currently injected into CEO/frontdoor. It is regenerated from sqlite and never feeds back into it, so a difference between the two is a damaged projection, not lost memory. Any content the store does not produce is archived first as `memory/MEMORY.md.bak.<YYYYMMDD-HHMMSS>` before the file is repainted — read those to recover a hand edit or a torn write, and treat a growing count of them as evidence that something outside the runtime is writing into the memory directory.
3. Inspect `memory/queue.jsonl` for pending `write` / `delete` requests.
4. Inspect `memory/failed.jsonl` for parked failed batches: each row carries `category` (`provider_error` / `window_refusal` / `protocol` / `agent_fault` / `capacity` / `capacity_noop`), `status` (`parked` / `requeued`), retry counters and a full `error_history`. A non-empty file means some turns of memory are waiting for a success signal or an operator decision. `capacity` is the ladder's last rung: that batch never fit, its payload is held verbatim, and the queue kept flowing. `capacity_noop` is a consolidation that ran clean and declared these rows have no category - discarding it is what re-arms consolidation for that same candidate set, so leaving it parked is a deliberate "stop asking", not a forgotten failure. `agent_fault` is a deterministic write fault (the batch could not be turned into a document at all): retrying it asks the model again rather than replaying the same answer, and it never rejoins the queue on its own; read `last_error_text` for the reason.
5. Inspect `memory/ops.jsonl` for the latest terminal batch history (`applied`, `precheck_failed`, `operator_discarded`). Note files removed by the orphan sweep appear as `note_reaped` change entries carrying the reason the store stopped owning them (`memory_deleted` / `ref_dropped` / `unreferenced`) — that is the only evidence trail for a note that is not on disk. A user saying "it did not remember" is diagnosed here, not in the worker log: every request that entered the queue ends as exactly one terminal row, so a missing row means the request never reached the queue.
6. If a processed row exposes `request_artifact_paths`, inspect the referenced files under `.g3ku/memory-requests/` before blaming prompt assembly or the provider adapter.
7. Use `negi memory current`, `negi memory queue`, and `negi memory flush` when you need a quick operator view without manually opening files.
8. If the queue head is stuck in `processing`, inspect `.g3ku/config.json -> models.roles.memory` before debugging the frontend.

Operator workflow for parked failed batches:

1. The web `记忆管理` page shows the `失败记忆` panel only while parked records exist (left column, under the pending queue). Cards are red, carry the failure category and a retry icon; clicking a card opens the detail drawer with the full error history.
2. `provider_error` records rejoin the queue automatically: every successfully applied batch requeues the oldest parked provider-error record at the queue tail. During a provider outage nothing retries; recovery is paced by real traffic. `queue.auto_requeue_on_success=false` disables the signal entirely. No other category rejoins on its own - `window_refusal`, `protocol` and `agent_fault` wait for an operator.
3. `window_refusal` records (the batch was refused by the memory lane's own pre-send capacity check, before any provider call) never auto-requeue: they wait for an operator. Retrying one is not a repeat of the same failure any more — the requeued payload goes through the same capacity splitter as fresh enqueues, so an oversized window comes back as several in-capacity queue rows. A refusal that survives a retry therefore means one atomic stage alone exceeds the whole window, and the payload has to shrink on the producer side.
4. `protocol` records (the memory agent answered but never produced a valid `memory_apply_batch` result) never auto-requeue; they wait for an operator.
5. Manual retry and discard are ordinary operator actions on this surface (no environment flag): retry requeues the record under its original `request_id`s; discard writes an `operator_discarded` terminal row into `ops.jsonl`, removes the parked record and any stale queue rows, and makes those `request_id`s dedupe-processed. Both actions append to `memory/admin_audit.jsonl`. Only the legacy queue-head retry contract still requires `G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS`.
6. The UI buttons stay usable for every operator; the protective layers are the in-app second-confirmation dialogs and the audit trail. Inspecting parked records needs no flag either.

Operator debugging order for a stuck queue head:

1. Check whether `models.roles.memory` is empty or points to an invalid model chain.
2. Check the queue-head `last_error_text`, `last_error_at`, `retry_after`, and `processing_started_at`.
3. Check `memory/failed.jsonl` and the worker logs for provider/tool-call failures or semantic validation failures — processing failures park instead of blocking, so an empty queue with a non-empty failed file is the normal signature of a provider outage, not a lost queue.
4. When many parked rows repeat the same `memory send exceeds model window` text, that is the memory lane's local capacity precheck, not an outage. Compare the two numbers inside the message: an `估算` far below `窗口` means the refusal came from the check's own capacity rule. A genuinely oversized batch should already have been split at enqueue time (the review window packs into pieces of half the smallest declared chain window), so a refusal means one atomic stage alone exceeds the whole window, and the payload has to shrink on the producer side. Retrying is still worth trying first: the requeued payload is re-split, so only a refusal that survives a retry points at an unshrinkable stage. Category and capacity contracts: `runtime-overview.md`「队列状态机与失败停车语义」.
5. Only after the runtime side is healthy should you debug the web `记忆管理` page.

Operator debugging order for duplicated successful memory writes:

1. Compare `memory/ops.jsonl` rows by `request_id`, not only by `batch_id`.
2. If the same `request_id` appears in two successful processed rows, treat that as duplicate consumption of one queue request rather than proof that the frontdoor called `memory_update` twice.
3. Inspect live `python -m g3ku web` / `python -m g3ku worker` processes and verify only one runtime instance should be actively consuming the memory queue.
4. If multiple runtimes were active, treat duplicate rows as historical worker-contention evidence first, then inspect whether the current build already has the memory-worker lease and processed-request dedupe protections.

### Memory Maintenance CLI

The memory CLI keeps only the queued Markdown runtime operator surface:

- `negi memory current`
- `negi memory queue`
- `negi memory flush`
- `negi memory uncompress <memory-id>` (`--all` warms every downgraded row)
- `negi memory doctor`
- `negi memory reconcile-notes`
- `negi memory reformat`
- `negi memory import-legacy <path>`
- `negi memory cleanup-legacy`

The active memory operator surface is `negi memory` with `current` / `queue` / `flush` / `uncompress` / `doctor` / `reconcile-notes` / `reformat` / `import-legacy` / `cleanup-legacy`; there is no reset subcommand, and legacy-only commands (runtime stats/trace/explain, decay, pending-fact review) are not part of the operator contract.

`negi memory uncompress` is the only way to undo a capacity downgrade: the runtime never re-promotes a row on its own, because automatic warming would cancel the decision the ladder just made and oscillate against the trigger line. Warming puts characters **back** into the resident block, so it goes through the same hard-cap validation as any other write and rolls the row back whole when the document would not fit - the report carries `resident_chars_after` next to the trigger line and the cap, which is how much headroom the action consumed.

The operator-oriented maintenance commands beyond `current`, `queue`, and `flush` are:

- `negi memory doctor`
  Read-only health check for the queued Markdown memory layout.
- `negi memory reconcile-notes`
  Explicit note-ref reconciliation for `MEMORY.md` and `memory/notes/`.
- `negi memory reformat`
  The one pass that moves an existing store onto the current on-disk shape. It flattens any memory row whose stored text spans several lines (a multi-line row has no one-line rendering, so the block would silently drop it), then repaints `MEMORY.md` from SQLite. Stored ids keep the length they were created with - the block prints none, so lengthening them moves nothing a reader sees. The report carries `flattened_rows`, `chars_before` / `chars_after` and `resident_rows`, which is how an operator confirms the pass actually did something.
- `negi memory import-legacy <path>`
  Minimal one-shot importer for legacy memory exports.

Use them with these boundaries in mind:

- `doctor` is inspection-only in the write sense: it opens the SQLite store on a read-only connection, which is what makes its ownership axes true, because note ownership (`note_ref`) and retirement (`consolidated_into`) are columns and nothing else - a manager that skipped the store would call every owned note an orphan and every retired class intact. It still creates nothing and changes nothing: no `MEMORY.md` repaint, no note creation or deletion, no queue mutation, no bootstrapping of missing paths, and a workspace whose store does not exist yet is reported from the files alone. Its two note axes do not mix: missing/orphan reads the ordinary column against `memory/notes/`, while a category name is judged only by `broken_category_links` because its file lives under `memory/index/` - running the category column through the note check reports every class as a missing note. The deletion guard on the commit path is deliberately wider than that report (any name a column still mentions survives, including a legacy name shared with a category note), so a note `doctor` lists as orphan is not necessarily one the runtime would sweep. It should be the first stop when an operator suspects notebook corruption or a blocked queue head. It checks the managed Markdown block format (measured on the resident entries, with the permanent consolidation index excluded), note-ref consistency, orphan notes under `memory/notes/`, retired memories whose category note under `memory/index/` does not exist (`broken_category_links`), malformed `queue.jsonl` rows (line-level diagnostics), stuck `processing` heads, and parked failed batches (`failed_parked` check with `failed_parked_count`; any parked record reports issues_found and lists the first five `failed_id(category)`); malformed queue rows surface as explicit queue-parse issues with a non-zero exit instead of a bare JSON decode crash.
- `reconcile-notes` is the explicit repair path for note/file consistency. It works the ordinary axis only: it may create placeholder note files for refs owned by `note_ref` and deletes orphan note files under `memory/notes/` only when the operator passes the explicit delete flag. A retired row whose category note under `memory/index/` is gone is reported as `broken_category_links` and never gets a placeholder — a placeholder category note would put a resident index line describing nothing.
- `import-legacy` is dry-run by default: it parses the legacy payload and prints a summary without creating `memory/`, `MEMORY.md`, `queue.jsonl`, `ops.jsonl`, or note files. Writing requires `--apply`, and the target notebook should already be empty — do not use it as a merge tool for a live non-empty queue.
- `cleanup-legacy` is dry-run by default and lists removable legacy artifacts (`HISTORY.md`, structured projections, sync journals, pending/audit files, `context_store/`). `--apply` refuses to delete data-bearing legacy artifacts while `MEMORY.md` is still empty: import or review old data first, then delete leftovers once the new notebook already contains the migrated memory.

Recommended operator order:

1. Run `negi memory doctor` first.
2. When the store is readable but its document does not parse into the current one-line shape, run `negi memory reformat` once and read `flattened_rows` / `chars_after` back.
3. If the only issues are missing note files or orphan notes, use `negi memory reconcile-notes`.
4. If the notebook is empty and you are doing a controlled migration, run `negi memory import-legacy <path>` once without `--apply`, inspect the summary, then rerun with `--apply`.
5. After migration is complete and `MEMORY.md` is already authoritative, run `negi memory cleanup-legacy` once in dry-run mode, review the paths, then rerun with `--apply` to remove leftovers.

Queue-head recovery caveats that matter during operations:

- A `processing` head that survives restart is expected durable state. Do not assume it means a live worker is still attached.
- If `retry_after` is still in the future, the restarted worker should leave that head untouched and keep later items blocked. Once `retry_after` has passed, the same head becomes eligible for retry. Blocking heads come from the configuration paths (`memory` role not configured, runtime config unreadable) or from builds that predate failure parking; processing failures in the current runtime park into `memory/failed.jsonl` and leave the queue flowing.
- `processing_started_at` is the first-claim timestamp for that head batch. It should remain stable across retries, so a new `last_error_at` with an old `processing_started_at` is normal. Parked records keep the original claim timestamp inside their stored `items`.

## Docker / Compose Startup

Negi has two supported operator startup modes:

- direct local startup through `start-negi.cmd` / `start-negi.ps1` / `start-negi.sh`
- container startup through `compose.yaml`

For the container path, the maintenance contract is:

- the `web` container owns Web shell startup, heartbeat and cron
- the `worker` container owns the background task worker only
- both containers must share the same workspace state

The required durable paths are:

- `.g3ku/`
- `memory/`
- `sessions/`
- `temp/`
- `skills/`
- `tools/`
- `externaltools/`

Do not treat only `.g3ku/` as sufficient persistence. Detached task temp files live under `temp/tasks/`, external tool installs live under `externaltools/`, and mutable skill/tool resource copies may also need to survive restart.

When the deployment sets a data root outside the image (`G3KU_DATA_DIR`, or a `.g3ku/data-root.json` pointer), the data path needs its own volume mounted at that same absolute path in **both** containers, and the install-root `.g3ku/` still needs a volume of its own for config and key material — one volume covering both is not equivalent, because the two roots hold different contracts (see 「关键状态文件与目录」).

Deployment unlock has an operator-facing env contract:

- `G3KU_BOOTSTRAP_PASSWORD` allows a locked project to auto-unlock at process start
- `.g3ku/llm-config/auto-unlock.key` (written when the operator checks 记住密码自动解锁 in the web 设置 dialog) unlocks the project at start without any env var; it holds the master key, so treat it as a password
- `G3KU_INTERNAL_CALLBACK_URL` allows the worker container to call back into the web container over the Compose network instead of assuming `127.0.0.1`
- The official container image also pins text/runtime locale explicitly with `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, and `PYTHONIOENCODING=utf-8`. If container-only `exec`, validation-command, or Python traceback output shows mojibake, verify those three env vars before blaming prompt assembly or websocket rendering.

If Docker startup appears healthy but detached tasks never report back, inspect these in order:

1. the shared `.g3ku/internal-callback.json` payload
2. the effective `G3KU_INTERNAL_CALLBACK_URL` in both containers
3. whether `web` is healthy at `/api/bootstrap/status`
4. whether the worker container actually reached unlocked state

## 10. Memory Reset Workflow

`memory/` contains both user long-term memory data and unified-context retrieval state, including tool/skill catalog retrieval indexes. A full physical reset of the directory removes all of these together.

Operator expectations:

- `memory/` has no reset command: `negi memory cleanup-legacy` (dry-run unless `--apply`) removes legacy artifacts it lists, and `negi memory doctor` reports queue/health. Do not manually delete a subset of files.
- 配置包 的 `memory` scope ships exactly four things — the store, the mirror, and the two note-body directories — so a restored deployment comes up with the source's knowledge but an **empty queue**: `queue.jsonl`, `ops.jsonl`, `failed.jsonl` and `review_state.json` never travel, and per-session review progress is keyed by session ids that only exist on the machine that wrote it. Import replaces the store whole and deletes no note the package omits; unowned bodies are removed by the runtime's own sweep. Contract: 见 `config-and-models.md`「Config Bundle Export And Import」.
- The reset recreates baseline managed files and sync state, but it does not immediately rebuild tool/skill catalog retrieval inside the command itself.
- After reset, user long-term memory is empty.
- After reset, tool/skill semantic retrieval is also empty until the next runtime startup.
- On startup, the runtime should rebuild catalog retrieval automatically by syncing the resource catalog back into the unified context store.

If tool/skill retrieval does not return after restart, first inspect resource runtime initialization and then confirm that the memory runtime reaches a healthy catalog-bridge state.

## 11. 读口报 `SQLITE_CANTOPEN`：先分"库打不开"还是"临时目录建不出文件"

- 异常文本是 `unable to open database file`，而日志里印的 path 是**库文件的绝对路径**，于是"库坏了"与"SQLite 建不出自己的排序临时文件"在文本上完全同形。判据只能来自"这条语句是否需要落盘"，不能来自异常消息。
- 哪些读需要落盘：带 `ORDER BY` 的大表扫描（`list_worker_status`、`list_nodes`、`task_disk_usage` 摘要、`perf_samples` 读数）。主键点查与 `count(*)` 不落盘。所以**"部分端点 200、部分稳定 500"是这条故障的正常形状**，不要按端点差异去归因。
- 读车道失败时现场打一行诊断，判读顺序：`spill=` 与 `point=` 先对——`spill=failed:SQLITE_CANTOPEN` 而 `point=ok` ⇒ 临时目录这条路坏了，库没事；两者都失败 ⇒ 才往库文件、句柄、路径方向查。`cross_check[<另一条车道>]` 证的是"是不是只有一条连接坏"，`conn_age_s` 证"是不是运行中才坏"。
- 两个会把人带偏的读数：`tempfile.gettempdir()` 在候选目录不可写时**静默回落到工作目录**，于是打印出一个明明可写的路径；`os.access(dir, W_OK)` 只看 ACL，过滤驱动拦截下仍返回 True。落点假设一律用"真建一个文件""真做一次需要落盘的排序"来判。`PRAGMA temp_directory` 在当前 SQLite build 取不到行，`create temp table` 可以全程留在内存里，两者都不能当证据。
- 落点由运行时自己定：`negi web` / `negi worker` 入口第一件事是把 `TMP` / `TEMP` / `TMPDIR` 与 `tempfile` 一起指到数据根的 `temp/`（`g3ku/deployment/runtime_startup.py::anchor_process_temp_dir`），启动时打一行 `Runtime temp anchored: <路径>`，所以同一次安装不会因为"谁起的它"而换一种读故障。数据根那一侧也不可写时它放弃锚定并打 `Runtime temp anchor skipped`，保持继承到的环境不动——把一个可能还能用的环境换成一定能坏的更糟。判据仍按上一条走（`spill=` / `point=`），异常文本本身不给方向。
- 锚定之后 `<数据根>/temp/` 里住的不只是任务临时目录（`temp/tasks/`，孤儿由 `scripts/cleanup_orphan_task_temp_dirs.py` 收），还有 SQLite 排序落盘的 `sqlite_temp*` 文件。正常关闭时 SQLite 自己删；被硬杀（停机排水到点）才会留下孤儿，磁盘普查把这一目录当可回收 scratch 看。
