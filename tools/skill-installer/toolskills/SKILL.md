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

## 安装后必须复核

默认自动完成安装与 manifest 补齐后，**不要直接把结果视为已经完全可用**。后续必须立刻做两步检查：

1. **检查 skill 是否处于待修复状态**
   - 先确认该 skill 不在当前运行时合同的 `repair_required_skills` 中。
   - 再尝试 `load_skill_context(skill_id="<skill_id>")`；如果返回的是修复指引、不可用提示，或资源本身带 warnings/errors，就说明它仍是待修复状态。
   - 一旦发现待修复，不要继续把它当成正常 skill 使用；先修复，再重新检查。

2. **必须检验一遍 `resource.yaml` 是否合规**
   - 自动生成的 `resource.yaml` 只是“尽量补齐”，不是最终验收结果。
   - 合规标准至少按下面清单逐项核对；任何不合规点都要立即修改。

`resource.yaml` 合规标准：

1. `schema_version` 必须是 `1`，`kind` 必须是 `skill`。
2. `name` 必须是最终要在本地使用的稳定 skill id，并与安装目录、后续调用 id 保持一致。
3. `description` 必须完整、准确、适合检索；不能保留被截断、多行丢失、语义含糊或与真实用途不符的描述。
4. `trigger.keywords` 必须覆盖真实触发表达；如果当前关键词不足以让模型稳定想到它，要立即补齐。
5. `requires.tools / bins / env` 必须反映 skill 的真实依赖；不能因为自动生成时未识别就长期保留空数组。
6. `content.main` 必须指向 `SKILL.md`；若存在 `references/`、`scripts/`、`assets/`，`content` 中也应显式声明。
7. `exposure.agent` 与 `exposure.main_runtime` 必须符合预期使用范围，不能靠默认值蒙混过关。
8. 若本次导入来自 GitHub，`source.type / url / repo / ref / path` 必须与真实来源一致，便于后续追溯。
9. 如果 skill 实际上并不适合直接作为 G3KU 本地 skill 使用，而只是上游素材，应立刻转 `skill-creator` 做结构适配，而不是带着不合规 manifest 继续使用。

结论要求：

- 只要发现不合规点，就要**立即修改**，不要把“先装上再说”当成完成。
- 只有在“非待修复”且 `resource.yaml` 通过上述清单后，才能把安装结果视为可继续使用的本地 skill。

## 行为边界

- 目标目录必须位于当前 workspace 内。
- 已存在的目标目录不会被覆盖；如需重装，先手动删除旧目录再调用。
- 如果上游自带 `resource.yaml`，工具会保留它，不会强改上游 skill id。
- 如果上游只有 `SKILL.md`，工具会自动生成一个结构感知的 `resource.yaml`，并触发一次资源刷新，但你仍然必须按上面的复核流程检查并修正。
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
