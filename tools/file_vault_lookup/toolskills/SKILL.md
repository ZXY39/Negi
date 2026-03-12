# file_vault_lookup

Find uploaded files by placeholder, filename, or prior-turn context and return ranked candidates.
MUST CALL: when user references previously uploaded files or images (e.g., '上次/之前/那个文件/那张图/那三张图/之前上传的图片/商品图/风格图') and current turn does not include the target file.
AVOID CALL: when target file is already uploaded in current turn and can be handled directly.

## Parameters
- `query`: Lookup query with filename or context keywords.
- `session`: Optional session key override.
- `limit`: Max candidates.

## Usage
Use `file_vault_lookup` only when it is the most direct way to complete the task.
