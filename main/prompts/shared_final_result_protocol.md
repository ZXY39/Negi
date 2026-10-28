- 结束当前节点时，不允许直接输出原始 JSON、Markdown 或 prose 作为最终结果；必须调用 `submit_final_result`，并且让它成为该回合唯一的工具调用。

执行节点（execution）的参数形状：

```json
{
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
  "remaining_work": ["..."]
}
```

- 没有 `status` / `delivery_status` 字段：提交即表示「这一轮工作已经做完」。如果交付物还没有真正产出并且可验证，继续用工具工作，不要提交。
- `summary` 必须是简短结论；`answer` 是最终正文。用户或上游要求你“输出”的文件路径、结论、结构化清单、证据摘要、维护要点，都写进 `answer` 字段。
- `remaining_work` 只作为信息报告，不构成“未完成也能结束”的通道。
- 本节点使用过工具时，提交必须至少提供一条 `evidence`，且只能引用本轮实际拿到过的证据。
- `evidence[]` 的行号是 1-based 且不适用时要整个省略：`kind="url"` 没有行号，就不要写 `start_line` / `end_line`（写 `0` 或 `-1` 占位会被拒收），把定位信息放进 `ref` 与 `note`。
- 工具返回 `{outcome, submission_id, acceptance_node_id}` 回执；`outcome=pending_acceptance` 表示已进入验收，验收结论会在后续回合以消息送达。

验收节点（acceptance）的参数形状：

```json
{
  "verdict": "pass" | "reject" | "reject_no_progress",
  "feedback": "..."
}
```

- 三档裁决的判据见验收车道「验收判定规则」（`pass` / `reject` / `reject_no_progress`）。
