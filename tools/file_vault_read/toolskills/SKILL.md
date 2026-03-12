# file_vault_read

Read uploaded file content by placeholder.
For images and supported files, this tool can return native multimodal content blocks back to the model.
MUST CALL: when placeholder is known and answer requires actual file content; this includes follow-up intents like continue reading, use these placeholders, read the file itself, or ??????.
DO NOT promise to read later when the needed placeholder is already known in the conversation; call file_vault_read in this turn.
AVOID CALL: when only existence/list confirmation is needed without reading content.

## Parameters
- `placeholder`: Canonical file placeholder.
- `mode`: Read mode.
- `max_chars`: Max returned chars in text mode.

## Usage
Use `file_vault_read` only when it is the most direct way to complete the task.
