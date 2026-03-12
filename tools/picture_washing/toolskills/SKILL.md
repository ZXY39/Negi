# picture_washing

Generate images for picture-washing by calling a DOUBAO-compatible POST /v1/images/generations endpoint. Use config-injected defaults for base_url/authorization when not provided by call args. When authorization is missing, auto-probe session cookie via agent_browser and compose Bearer token.

## Parameters
- `base_url`: Optional override. DOUBAO service base URL, e.g. http://localhost:8000. If omitted, use tools.pictureWashing.baseUrl from config.
- `authorization`: Optional override. Bearer token or raw sessionid string. If omitted, use tools.pictureWashing.authorization from config, or auto-probe session cookie via agent_browser if enabled.
- `authorization_probe_url`: Optional override. URL used by agent_browser when authorization is missing. If omitted, use tools.pictureWashing.authorizationProbeUrl or infer from base_url.
- `auto_probe_authorization`: Optional override to enable/disable authorization auto-probing.
- `prompt`: Final compliant image prompt.
- `image`: Product image URL or data:image/...;base64,...
- `ratio`: Target ratio, e.g. 1:1, 3:4, 4:3, 16:9, 9:16.
- `style`: Optional generation style override.
- `model`: Optional generation model override.
- `stream`: Optional stream override.
- `timeout_s`: Optional HTTP timeout override in seconds.

## Usage
Use `picture_washing` only when it is the most direct way to complete the task.
