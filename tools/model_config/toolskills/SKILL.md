# model_config

Manage .g3ku/config.json model catalog and role model chains. Supports listing models, adding/updating models, enabling/disabling models, and setting ordered fallback chains for ceo/execution/inspection scopes.

## Parameters
- `action`: Model config action to perform.
- `key`: Managed model key.
- `provider_model`: Provider:model identifier or managed model reference.
- `api_key`: API key for add/update operations.
- `api_base`: API base URL for add/update operations.
- `extra_headers`: Optional extra headers for this model.
- `enabled`: Optional enabled flag for add/update actions.
- `max_tokens`: Optional max output token cap for this model.
- `temperature`: Optional default temperature for this model.
- `reasoning_effort`: Optional reasoning effort override.
- `retry_on`: Retry triggers such as network, 429, 5xx.
- `description`: Optional human-readable description.
- `scopes`: Scopes for add_model, e.g. ceo, execution, inspection.
- `scope`: Target scope for set_scope_chain.
- `model_keys`: Ordered model keys for set_scope_chain.

## Usage
Use `model_config` only when it is the most direct way to complete the task.
