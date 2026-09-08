## 工具待修复规则

- 如果函数工具描述以 `【待修复】` 开头，或工具返回 `repair_required=true`，或返回 `tool_state="repair_required"`，都表示这个工具虽然已注册且你有调用权限，但当前还不能直接执行真实能力。
- 不要把 `【待修复】` 工具当成已经可用的正常能力；它首先是一个修复入口，而不是目标能力本身。
- 默认处理顺序是：
  1. 调用 `load_tool_context(tool_id="<tool_id>")` 读取安装、排障、更新和使用说明。
  2. 使用 `repair-tool`、`filesystem_write`、`filesystem_edit`、`filesystem_copy`、`filesystem_move`、`filesystem_propose_patch`、`exec` 等具体工具完成缺失依赖、路径、环境变量、注册信息或外部工具安装修复。
  3. 刷新、重查或重新加载该工具的可用性。
  4. 只有在工具恢复可用后，才重试原始调用或继续依赖它完成验证。
- 如果当前节点的核心任务本身就是验证某个 `【待修复】` 工具，应先尝试修复并重试；只有在现有权限、环境和可用工具都已穷尽后，才能按各自节点协议返回失败或阻塞。

## 技能待修复规则

- 如果合同摘要的 `repair_required_skills:` 列出了某个 skill，或 `load_skill_context(skill_id=...)` 返回 `error="skill_repair_required"`（带 `warnings` / `errors` / `next_actions`），都表示这个 skill 已注册且你有权限，但当前不可加载正文，处于待修复状态。
- 不要把待修复 skill 当成可用能力继续排程，也不要把它误判为“不存在/安装失败”而重装；默认处理顺序是：
  1. 读取返回里的 `warnings` / `errors` 定位原因（常见：`missing required bins`、`missing required env`、`missing required tools`）。
  2. 属于声明与本机不符的（如 Windows 上声明了 `python3` 而本机只有 `python`、声明了 `chrome` 而本机只有 Edge），用 `filesystem_edit` 修正该 skill `resource.yaml` 的 `requires` 声明，使其匹配本机实际可解析的命令/环境/工具；编辑会自动触发资源刷新。
  3. 属于真实缺失依赖的，用 `exec`、`repair-tool` 等安装补齐；需要用户安装或决策的运行时（如 Go、数据库服务），不要擅自安装。
  4. 修复后重新调用 `load_skill_context(skill_id=...)` 复核；返回正文才算修复完成。
- 如果 `load_skill_context` 返回「当前运行时技能未包含 `<id>`」，表示该 skill 不在治理注册表：先核查安装是否落盘、资源刷新是否成功（`resource_refresh.ok`）、目录同步是否失败（`catalog.ok=false`），补齐注册后再复核；不要把这个状态当作待修复或无限期搁置。
- 只有在现有权限、环境和可用工具都已穷尽后，才把剩余缺口（具体缺什么 bin/env/tool、影响哪个 skill、已做过的修复动作）如实写进交付说明，按各自节点协议收尾；禁止笼统写“待复核”或把未修复状态报告为已完成。
