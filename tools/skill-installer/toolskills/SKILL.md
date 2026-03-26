# skill-installer

把一个现成 GitHub repo/path 里的 skill 安装到当前工作区的 `skills/` 目录。

这条工具链只负责 **GitHub repo/path -> 本地 `skills/`**。
如果用户要从 `clawhub.ai` 搜索、下载、安装或更新现成 skill，不要继续使用本工具；转到 `load_skill_context(skill_id="clawhub-skill-manager")`，按 `clawhub-skill-manager` 的唯一工作流处理。ClawHub 来源的 skill 默认是面向第三方项目的上游素材，安装前要先评估是否需要按 G3KU 要求重写。

## 何时使用

- 用户给的是 GitHub skill 链接，目标是“直接装进当前 G3KU 环境”。
- 用户给的是 `owner/repo + path`，并且不想手工 clone、复制、补 `resource.yaml`。
- 上游只有 `SKILL.md` 没有 `resource.yaml`，需要自动补齐成可被 G3KU 资源系统发现的本地 skill。

## 不要在这些情况使用

- 用户明确提到 `clawhub.ai`、ClawHub 页面链接、ClawHub `slug`
- 用户要“搜索 skill / 下载 skill / 安装 skill / 更新 skill”，且来源是 ClawHub
- 用户要检查某个由 ClawHub 安装到本地的 skill 是否有新版

如果用户要的是“重写/拆分/重新设计 skill”，先安装，再决定是否继续走 `skill-creator`。对于来自 ClawHub 的 skill，这种二次适配通常是常态而不是例外。

## 使用

最常见的两种调用方式：

- 直接传 GitHub URL：
  - `url`: `https://github.com/<owner>/<repo>/tree/<ref>/<path/to/skill>`
- 传 repo + path：
  - `repo`: `<owner>/<repo>`
  - `path`: `<path/to/skill>`
  - `ref`: 可选，默认 `main`

可选参数：

- `dest`: 自定义目标目录。相对路径从当前 workspace 解析；默认安装到 `skills/<basename>`。
- `name`: 当上游 skill 没有 `resource.yaml` 时，作为本地 skill id 写入自动生成的 `resource.yaml`。
- `method`: `auto | download | git`
  - `auto` 默认优先走 `git sparse-checkout`，失败后再回退到 GitHub codeload zip
  - git 回退会改用新的临时 clone 目录，避免 Windows 上被占用的 `.git/objects/pack/*` 阻塞第二次 clone
  - 临时目录清理使用 `ignore_cleanup_errors=True`，避免安装已经完成却因为临时锁文件残留而被误判失败
  - `git` 与下载都带明确超时，超时后会直接返回错误，不会长期停在 `Working...`

默认设置来自 `tools/skill-installer/resource.yaml -> settings`：

- `download_timeout`
- `git_timeout`
- `auto_prefer`

## 强取消

当前版本支持强取消：

- 通过统一 cancellation token 感知会话暂停 / 取消
- 对正在运行的 `git` 子进程登记句柄，并在取消时主动 `terminate/kill`
- 对下载、解压、文件复制这些长步骤在阶段边界主动检查取消状态
- 当用户请求暂停时，会先显示“用户已请求暂停，正在安全停止...”再进入停止流程

返回结果是 JSON，重点看：

- `skill_id`
- `installed_path`
- `manifest_created`
- `method`
- `warnings`

## 行为边界

- 目标目录必须位于当前 workspace 内。
- 已存在的目标目录不会被覆盖；如需重装，先手动删除旧目录再调用。
- 如果上游自带 `resource.yaml`，工具会保留它，不会强改上游 skill id。
- 如果上游只有 `SKILL.md`，工具会自动生成最小可用的 `resource.yaml`，并触发一次资源刷新。
- 本工具不负责 ClawHub 搜索、下载或更新；这些请求统一走 `clawhub-skill-manager`。

## 失败与回退

常见失败：

- GitHub URL 不是目录或 `SKILL.md` 路径
- `repo + path` 缺失其一
- 目标目录已存在
- 下载失败且本机没有 `git`
- 选中的目录里没有 `SKILL.md`

回退方式：

- 先检查返回里的 `error`
- 如果是目录冲突，删除旧目录后重试
- 如果是下载失败，改用 `method="git"` 或安装 `git`
- 如果上游结构不标准，先安装可行部分，再交给 `skill-creator` 做二次适配
- 如果日志里出现 `WinError 5` 指向临时目录中的 `.git/objects/pack/*`，优先升级到当前修复版本后再重试

## 安装与更新

这是仓库内置工具：

- 无需独立安装，代码位于 `tools/skill-installer/main/tool.py`
- 更新方式是同步修改：
  - `tools/skill-installer/resource.yaml`
  - `tools/skill-installer/main/tool.py`
  - `tools/skill-installer/toolskills/SKILL.md`
