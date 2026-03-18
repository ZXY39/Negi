# skill-installer

把一个现成 GitHub repo/path 里的 skill 安装到当前工作区的 `skills/` 目录。

## 何时使用

- 用户给的是 GitHub skill 链接，目标是“直接装进当前 G3KU 环境”。
- 用户给的是 `owner/repo + path`，并且不想手工 clone、复制、补 `resource.yaml`。
- 上游只有 `SKILL.md` 没有 `resource.yaml`，需要自动补齐成可被 G3KU 资源系统发现的本地 skill。

如果用户要的是“重写/拆分/重新设计 skill”，先安装，再决定是否继续走 `skill-creator`。

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
  - `auto` 先走 GitHub codeload zip，失败后回退到 `git sparse-checkout`
  - git 回退重试前会清理临时 clone 目录，避免上一次失败留下的非空 `repo/` 阻塞第二次 clone

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

## 安装与更新

这是仓库内置工具：

- 无需独立安装，代码位于 `tools/skill-installer/main/tool.py`
- 更新方式是同步修改：
  - `tools/skill-installer/resource.yaml`
  - `tools/skill-installer/main/tool.py`
  - `tools/skill-installer/toolskills/SKILL.md`
