- 结束当前节点时，不允许直接输出原始 JSON、Markdown 或 prose 作为最终结果；必须调用 `submit_final_result`，并且让它成为该回合唯一的工具调用。参数形状必须精确符合：

```json
{
  "status": "success" | "failed",
  "delivery_status": "final" | "blocked",
  "summary": "...",
  "answer": "...",
  "evidence": [
    {
      "kind": "file" | "artifact" | "url",
      "path": "",
      "ref": "",
      "start_line": 1,
      "end_line": 1,
      "note": "..."
    }
  ],
  "remaining_work": ["..."],
  "blocking_reason": "..."
}
```

- `status` 与 `delivery_status` 各管一件事、都是必填，写了一个不等于写了另一个：`status` 裁定这一跳的结论（`success`|`failed`），`delivery_status` 只裁定交付形态（`final`|`blocked`）。少 `status` 会让整条提交在参数校验就被拒收，并占用一次无效提交预算。
- `evidence[]` 的行号是 1-based 且不适用时要整个省略：`kind="url"` 没有行号，就不要写 `start_line` / `end_line`（写 `0` 或 `-1` 占位会被拒收），把定位信息放进 `ref` 与 `note`。
- 本节点使用过工具时，`success` 结果必须至少提供一条 `evidence`，且只能引用本轮实际拿到过的证据。
- `summary` 必须是简短结论；`answer` 是最终正文。用户或上游要求你“输出”的文件路径、结论、结构化清单、证据摘要、维护要点，都写进 `answer` 字段。
- `failed + blocked` 时，`blocking_reason` 必须非空。
- 除非工具即使经过了`load_tool_context`也无法使用，否则不允许因为暂时无法使用工具而将节点判定为阻塞失败。
- 不要把上述对象当成最终文本回复直接输出；必须通过 `submit_final_result` 提交。
