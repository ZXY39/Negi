# 表情包落盘格式与 AstrBot 映射

## 我们自己的落点

```
<workspace>/stickers/<pack>/
  manifest.json          # {"theme": 一句话, "categories": {"<键>": {"description": ≤120 字}}}
  registry.json          # 可见性真相：{"schema_version":1,"categories":{"<分类>":{"<NNN.ext>":{"sha256","size","registered_at"}}}}
  <category>/<NNN>.<ext> # 序号即文件名
  _<anything>/           # 下划线开头 = 隐身（软删），不渲染、不进清单
<workspace>/.g3ku/sticker-sheets/<sheet_id>.(png|jpg)   # 拼图缓存，按内容哈希命名
```

不变量：

- **目录说明存在，注册表说明可见**。渲染、索引解析、类别随机都只认已注册文件；直接放进去的文件出现在「未注册」列表里，必须注册。
- **编号 append-only**：铸号 = 分类内最大号 + 1，三位零填充；删除留空洞，永不重排。序号永远指向同一张图，除非它被删——被删是"不发"，不是"发错"。
- 单分类上限 **36**：注册处程序性拒绝第 37 张；>36 的解是拆分类，不是截断。
- 注册时记 `sha256` 与 `size`。索引发送前会重算 sha；不符按未注册处理（fail-closed）。
- 图片只认 `.png/.jpg/.jpeg/.gif/.webp`；单文件上限 20MiB；符号链接解析后必须落在包目录内。

## AstrBot 包怎么吃进来

AstrBot 协议形状：

```
<pack>/
  manifest.json          # schema_version / id / version / categories{}.description
  memes/<category>/<image>
  previews/              # 包预览图，导入时忽略
  semantic_metadata.json # 逐图 caption（旧语义检索用），本仓库已废弃，忽略
```

映射：

| AstrBot | 我们 | 处理 |
|---|---|---|
| `memes/<cat>/<img>` | `<cat>/<NNN>.<ext>` | 脚本自动把 `memes/` 当源根；按分类注册铸号 |
| `categories[].description` | `categories.<键>.description` | **不自动搬**：导入必须给 `--map` 描述，缺一个整批拒绝 |
| `manifest.json` 的 id/version | 包目录名 / 报告行 | 只作提示，不落盘 |
| `previews/` | 丢弃 | 混进来会污染随机取图 |
| `semantic_metadata.json` | 忽略 | 无消费者；精确选图改走拼图 |
| `_` 前缀分类 | `_` 前缀保留 | AstrBot 用它标"特殊场景"，Negi 用它标隐身，语义对齐 |

## 描述怎么写（≤120 字）

拼图接管分类内选择之后，描述是模型**选分类的唯一依据**，要编码四个面：

1. 触发情境（"当对话包含抱怨、批评或强烈反对时"）
2. 态度指向（自嘲 vs 嘲弄用户、安慰 vs 附和）
3. 话题域（原神 / 股票涨跌 / 某个梗）
4. 频率提示（低频）

渲染侧**不截断**（截断会砍掉兄弟分类的区分词），长度上限只在写入侧强制：界面/API 超限报错，导入超限截断并在报告里列出。

## 生成与投递

- `show_sticker_category(pack, category)` 渲染一分类一拼图：tile 200px、内容 bbox 裁剪 + fit 放大、等比 letterbox、深色圆角底白字徽标、≤36 格 / 整图 ≤1256²、JPEG q85（PNG 在 300KB 内优先）。
- 拼图走 overlay 只注入当跳请求尾巴，不进耐久历史、不进稳定前缀。
- `[[sticker:<包>/<分类>/<N>]]` 解析：注册表 → 文件存在 → sha 复验；任一不符则整行吞掉 + `[表情包未发出：…]` + `sticker_send_failed` 审计。
