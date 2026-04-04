# filesystem

统一的本地文件工具。所有 `path` 都必须是绝对路径；不会再把相对路径自动解析到工作区。

## 何时调用

- 需要确认目录结构时，用 `action=list`。
- 需要搜索单文件或整个目录树时，用 `action=search`。
- 需要确认文件概况时，用 `action=describe`。
- 需要读取文件局部内容时，用 `action=open`、`action=head`、`action=tail`。
- 需要直接落盘修改时，用 `action=write`、`action=edit`、`action=delete`。
- 需要先产出补丁提案而不是直接改文件时，用 `action=propose_patch`。

## 路径规则

- 临时内容必须写到工作区根目录的 `temp/` 下。
- 这里的“临时内容”包括下载结果、抓取缓存、一次性脚本、日志、解压产物、测试输出、临时生成文件和其他不应长期留在正式目录中的内容。
- 第三方工具本体、解压后的可执行文件、运行时、模型文件、附带依赖等，必须写到工作区根目录的 `externaltools/<tool_id>/` 下。
- `tools/` 目录只用于注册和适配，不用于存放第三方工具本体。
- 在 `tools/<tool_id>/` 下只应保留 `resource.yaml`、`main/`、`toolskills/` 及其注册代码、包装脚本、说明文档。
- 不要把临时内容写到工作区根目录其他位置、`tmp/`、`.g3ku/tmp/`、系统临时目录、桌面、下载目录或用户主目录。

## 使用原则

- 先用绝对路径确认目标目录或文件，再做搜索和局部打开。
- 如果拿到的是 `artifact:` / content 引用，不要把它塞进 `path`；改用 `content` 工具并通过 `ref` 读取。
- `action=search` 同时支持单文件和目录递归搜索；目录搜索会跳过二进制文件和无法解码的文件。
- 搜索目录时优先缩小到目标子目录，不要把整个仓库当成默认搜索范围。
- 如果调用方开启了 `restrict_to_workspace`，所有 `path` 都必须留在允许的工作区范围内。
- 如果 `action=search` 返回 `requires_refine=true` 或 `overflow=true`，先缩小路径、范围或关键词，再继续；不要重复相同的超限查询。
- 单文件阅读优先使用 `search` + `open` 的组合，而不是一次性请求完整文件。
- 要落盘前先判断文件属于哪一类：
  - 业务代码、配置、文档：写到它们原本所在的位置。
  - 临时内容：写到 `temp/`。
  - 第三方工具本体：写到 `externaltools/<tool_id>/`。
- 如果一个路径看起来像压缩包、安装包、二进制、日志、缓存或其他临时产物，但不在 `temp/` 或 `externaltools/` 下，应改路径后再执行。

## 常见例子

- 临时抓取结果：`<workspace>/temp/fetch/result.json`
- 临时脚本：`<workspace>/temp/scripts/check.ps1`
- 第三方工具安装目录：`<workspace>/externaltools/ffmpeg/`
- 工具注册文件：`<workspace>/tools/ffmpeg/resource.yaml`

## 先停一下再继续的情况

- 你准备把下载包、压缩包、日志、缓存或测试输出写到 `temp/` 之外。
- 你准备把第三方工具的真实文件写进 `tools/`。
- 用户明确要求使用不同目录。
