# G3KU 配置与模型系统说明

本文档解释项目配置如何加载、模型如何绑定到运行时、哪些数据写在 `.g3ku/config.json`，哪些数据在 `llm-config` 存储里。

## 1. 配置入口

项目配置的统一入口是：

- `g3ku/config/loader.py`
- `g3ku/config/schema.py`

其中：

- `loader.py` 负责读取、迁移、保存、强校验
- `schema.py` 定义完整的 Pydantic 配置模型

当前项目明确要求配置从项目本地路径读取：

- `.g3ku/config.json`

## 2. 配置模型的几个核心部分

### `agents`

定义：

- 默认 workspace
- runtime 模式
- 温度、max tokens、memory window
- role iterations / concurrency
- multi-agent 配置

### `models`

定义：

- `catalog`
  管理模型条目
- `roles`
  把模型键映射到 `ceo / execution / inspection`

### `providers`

保存 provider 级基础信息，例如：

- `api_key`
- `api_base`
- `extra_headers`

### `web`

定义 Web bind host / port。

### `resources`

定义 skills / tools 资源目录与 reload 策略。

### `main_runtime`

定义任务运行时存储与调度参数。

### `china_bridge`

定义 Node 宿主、控制端口、自动启动、中国渠道配置。

## 3. 配置加载时做了什么

`load_config()` 不是简单读 JSON，它会做很多迁移和约束检查：

- legacy 字段迁移
- gateway / channels / old tools config 清理
- role iteration / concurrency 补默认值
- LLM 相关旧配置迁移
- secret overlay 应用
- 运行时字段显式性校验

这意味着：

- 启动时报配置错误，很多不是 JSON 语法错，而是 schema contract 变更
- 不要手写“看起来像旧版”的配置字段，loader 会直接拒绝

## 4. 配置热刷新

运行时读取不总是直接 `load_config()`，而是走 `g3ku/config/live_runtime.py` 的 `get_runtime_config()`：

- 按 `.g3ku/config.json` 的 `mtime` 检测变更
- 维护 revision
- 刷新失败时保留 last good config

这对维护者很重要，因为它解释了两个现象：

- 改配置后为什么有时不必整进程重启
- 配置写坏时为什么服务可能还暂时“看起来能跑”

管理面保存模型配置时，还要区分两层刷新：

1. Web 进程自己的 runtime refresh
2. Web 托管 worker 的 runtime refresh

当前行为是：

- 保存类接口先写盘
- 写盘成功后立即返回成功响应
- worker 刷新改为异步命令，通过 `task_commands` 中的 `refresh_runtime_config` 记录确认是否真正应用

因此，`200 OK` 只表示配置已经保存成功；它不等价于“worker 已经确认加载新配置”。

## 5. 模型系统不是只靠 `config.json`

这是新人最容易误解的一点。

G3KU 的模型系统分两层：

1. 项目配置中的模型绑定与角色路由
   `models.catalog`
   `models.roles`

2. `llm-config` 子系统里的 provider config record
   位于 `.g3ku/llm-config/`

`config.json` 更像“项目如何引用模型”，而不是所有模型秘密和 provider 配置的最终存储地。

## 6. `llm_config` 子系统

关键入口是：

- `g3ku/llm_config/facade.py`

它负责：

- provider config record 的增删改查
- 绑定模型 key 到 config record
- memory embedding / rerank 绑定
- 导出 runtime target
- 把 secrets 存进安全 overlay，而不是明文长期放在 record 中

## 7. 运行时是如何拿到模型的

典型路径如下：

1. `Config.resolve_role_model_key("ceo")`
2. `Config.get_scope_model_target(...)`
3. `bootstrap_factory.make_provider(...)`
4. `g3ku.providers.chatmodels.build_chat_model(...)`

若某模型条目绑定了 `llm_config_id`，则：

- `Config` 会借助 `LLMConfigFacade.get_binding(...)` 解析真实 provider/model

这意味着：

- 运行时看的是“role -> model key -> binding -> provider target”
- 不是简单的“role 直接写死 provider:model”

## 8. secret 的真实去向

配置里的 secret 不一定直接写回文件。

当前机制里：

- `config.json` 里会保留结构性配置
- 真正 secret 通过 bootstrap security overlay 管理
- `LLMConfigFacade` 存 record 时会清洗掉明文 secret，再把 secret 写入 overlay

所以：

- 如果你看到某个 config record 没有明文 api key，不代表配置丢了
- 排查模型鉴权问题时，不能只看 JSON 文件

## 9. China bridge 配置

中国渠道统一走：

- `chinaBridge.channels.<channel-id>`

而不是历史上的 `channels.*`。

loader 会显式拒绝 legacy `channels.*` 配置，这一点在迁移和排障时很重要。

支持的 canonical ids：

- `qqbot`
- `dingtalk`
- `wecom`
- `wecom-app`
- `wecom-kf`
- `wechat-mp`
- `feishu-china`

## 10. 常见排障入口

### 启动时报配置字段错误

先看：

- `g3ku/config/loader.py`
- `g3ku/config/schema.py`

### Web 里显示没模型可用

先看：

- `models.roles.ceo`
- `models.catalog`
- `g3ku/llm_config/facade.py`

### memory embedding / rerank 不生效

先看：

- `.g3ku/llm-config/memory_binding.json`
- `LLMConfigFacade.get_memory_binding()`

### China bridge 配置改了但宿主行为没更新

先看：

- `build_runtime_config_payload(...)`
- `g3ku/shells/web.py` 中的 refresh / sync china bridge 逻辑

## 11. 维护高风险点

- `g3ku/config/loader.py`
  同时承担迁移、校验、保存；改动容易破坏老项目兼容。

- `g3ku/config/schema.py`
  是 runtime contract 源头，一旦字段语义改动，前后端与运行时都可能受影响。

- `g3ku/llm_config/facade.py`
  涉及 secret、record、binding、memory target，多条模型链路都会经过这里。

## Frontdoor Compression Config Cleanup

The frontdoor compression config surface is now token-based only for long-context summarization.

- `frontdoor_global_summary_*` fields remain the supported runtime entrypoints.
- The older `frontdoor_*message_count` settings were removed from `MemoryAssemblyConfig` rather than kept as compatibility no-ops.

This matters for maintenance because a stale config file using the removed message-count keys should now be treated as outdated configuration, not as silently ignored tuning.

If a user reports that frontdoor compaction settings stopped working after upgrade, check:

- whether they are still trying to set the removed message-count fields
- whether they migrated to the `frontdoor_global_summary_*` token/ratio settings instead
