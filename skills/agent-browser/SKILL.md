# agent_browser 浏览器技能

当用户要求你打开网站、搜索页面、点击元素、填写表单、登录网站、获取网页内容、截图或下载文件时，使用 `agent_browser` 工具。

## 使用时机

- 打开网站并搜索内容
- 点击按钮、链接、切换 tab
- 填写表单、登录站点
- 读取页面文本、URL、title、cookies、storage
- 截图、下载、保存 state

## 可见与后台模式

- 用户说“打开浏览器”、“让我看到浏览器操作”时，传 `headless=false`
- 后台鉴权、静默检查、内部自动化步骤时，传 `headless=true`

## 核心工作流

1. `open`
2. `snapshot -i`
3. 使用 `@e1`、`@e2` 这类 refs 执行 `click` / `fill` / `get`
4. 页面变化后再次 `snapshot -i`
5. 必要时 `wait`
6. 任务结束后按需 `close`

## 重新 snapshot 规则

以下情况发生后，旧 refs 可能已经失效，必须重新 `snapshot -i`：

- 页面导航
- DOM 更新
- 弹窗打开或关闭
- 切换 tab
- 登录成功后跳转

## 工具参数

- `command`：主命令，如 `open`、`snapshot`、`click`、`fill`、`get`、`wait`、`cookies`、`storage`、`screenshot`、`state`、`close`
- `args`：位置参数数组
- `headless`：`false` 可见，`true` 后台
- `session`：默认使用当前 Nano 会话

## 常见示例

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

要显式读取 `success`、`data`、`error`、`hint`、`stdout_raw`、`stderr`。如果页面已变化，先重新 `snapshot -i`，再继续交互。
