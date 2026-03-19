# filesystem

统一的本地文件工具。所有 `path` 都必须是绝对路径；不会再把相对路径自动解析到工作区。

## 何时调用

- 需要确认目录结构时，用 `action=list`。
- 需要搜索单文件或整个目录树时，用 `action=search`。
- 需要读取局部内容时，用 `action=describe`、`action=open`、`action=head`、`action=tail`。
- 需要直接落盘修改时，用 `action=write`、`action=edit`、`action=delete`。
- 需要先产出补丁提案而不是直接改文件时，用 `action=propose_patch`。

## 使用原则

- 先用绝对路径确认目标目录或文件，再做搜索和局部打开。
- `action=search` 现在既支持单文件，也支持目录递归搜索；目录搜索会跳过二进制和无法解码的文件。
- 搜索目录时优先缩小到目标目录，不要把整个仓库当作默认搜索范围。
- 单文件阅读优先 `search` + `open` 的组合，而不是一次性请求完整文件。
