# picture_washing (图片洗稿工具)

通过调用兼容豆包 (DOUBAO) 的 `POST /v1/images/generations` 端点来为图片洗稿生成图像。当调用参数中未提供 `base_url` 或 `authorization` 时，使用配置中注入的默认值。如果缺少授权信息，将通过 `agent_browser` 自动探测会话 Cookie 并组合成 Bearer 令牌。

## 参数 (Parameters)
- `base_url`: 可选覆盖。豆包服务的根 URL，例如 `http://localhost:8000`。如果省略，则使用配置中的 `tools.pictureWashing.baseUrl`。
- `authorization`: 可选覆盖。Bearer 令牌或原始 sessionid 字符串。如果省略，则使用配置中的 `tools.pictureWashing.authorization`；如果启用了自动探测，则通过 `agent_browser` 自动探测会话 Cookie。
- `authorization_probe_url`: 可选覆盖。当缺少授权信息时，`agent_browser` 使用的 URL。如果省略，使用 `tools.pictureWashing.authorizationProbeUrl` 或从 `base_url` 推断。
- `auto_probe_authorization`: 可选覆盖，用于启用/禁用授权信息的自动探测。
- `prompt`: 最终生成的符合要求的图片提示词。
- `image`: 产品图 URL 或 `data:image/...;base64,...`。
- `ratio`: 目标比例，如 `1:1`, `3:4`, `4:3`, `16:9`, `9:16`。
- `style`: 可选的生成样式覆盖。
- `model`: 可选的生成模型覆盖。
- `stream`: 可选的流式输出覆盖。
- `timeout_s`: 可选的 HTTP 超时覆盖（秒）。

## 用法 (Usage)
仅当 `picture_washing` 是完成任务最直接的方式时才使用它。

