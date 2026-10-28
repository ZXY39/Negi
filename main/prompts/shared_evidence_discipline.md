## 交付物证据

- 除非工具即使经过了 `load_tool_context` 也无法使用，否则不允许因为暂时无法使用工具而把节点判成阻塞失败。
- 交付物是文件时，先用只读工具 `filesystem_stat` 实测：存在性、`size_bytes`（磁盘真实字节数）、`mtime`，目录再取 `file_count` / `total_bytes` 与文件清单；它只出现在 `candidate_tools` 时先 `load_tool_context`，下一轮再调用。
- 要回答“这批产物有几个、多大、哪些是这一轮写的、真实文件名是什么”，只能靠 `filesystem_stat` 或同类实测，不得靠占位串统计或清单外推。
- 二进制交付物（PDF、图片、xlsx、压缩包等）只认三类证据：`content_describe` / `content_open` 结果里的 `size_bytes`、`binary` / `content_display_replaced` 标记与 `mime_type`；`content_search` 的字节级命中（`byte_level: true` 时的 `byte_offset` 与上下文片段）。占位串（`[二进制文件：…]` / `[图片文件：…]`）的字符数以及 `line_count` / `char_count` 只描述占位串本身，不能用来推断文件的体积、类型或有效性——合法文件与空壳在文本通道里可能输出逐字相同。
- `completed_stage_summary` 必须是对刚结束那条阶段的简要概括：写清本阶段已确认的事实、剩下的目标、以及从犯过的错误中总结出的经验教训。
