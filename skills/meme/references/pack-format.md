# 表情包包格式与 AstrBot 兼容映射

## 我们自己的落点

```
<workspace>/stickers/<pack>/
  manifest.json          # 主题 / 分类描述 / 来源（只存不可推导的事实）
  <category>/<image>     # 键 = 目录名，图片直接子项，不递归
  semantic/              # 可选：逐图 caption/tags（P2 语义检索用）
  _<anything>/           # 下划线开头 = 隐身（软删）
```

不变量：

- 目录是唯一真相。manifest 与 `semantic/` 都是派生物，删图不需要同步删它们。
- 图片只认 `.png/.jpg/.jpeg/.gif/.webp`；其余忽略并计入报告。
- 单文件上限 20MiB（渠道出站上限）。
- 符号链接不算可用图：解析后必须落在包目录内。

## AstrBot meme pack 协议（我们要吃进来的形状）

```
<pack>/
  manifest.json          # schema_version / id / version / categories[].description
  memes/<category>/<image>
  previews/              # 可选，包预览图，导入时丢弃
  semantic_metadata.json # 可选，逐图 caption/tags + entry_id
```

- 分类键字符集 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，与我们的键完全一致，所以**不需要转换**。
- 分类描述 1–2000 字，人工写的，原样搬进我们的 manifest。
- 路径规则（协议本身要求，我们照抄判据）：不得绝对、不得含 `..`、不得含反斜杠、不得经符号链接逃逸包根。

## 逐字段映射

| AstrBot | 我们 | 处理 |
|---|---|---|
| `schema_version` | — | 只认 `1`；不认识的版本拒绝导入并报告 |
| `id` | 包目录名 | 转 ASCII 键；与已有包重名 → 默认拒绝，需 `--force` 或 `--pack` |
| `version` | `source.version` | 原样记 |
| `categories[].description` | `categories.<键>.description` | 原样搬（头部渲染截 20 字，全文留给语义检索） |
| `memes/<cat>/<img>` | `<cat>/<img>` | 原样；`/` 是我们的包/分类分隔符，AstrBot 键里不可能出现，零冲突 |
| `previews/` | 丢弃 | 包预览图混进去会污染随机取图 |
| `semantic_metadata.json` | `semantic/metadata.source.json` | **原样复制**，不重算 |

## `entry_id` 为什么暂时不重算

AstrBot 的语义条目身份是 `entry_id = sha256(图片字节 + 分类 + 相对路径)`。我们把相对路径从 `memes/<cat>/x.png` 改成了 `<cat>/x.png`，所以源文件的 `entry_id` 全部失效。

在没有 P2 语义检索的当前版本里，这些 ID 没有任何消费者，因此导入脚本只做原样复制并记录"stale"。**开启 P2 之前必须重算**：内容或分类变了，身份必须跟着变，否则候选表校验会整批失败。

## 导入后的自查

1. `manifest.json` 的 `theme` 是否已人工定稿（`theme_source: "human"`）。
2. 每个分类是否有描述；没有描述的分类在契约头里只有键名，模型很难选准。
3. `_review/` 是否已清空。
4. 单包分类数是否仍在 20 上下（超过就该拆包）。
