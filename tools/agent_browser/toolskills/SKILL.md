# agent_browser

当用户希望你打开网站、搜索页面、点击元素、填写表单、登录网站、读取页面内容、截图或下载文件时，使用 `agent_browser`。

## 适用场景

- 打开网页并搜索内容
- 点击按钮、链接、切换 tab
- 填写表单、执行登录
- 读取页面文本、URL、title、cookies、storage
- 截图、下载、保存 state

## 可见/后台模式

- 用户明确说“打开浏览器”“让我看到操作”时：`headless=false`
- 后台探查、静默检查时：`headless=true`

## 核心流程

1. `open`
2. `snapshot -i`
3. 用 `@e1`、`@e2` 等 refs 执行 `click` / `fill` / `get`
4. 页面变化后再次 `snapshot -i`
5. 必要时 `wait`
6. 任务完成后按需 `close`

## 需要重新 snapshot 的情况

以下情况 refs 可能失效，必须重新 `snapshot -i`：

- 页面导航发生变化
- DOM 更新
- 弹窗打开/关闭
- 切换 tab
- 登录成功后的跳转

## 编排规则

- 目标页面或路径不明确时，优先搜索或轻量检索，再决定是否打开浏览器。
- 只有在用户明确需要可见操作、截图、页面检查或导航时，才使用浏览器动作。

## 操作规则

- 优先输出简洁、可证据支持的总结。
- 浏览器动作失败时，说明具体阻塞原因，并给出最小恢复步骤。

## 参数

- `command`：主命令，如 `open`、`snapshot`、`click`、`fill`、`get`、`wait`、`cookies`、`storage`、`screenshot`、`state`、`close`
- `args`：位置参数数组
- `session`：可选浏览器会话覆盖值，默认当前 Nano 会话
- `headless`：是否后台模式。用户要看可见浏览器时用 `false`
- `timeout_s`：可选命令超时时间（秒）

## 示例

```json
{"command":"open","args":["https://www.bilibili.com"],"headless":false}
```

```json
{"command":"snapshot","args":["-i"],"headless":false}
```

```json
{"command":"click","args":["@e3"],"headless":false}
```

## 错误处理

显式检查 `success`、`data`、`error`、`hint`、`stdout_raw`、`stderr`。如果页面已变化，先 `snapshot -i` 再继续。
