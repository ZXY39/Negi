# content

按需查看大内容的统一工具。

## 何时调用

- 当工具结果、日志、节点输出、补丁或文件内容过长时，先使用 `content`。
- 默认先 `action=describe` 或 `action=search`，再 `action=open` 读取局部。
- 只有在明确知道要查看文件开头或结尾时，才使用 `action=head` / `action=tail`。

## 使用原则

- 不要请求全文。
- 先缩小范围，再打开局部。
- 搜索命中后，只打开与命中相关的 20-80 行窗口。
