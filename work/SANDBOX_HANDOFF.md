# 沙箱执行面：上机验收接续指引（可移植版）

适用：把「沙箱执行面」验收清单里因环境阻塞未能上机的那几条，搬到能装后端的机器上跑完。
本文件只含通用命令与判据，**不含任何机器标识、账号标识、会话键或本地绝对路径**。
完整验收记录留在原测试机本地，未入库。

代码基线要求：`main` ≥ `83c9192c6`（含"整批被策略拒时不得回退执行整批"的修复）。

## 0. 先选一条后端路

| 路 | 覆盖清单 | 备注 |
|---|---|---|
| Linux + bubblewrap | 1–9、11、12 | 最省事；bwrap 与 WSL 后端共用同一份 profile |
| Windows + WSL2 + bwrap | 1–11、12（含第 10 条） | 只有这条路能验 `/mnt`、`/init`、`.exe` |
| macOS + sandbox-exec | 1–9、11、12 | 第 10 条 N/A；`process` / `resource` 维度会是 `missing` |

## 1. 装后端

### Linux（Ubuntu 22.04 / Debian 12 推荐）

```bash
sudo apt-get update && sudo apt-get install -y bubblewrap python3 python3-venv git curl
bwrap --version
# 冒烟：非特权 user namespace 能不能建起来
bwrap --unshare-all --die-with-parent --ro-bind /usr /usr \
      --symlink usr/lib /lib --proc /proc --dev /dev -- /bin/true && echo BWRAP_OK
```

> Ubuntu 24.04 起 AppArmor 限制非特权 user namespace，上面这条会失败。
> 处置：换 22.04 / Debian 12，或临时
> `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`（仅测试机）。

### Windows（宿主必须已向 guest 暴露 VT-x；普通云主机一般不满足）

```powershell
wsl --install -d Ubuntu-24.04
wsl -d Ubuntu-24.04 -u root -- sh -lc "apt-get update && apt-get install -y bubblewrap python3"
# 默认用户不能是 root，否则会话卷落在 /root 会被禁绑断言打死（见第 6 节坑 2）
wsl -d Ubuntu-24.04 -u root -- sh -lc "adduser -D negi && mkdir -p /home/negi && chown -R negi:negi /home/negi && printf '[user]\ndefault=negi\n' > /etc/wsl.conf"
wsl --terminate Ubuntu-24.04
wsl -d Ubuntu-24.04 -- sh -lc 'id; echo HOME=$HOME; command -v bwrap; command -v python3'
```

## 2. 起服务并读安装态（清单第 1 条）

```bash
git clone https://github.com/ZXY39/Negi.git && cd Negi && ./install.sh
# 第 3–10 条靠真实模型回合：要么带一份能用的模型链，要么在这台机器上重新配一条
curl -s "http://127.0.0.1:18790/api/sandbox/status?refresh=1"
```

期望读数：`install_state=ready`、`enforcement.fs_read/fs_write/network` 均为 `full`、
`backend` 为 `bwrap` 或 `wsl_bwrap`、`ui_labels` 变成「开启沙箱 / 关闭沙箱」。

## 3. 开闸并造一个 jailed 会话（清单第 2 条）

```bash
# 先优雅停旧实例，再带闸门重启
G3KU_SANDBOX_ENABLE_JAIL=1 python -m g3ku web --host 127.0.0.1 --port 18790

curl -s "http://127.0.0.1:18790/api/ceo/sessions" | head -c 400      # 取会话 id
curl -s -X PATCH -H 'Content-Type: application/json' \
     -d '{"mode":"restricted"}' \
     "http://127.0.0.1:18790/api/ceo/sessions/<id>/sandbox"
```

期望读数：`resolved_tier=jailed`、`backend` 非空、`tasks_allowed=false`、
`effective_from="next_turn"`。owner 会话切档不需要口令；第三方会话切 `unrestricted` 才要项目解锁口令。
档位只在回合边界生效，**发探针之前先走完一跳**。

## 4. 第 3–9 条：在 jailed 会话里逐条发

| # | 发给模型 | 期望 |
|---|---|---|
| 3 | `exec pwd`，再 `ls /` | `pwd` = `/workspace`；`ls /` 只有 workspace/shared/tmp/proc/dev/usr/bin/lib 之类，**没有任何宿主目录树** |
| 4 | `exec cat <宿主数据根>/.g3ku/master.key`；再用 `content_open` 打同一个宿主绝对路径 | 两条都失败；一条"文件不存在"、一条 `host path is not reachable inside the sandbox`；**不许出现"读到只读副本"** |
| 5 | `exec sh -c 'echo x > /workspace/x && cat /workspace/x'`，再写 `/etc/x`、`/home/x` | workspace 成功；`/etc`、`/home` 失败 |
| 6 | `exec curl -sS -m 5 https://example.com` | 失败（`network=none`） |
| 7 | 打宿主 web 端口、`/var/run/docker.sock`、`$SSH_AUTH_SOCK` | 全部失败：jail 里够不到宿主控制面 |
| 8 | `exec env` | 只有 `PATH/HOME/TMPDIR/LANG/SANDBOX_ID`；**无任何 `G3KU_*`、模型 key、master key、渠道 secret** |
| 9 | `exec ps -ef`；同时在宿主 `ss -lntp`（Linux）或 `netstat -ano`（Windows）对比开闸前后 | jail 内没有 Negi/web/worker 进程；宿主**没有新增监听端口** |

每条都记下工具回执原文，并同时留一份服务端证据（见第 7 节）。

## 5. 第 10 条（只有 WSL 路要跑）

```
exec ls -la /mnt                                      → 空目录
exec sh -c 'test -e /init && echo PRESENT || echo ABSENT'   → ABSENT
exec notepad.exe                                      → 失败
```

Linux / macOS 路这一条记 **N/A**，不要记成通过。

## 6. 跑到不通时先排这三个坑

1. **P1（已修）**：一批工具调用全部被策略拒绝时，旧代码会把"空执行名单"当成"没设置"而回退执行整批，
   被拒的调用照样产生副作用。`83c9192c6` 已修；若新环境不是这根基线，会看到"被拒但生效"。
2. **WSL 默认用户是 root**：会话卷落在 `/root/.negi-sandbox/...`，而 `/root` 在禁绑前缀里，
   `build_bwrap_argv` 会抛 `refusing to bind forbidden host path into the jail` ——
   探测报 `ready`、每次数据面调用全失败。先按第 1 节建非 root 默认用户再开闸。
3. **安装态探测不判 `wsl -l -q` 的返回码**：WSL 未安装时桩程序的提示文本会被当成发行版名，
   `install_available` 误报 true、安装计划指向一个不存在的发行版。

## 7. 需要带回来的证据

- `GET /api/sandbox/status?refresh=1` 与 `GET /api/ceo/sessions/<id>/sandbox` 的**原始 JSON**；
- 该会话最新的请求原件（`<数据根>/web-ceo-requests/<session>/`，每轮取最后一个文件）里的
  `actual_tool_schema_hash`、`stable_prefix_hash`、`tools[]` 名字数与名单；
- web 进程日志里与 `Cron:` / `bwrap:` / `sandbox` 相关的行；
- 宿主监听对比：开闸前后各一份 `ss -lntp` / `netstat -ano`；
- 每条探针的**工具回执原文**，失败时把 stderr 一起带上；
- 某条不通时，再抓一份该次调用的实际 argv（宿主进程列表里的 `bwrap` 命令行），
  用来区分"profile 写松了"还是"路由没接上"。

## 8. 第 11、12 条的跑法与已知偏差

- **第 11 条（降级）**：去掉 `G3KU_SANDBOX_ENABLE_JAIL` 重启 → 第三方（`ext:`）会话
  `resolved_tier` 回 `readonly`，`exec` 不在其 `tools[]` 内；若模型仍发出名单外调用，
  回执必须是策略拒绝且**没有任何宿主副作用**（cron / 文件 / 进程都不许变）。
  已在无后端环境实测过这一条：只读档解析、宿主绝对路径被读根拒、记忆三本账 0 命中。
- **第 12 条（缓存）**：同一会话 `restricted(jailed)` ↔ `unrestricted(host_full)` 各跑一轮，
  对比两跳的 `actual_tool_schema_hash`。当前实现的**声明面 `tools[]` 会随档位变**
  （无后端环境实测：第三方只读 11 个 vs owner 全权限 35 个），与清单里"切档前后 `tools[]`
  字节不变"冲突；同档内字节稳定已实测通过（同一会话 14 跳哈希一致）。
  另外头部沙箱声明（`## Sandbox Environment` / `## Read-only Session`）**代码里还不存在**，
  这一句在实现补上之前必然过不了。

## 9. 判定口径

任何一条失败，就把 `G3KU_SANDBOX_ENABLE_JAIL` 留在未设置状态；此时第三方会话仍落只读档、
数据面工具回策略拒绝，项目其余功能不受影响。全部通过后才保留该环境变量。
