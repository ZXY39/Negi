将最终符合合规要求的生成 Prompt 映射到 `picture_washing` 工具参数，并执行工具调用。

## 工具名称 (Tool Name)

`picture_washing`

## 输入契约 (Input Contract) (配置优先 + 覆盖更新)

- `prompt` (字符串，必需)：最终合规的生成 Prompt。
- `image` (字符串，必需)：产品图 URL 或 base64 数据 URL。
- `ratio` (字符串，必需)：输出比例，如 `1:1`, `3:4`, `4:3`, `16:9`, `9:16`。
- `base_url` (字符串，可选覆盖)：豆包 (DOUBAO) 服务根 URL。
- `authorization` (字符串，可选覆盖)：Bearer Token 字符串或原始 session id 字符串。
- `authorization_probe_url` (字符串，可选覆盖)：当授权信息缺失时，用于获取 Cookie 的页面 URL。
- `auto_probe_authorization` (布尔值，可选覆盖)。
- `style` (字符串，可选覆盖)。
- `model` (字符串，可选覆盖)。
- `stream` (布尔值，可选覆盖)。
- `timeout_s` (整数，可选覆盖)。

## 授权解析优先级 (Authorization Resolution Priority)

1. 如果调用参数提供了 `authorization`，则使用它。
2. 否则，使用配置默认值 `tools.pictureWashing.authorization`。
3. 如果仍缺失且 `auto_probe_authorization=true`，则自动调用 `agent_browser`：
- `launch` (启动)
- `goto` (跳转) (`authorization_probe_url` -> 配置 `authorizationProbeUrl` -> 从 `base_url` 根路径推断)
- `get_cookies` (获取 Cookie)
- 解析 Cookie 中的 `sessionid` / `session_id`
- 组合成 `Bearer <sessionid>`
- `close` (关闭)
4. 如果仍然缺失，则快速失败并返回缺失字段诊断信息。

## 映射规则 (Mapping Rules)

1. 将来自合规性检查步骤的最终 Prompt 直接传递给 `prompt`。
2. 将产品原图直接传递给 `image`。
3. 将用户要求的比例直接传递给 `ratio`。
4. 仅当调用者显式提供覆盖参数时才传递它们；否则依赖配置默认值和授权自动探测机制。
5. 请勿在硬编码的 Prompt 中包含网络和授权详情。

## 工具调用示例 (自动授权探测模式)

```json
{
  "base_url": "http://localhost:8000",
  "prompt": "<最终合规生成的_prompt>",
  "image": "https://example.com/product.png",
  "ratio": "1:1"
}
```

## 工具调用示例 (显式授权覆盖)

```json
{
  "base_url": "http://localhost:8000",
  "authorization": "Bearer <sessionid>",
  "prompt": "<最终合规生成的_prompt>",
  "image": "https://example.com/product.png",
  "ratio": "1:1"
}
```

## 响应契约 (Response Contract)

该工具返回标准化的 JSON 字符串字段：

- `success` (布尔值)
- `error` (字符串或 null)
- `requestMeta` (对象，包含 `authorizationSource` 和 `authorizationProbe`)
- `images` (URL 数组)
- `raw` (原始响应片段)

## 失败处理 (Failure Handling)

- 缺失 `base_url`: 快速失败。
- 自动探测后仍缺失授权: 快速失败并返回探测诊断信息。
- `401/403`: 授权无效或已过期。要求调用者重新登录并重试。
- `429`: 触发限流。使用退避算法重试。
- `5xx` / 超时 / 网络错误: 服务不稳定。重试或切换 `base_url`。
- `success=false` 且 `images` 为空: 在诊断信息中包含 `raw` 片段，并将该参考图标记为“失败”。
