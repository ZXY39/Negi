# filesystem

统一的工作区文件工具。默认按“先定位，再打开局部”的方式访问文件。

## 何时调用

- 当任务需要访问或修改工作区文件时，优先使用 `filesystem`。
- 只读场景先用 `action=describe`、`action=search`、`action=open`、`action=head`、`action=tail`、`action=list`。
- 直接落盘修改使用 `action=write`、`action=edit`、`action=delete`。
- 需要先生成可审阅变更而不是直接改文件时，使用 `action=propose_patch`。

## 使用原则

- 不要请求全文读取。
- 不要尝试读取、修改或删除工作区外路径。
- 默认先 `search` 缩小范围，再 `open` 查看命中附近的 20-80 行。
- 只有明确知道要看文件头部或尾部时，才使用 `head` / `tail`。
