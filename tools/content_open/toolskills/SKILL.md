# content_open

Use this to read one local excerpt from a content target.

Provide:
- `ref`: an `artifact:` content ref when you already have one
- `path`: an absolute file path when you need path mode
- `view`: optional `canonical` or `raw`; prefer `canonical`
- `start_line` and `end_line` as the line range you want to open
- line values are 1-based integers

If both `ref` and `path` are provided, the tool attempts both targets and returns separate `ref` and `path` results.

Open only the lines you need. If you do not know where to look yet, use `content_describe` or `content_search` first.

If the target is a historical image path or image ref and you need direct visual inspection, call `content_open`.

On multimodal routes, the opened image is attached to the next model request only.
On non-multimodal routes, image open fails with `非多模态模型无法打开图片`.
