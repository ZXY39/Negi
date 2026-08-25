# model_config

Manage .g3ku/config.json model catalog, provider templates/drafts/bindings, and role model chains.

## Parameters
- `action`: Model config action to perform.
- `key`: Managed model key.
- `provider_model`: Provider:model identifier or managed model reference.
- `provider_id`: Provider template id.
- `draft`: LLM config draft payload.
- `binding`: Binding payload containing key/config_id/enabled/retry settings.
- `api_key`: API key for add/update operations.
- `api_base`: API base URL for add/update operations.
- `extra_headers`: Optional extra headers for this model.
- `enabled`: Optional enabled flag for add/update actions.
- `max_tokens`: Optional max output token cap for this model.
- `temperature`: Optional default temperature for this model.
- `reasoning_effort`: Optional reasoning effort override.
- `retry_on`: Custom retry keywords (list or comma-separated string) matched as lowercase substrings of the provider error text; a hit triggers automatic retry. Preset aliases `network` and `429` still expand; defaults to `network, 429`.
- `retry_count`: Retryable failures to allow on the same model before fallback.
- `description`: Optional human-readable description.
- `scopes`: Scopes for add_model, e.g. ceo, execution, inspection.
- `scope`: Target scope for set_scope_chain.
- `model_keys`: Ordered model keys for set_scope_chain.

## Supported actions
- `list_templates`, `get_template`
- `validate_draft`, `probe_draft`
- `create_binding`, `update_binding`
- `migrate_legacy`
- `list_models`, `get_model`, `add_model`, `update_model`
- `enable_model`, `disable_model`
- `set_scope_chain`

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
