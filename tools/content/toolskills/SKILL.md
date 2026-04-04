# content

按需查看大内容体的统一工具。它只处理单个内容体或单个文件路径，不负责目录递归搜索。

## 何时调用

- 工具输出、日志、节点结果或补丁内容过长时，优先使用 `content`。
- 已经拿到 `artifact:` 之类的 `ref` 时，优先走 `ref` 模式。
- 需要直接看本地文件时，传绝对 `path`。

## 使用原则

- 不要请求全文，先 `action=describe` 或 `action=search`，再 `action=open` 做局部阅读。
- 来自其他工具输出的 `artifact:` / content 引用，一律走 `ref` 模式；不要把这类引用误当成本地 `path` 传给 `filesystem`。
- `path` 模式只接受绝对路径；相对路径会直接报错。
- 如果调用方开启了 `restrict_to_workspace`，`path` 必须留在允许的工作区范围内。
- 目录级搜索请使用 `filesystem search`，不要把 `content` 当成目录工具。
- 搜索命中后，只打开和命中相关的局部窗口。
- 如果 `action=search` 返回 `requires_refine=true` 或 `overflow=true`，先收窄查询，再重试；不要重复同一个超限搜索。
