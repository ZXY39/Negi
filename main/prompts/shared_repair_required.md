## 工具待修复规则

- 函数工具描述以 `【待修复】` 开头，或工具返回 `repair_required=true` / `tool_state="repair_required"`，都表示这个工具虽然已注册且你有调用权限，但当前还不能直接执行真实能力：它是修复入口，不是目标能力。修复前不要重试原始调用，也不要把它当正常能力排程。
- 处置顺序（先 `load_tool_context(tool_id="<tool_id>")` 读安装与排障说明，再修复、刷新复核，恢复可用后才重试原始调用）住 skill `repair-tool`，按它执行。当前节点的核心任务本身就是验证某个待修复工具时，同样先修再验；现有权限、环境和可用工具都已穷尽后，才能按各自节点协议返回失败或阻塞。

## 技能待修复规则

- 合同的 `repair_required_skills:` 列出某个 skill，或 `load_skill_context(skill_id=...)` 返回 `error="skill_repair_required"`（带 `warnings` / `errors` / `next_actions`），都表示这个 skill 已注册且你有权限，但当前不可加载正文。不要当可用能力继续排程，也不要误判为“不存在/安装失败”而重装。
- 默认修复路径：先读返回里的 `warnings` / `errors` 定位原因（常见 `missing required bins`、`missing required env`、`missing required tools`）。属于声明与本机不符的（如 Windows 上声明了 `python3` 而本机只有 `python`、声明了 `chrome` 而本机只有 Edge），用 `filesystem_edit` 修正该 skill `resource.yaml` 的 `requires` 声明，使其匹配本机实际可解析的命令/环境/工具；编辑会自动触发资源刷新。判据只有两条——该项在本机实测已有，或该 skill 正文并不使用该项；正文仍需要的项一律不许删，删掉声明同样会让下一步返回正文，那不算修复。属于真实缺失依赖的，用 `exec` 安装补齐；需要用户安装或决策的运行时（如 Go、数据库服务），不要擅自安装。修复后重新调用 `load_skill_context(skill_id=...)` 复核；返回正文才算修复完成。`requires` 声明与探测条目的写法见 skill `skill-creator`。
- 如果 `load_skill_context` 返回「当前运行时技能未包含 `<id>`」，表示该 skill 不在治理注册表：核查安装是否落盘、资源刷新与目录同步是否成功（读数在 skill-installer 安装回执的 `resource_refresh.ok` / `catalog.ok`），补齐注册后再复核；不要把这个状态当作待修复或无限期搁置。
- 只有在现有权限、环境和可用工具都已穷尽后，才把剩余缺口（具体缺什么 bin/env/tool、影响哪个资源、已做过的修复动作）如实写进交付说明，按各自节点协议收尾；禁止笼统写“待复核”或把未修复状态报告为已完成。
