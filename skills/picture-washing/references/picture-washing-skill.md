Map the final compliant generation prompt to `picture_washing` tool parameters and execute the tool call.

## Tool Name

`picture_washing`

## Input Contract (config-first + override)

- `prompt` (string, required): final compliant generation prompt.
- `image` (string, required): product image URL or base64 data URL.
- `ratio` (string, required): output ratio such as `1:1`, `3:4`, `4:3`, `16:9`, `9:16`.
- `base_url` (string, optional override): DOUBAO service root URL.
- `authorization` (string, optional override): bearer token string or raw session id string.
- `authorization_probe_url` (string, optional override): page URL used for cookie probing when authorization is missing.
- `auto_probe_authorization` (boolean, optional override).
- `style` (string, optional override).
- `model` (string, optional override).
- `stream` (boolean, optional override).
- `timeout_s` (integer, optional override).

## Authorization Resolution Priority

1. If call args provide `authorization`, use it.
2. Otherwise use config default `tools.pictureWashing.authorization`.
3. If still missing and `auto_probe_authorization=true`, auto-call `agent_browser`:
- `launch`
- `goto` (`authorization_probe_url` -> config `authorizationProbeUrl` -> inferred from `base_url` root)
- `get_cookies`
- parse cookie `sessionid`/`session_id`
- compose `Bearer <sessionid>`
- `close`
4. If still missing, fail fast and return missing-field diagnostics.

## Mapping Rules

1. Pass final prompt from compliance step to `prompt` directly.
2. Pass product image to `image` directly.
3. Pass user ratio to `ratio` directly.
4. Pass override args only when caller explicitly provides them; otherwise rely on config defaults and auth auto-probe fallback.
5. Keep network and auth details out of hardcoded prompts.

## Tool Call Example (auto auth-probe mode)

```json
{
  "base_url": "http://localhost:8000",
  "prompt": "<final_compliant_prompt>",
  "image": "https://example.com/product.png",
  "ratio": "1:1"
}
```

## Tool Call Example (explicit authorization override)

```json
{
  "base_url": "http://localhost:8000",
  "authorization": "Bearer <sessionid>",
  "prompt": "<final_compliant_prompt>",
  "image": "https://example.com/product.png",
  "ratio": "1:1"
}
```

## Response Contract

The tool returns standardized JSON string fields:

- `success` (boolean)
- `error` (string or null)
- `requestMeta` (object, includes `authorizationSource` and `authorizationProbe`)
- `images` (array of URLs)
- `raw` (raw response fragment)

## Failure Handling

- Missing `base_url`: fail fast.
- Missing auth after auto-probe: fail fast and return probe diagnostics.
- `401/403`: auth invalid or expired. Ask caller to re-login and retry.
- `429`: rate limited. Retry with backoff.
- `5xx`/timeout/network error: service unstable. Retry or switch `base_url`.
- `success=false` with empty `images`: include `raw` in diagnostics and mark this reference image as failed.
