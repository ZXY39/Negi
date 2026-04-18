# 核心记忆 (Memory)

## 当前协议

- `memory/MEMORY.md`
  当前唯一的长期记忆正文源。每个 fresh turn 会读取它的冻结快照，并直接注入上下文。
- `memory/notes/`
  存放详细笔记正文。只有当 `MEMORY.md` 里的精炼句引用了 `ref:note_xxxx`，并且你显式调用 `memory_note(ref="note_xxxx")` 时，才会按需读取。
- `memory/queue.jsonl`
  长期记忆写入/删除请求的异步队列，不是用户可直接编辑的正文。
- `memory/ops.jsonl`
  已成功处理批次的操作日志，不是长期记忆正文。

## 如何查看当前长期记忆

- 优先直接阅读 `memory/MEMORY.md`。
- 如果某条正文引用了 `ref:note_xxxx`，再调用 `memory_note(ref="note_xxxx")` 读取对应详细笔记。
- 不要再依赖旧的 history 日志文件；它不属于当前长期记忆协议。

## 如何写入或删除长期记忆

- 需要让系统记住稳定偏好、长期规则、身份信息或项目事实时，调用 `memory_write(content=...)`。
- 需要删除当前快照里已经可见的旧记忆时，调用 `memory_delete(target_text=...)`。
- `memory_write` 和 `memory_delete` 都只是入队请求，不会在当前轮同步改写 `MEMORY.md`。

## 使用原则

- 只把稳定、可复用、跨轮次仍然有价值的信息写入长期记忆。
- 不要把暂停状态、处理中标记、临时调试信息、当前任务局部状态写进长期记忆。
- 对复杂背景，优先保留简短正文并引用 `ref:note_xxxx`，不要把长段细节直接塞进 `MEMORY.md`。
