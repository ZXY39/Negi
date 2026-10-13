# model_config

Manage .g3ku/config.json model catalog, provider templates/drafts/bindings, and role model chains.

## Parameters
- `action`: Model config action to perform.
- `key`: Managed model key.
- `new_key`: Target key for `rename_model`.
- `provider_model`: Provider:model identifier or managed model reference.
- `provider_id`: Provider template id.
- `draft`: LLM config draft payload.
- `binding`: Binding payload containing key/config_id/enabled/retry settings.
- `api_key`: API key for add/update operations (reads back masked).
- `api_base`: API base URL for add/update operations.
- `extra_headers`: Optional extra headers for this model.
- `enabled`: Initial enabled state for `add_model` only; use `enable_model` / `disable_model` afterwards.
- `max_tokens`: Optional max output token cap for this model.
- `temperature`: Optional default temperature for this model.
- `reasoning_effort`: Optional reasoning effort override.
- `context_window_tokens`: Declared context window. Required for `add_model`; the send and dispatch
  path reads this value, so declaring more than the provider really serves shows up as a clamped reply,
  not an error.
- `request_timeout_seconds`: Optional per-request timeout for this model.
- `image_multimodal_enabled`: Whether the model can take images. A chain is treated as
  image-incapable when any member is off, so this changes what every chain holding the model can do;
  the write echoes the affected chains.
- `quota_pool_key`: Optional quota pool shared with other bindings.
- `name`: Display name shown in the catalog and chains.
- `retry_on`: Custom retry keywords (list or comma-separated string) matched as lowercase substrings of the provider error text; a hit triggers automatic retry. Preset aliases `network` and `429` still expand; defaults to `network, 429`. An empty list disables keyword retry instead of restoring the default.
- `retry_count`: Retryable failures to allow on the same model before fallback.
- `description`: Optional human-readable description.
- `scopes`: Scopes for add_model, e.g. ceo, execution, inspection.
- `scope`: Target scope for `get_scope_chain` and `set_scope_chain`.
- `model_keys`: Ordered model keys for `set_scope_chain`. Flat form: it replaces the stored chain
  entirely, so a chain that contains load-balance groups must be written with `route_entries`.
- `route_entries`: Ordered chain as stored, for `set_scope_chain`; each entry is
  `{"type":"model","model_key":"..."}` or `{"type":"load_balance","group_key":"..."}`. Groups must
  already exist; this tool does not create or edit them. Load-balance entries are accepted on
  `execution` and `inspection` only.
- `max_iterations`: Per-role iteration budget for `set_scope_chain`.
- `max_concurrency`: Per-role concurrency for `set_scope_chain`. The `memory` role is fixed at 1.

## Supported actions
- `list_templates`, `get_template`
- `validate_draft`, `probe_draft`
- `create_binding`, `update_binding`
- `migrate_legacy`
- `list_models`, `get_model`, `add_model`, `update_model`
- `enable_model`, `disable_model`, `rename_model`, `delete_model`
- `get_scope_chain`, `list_load_balance_groups`, `set_scope_chain`

## Read before you write a chain
- `get_scope_chain` returns both views: `model_keys` (candidates, groups expanded) and
  `route_entries` (the stored order, group slots intact). Only `route_entries` tells you that a hop
  is a group.
- A `model_keys` write on a chain that stores group slots flattens them. Read first, then write the
  whole shape back through `route_entries`.
- Omitted fields are left alone, and so is any field you send as `null` — sending `null` does not clear
  anything. To clear a field that has a clearable meaning (`max_tokens`, `context_window_tokens`,
  `temperature`, `request_timeout_seconds`, `extra_headers`), send an empty string `""` instead.
  Never list a chain field you are not changing: `model_keys` and `route_entries` are mutually
  exclusive, and a `null` alongside the other one is still a conflict.
- `delete_model` quietly removes the model from every chain holding it and reports
  `removed_from_scopes`.

## When changes take effect
- Writes land in `.g3ku/config.json` immediately and refresh the loop runtime.
- A new turn/session resolves the updated chain at turn start.
- For the turn currently running, the runtime re-resolves the role chain at the
  next model call: after `set_scope_chain`, the next assistant step already uses
  the new chain, and capability checks tied to it (e.g. `content_open`
  multimodal gate) follow the new chain too. You can switch, then retry the
  gated operation in a follow-up step; do not batch the switch and the gated
  call as parallel tool calls in one step.
- An already-sent single provider request is never hot-swapped mid-request; a
  chain change only rebuilds the next request.
- If the switch is temporary, note the previous chain first and restore it with
  a follow-up `set_scope_chain` once done.

## Usage
Use `model_config` only when it is the most direct way to complete the task.
