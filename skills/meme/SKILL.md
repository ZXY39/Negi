# 表情包图库管理

本文只在**增删图库、导入外部表情包项目、校准描述**时读。**发送表情包不需要读本文**：可用清单（每个包的主题 + 分类键 + 分类描述）已经钉在契约头里，直接按那里的键写标记即可。

## 发送语法（供核对，不用查表）

在回复**最后一行**单独写一个标记：

```
[[sticker:<包>/<分类>]]
```

- 只认独占一行；写在句子里不会被识别，也不会被吞掉。
- 每轮默认最多 1 张，超出的标记整行丢弃、不发图。
- 键不存在或短形态跨包重名 → 整行丢弃、不发图、记 warning。**任何情况下都不会把标记原文发给用户。**
- 你只看得到分类描述，看不到具体那张图。**不要替它编画了什么**：不要说"这张图里的人在……"这类具体断言。

## 目录布局

```
<workspace>/stickers/<pack>/<category>/<image>
```

- 目录就是清单，没有第二份注册表。加文件即生效，删文件即消失（扫描缓存 60 秒）。
- `pack` 与 `category` 的键：`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`。**中文名不能直接做键**，中文放描述里。
- **禁用名**（文件工具会拦）：`build`、`dist`、`node_modules`、`.venv`、`venv`、`site-packages`、`__pycache__`，以及 `tmp_` / `temp_` / `.tmp_` 开头的名字。
- 键名以 `_` 开头的**包或分类整体隐身**：不进契约头、不可解析。这是软删。
- 网页的「表情包」页改的是**同一棵树**：改主题/分类描述、停用启用、新建分类、上传、删除都在那里，与这里写的是同一份目录约定。你用文件工具改完不用通知界面（它下次刷新就读到）；界面改完，下一轮对话的 `[RUNTIME_NOTICE]` 会自动把清单变化告诉你。

## `<pack>/manifest.json`

只存不能从磁盘推导的事实：主题、分类描述、来源。

```json
{
  "schema_version": 1,
  "pack": "atri",
  "theme": "动漫少女 ATRI，日常卖萌与吐槽",
  "theme_source": "human",
  "source": {"kind": "astrbot_pack", "id": "atri-meme-pack", "version": "1.4.2",
             "url": "https://github.com/...", "commit": "abc1234",
             "imported_at": "2026-10-26T00:00:00+00:00", "license": "MIT"},
  "categories": {"happy": {"description": "开心大笑、庆祝"}}
}
```

- `theme`：一句 ≤60 字，写**这套图是什么、什么调性**。契约头里它决定模型先选哪个包。
- `theme_source`：`human` 表示人工定稿，导入脚本与运行时都不得覆盖。
- 张数、分类清单**不要**写进来，磁盘上就有。

## 加一张图

1. 来源图通常已经在 `<workspace>/.g3ku/external-uploads/<session>/`（用户从渠道发来的图）。
2. 不确定放哪个分类时先 `content_open` 那张图看一眼，再决定。
3. `filesystem_copy` 到 `stickers/<pack>/<要放的分类>/`，文件名保留但确保后缀是 `.png/.jpg/.jpeg/.gif/.webp`。
4. 新增分类时，在 `manifest.json` 的 `categories` 里补一行描述（20 字内，说清"什么时候用"）。

## 删一张图 / 停用一个包

- 单张：`filesystem_delete`。不可逆，删前先 `filesystem_stat` 确认路径。
- 暂时不想被选中：把分类目录或整个包目录改名为 `_` 开头（例如 `_awkward`、`_atri`），要恢复再改回来。
- 一个包删空之后目录会自己从契约头消失；`manifest.json` 不用动。

## 导入外部表情包项目

脚本：`skills/meme/scripts/import_pack.py`（在 workspace 根目录下运行）。

```
python skills/meme/scripts/import_pack.py <源> --dry-run
python skills/meme/scripts/import_pack.py <源> --theme "一句话主题" [--pack NAME] [--force]
```

`<源>` 可以是本地目录、`.zip`、或 git 仓库 URL。脚本自己处理：解压/clone 到 `temp/`、形状探测、路径安全、后缀白名单、20MiB 上限、sha256 去重、幂等（同源重跑 0 新增）、写 manifest、输出**短报告**。

支持三种形状：

| 形状 | 判据 | 落点 |
|---|---|---|
| AstrBot 包 | 根有 `manifest.json` + `memes/` | 按原分类写入，`previews/` 丢弃，描述原样搬 |
| 一层子目录 | 子目录里全是图 | 子目录名 → 分类键（自动转 ASCII） |
| 平铺图堆 | 根目录全是图 | 默认全部进 `_review/`（隐身待分拣） |

四条实测过的边界（真实社区包踩出来的，别当例外处理）：

- **AstrBot 的 `_` 前缀原样保留**。作者用它标"特殊场景"，Negi 用同一个前缀标"隐身"，语义正好对齐，所以这类分类导入后**不进契约头**。报告里的 `hidden:` 一行会写清几类几张；确实要发布，去「表情包」页点启用（那是改名去 `_`，不是重新导入）。
- **去重只在分类内做**。同一张图被作者放进 `happy` 和 `laugh` 两个分类时，两边都会保留；只有同一分类里重复的字节才会被跳过。重跑仍然幂等。
- **源 manifest 的 BOM、缺失的 `schema_version`、分类键里的零宽字符都已兼容**。缺 `schema_version` 按 1 处理；写了非 1 的版本号才拒绝。分类描述按归一化后的键匹配，所以 `_kg` 与 `kg` 共用一条描述。
- **URL 导入是 `git clone --depth 1`，大包很慢**（实测 78MB 的包要几分钟）。被中断会留下 `temp/sticker-import-*`（脚本会在下次运行清掉超过 6 小时的），不要连开三次重试——那是三份 78MB。

**导入之后必须做两件事**：

1. **定主题**。报告里给了 `theme_samples`（分层抽的 8 张路径）。用 `content_open` 看这几张，再用一句 ≤60 字概括这套图的主题，重跑脚本带 `--theme "..."`（人工定稿，此后不会被覆盖）。没有主题的包在契约头里显示"（未设置主题）"，模型没法按风格选它。
2. **分拣 `_review/`**（如果有）。平铺图堆不会被猜分类。报告里给出待分拣数量；逐批看图后用 `filesystem_move` 或重新导入（带 `--classify-map`）归到已有分类。**不要发明新分类键**：优先复用图库里已有的键，需要新键时同时补描述。

AstrBot 包协议与逐字段映射见 `references/pack-format.md`；描述与 caption 的写法规则见 `references/caption-policy.md`。

## 精确取图（可选，需先打开语义检索）

分类键只能到"大致情景"。包里有逐图 caption/tags 之后，可以打开 `stickers.semanticSearch`，模型就多一颗 `search_stickers` 工具：按想表达的态度检索候选，再用 `[[sticker:<包>/@<id>]]` 精确发送那一张。

1. 看还缺哪些描述：`python skills/meme/scripts/caption_pack.py --list <pack>`
2. 看图写 caption/tags（写法见 `references/caption-policy.md`），存成 JSON：
   `{"happy/h1.png": {"caption": "…", "tags": ["…"], "visible_text": ""}}`
3. 落盘：`python skills/meme/scripts/caption_pack.py <pack> --map captions.json`
4. 在配置里打开 `stickers.semanticSearch`（默认关：多一颗工具会让 `tools[]` 前缀作废一次，且没有 caption 的库检索不出东西）。

`entry_id` 与 `sha256` 由脚本按磁盘字节现算：换图或改分类之后旧 id 自动失效，模型拿着旧 id 发送只会"不发图"，不会发错图。

## 边界

- 契约头里的清单是**钉住时点的快照**：两次重印之间增删，你会在下一轮收到一条 `[RUNTIME_NOTICE]` 的 `sticker_pack:<包>` 分节，以那条为准；头部要到下一次上下文压缩才刷新。
- 一个包超过约 20 个分类就该拆包或改用语义检索，不要继续加类。
- 图库在 workspace 内：渠道会话只能发 workspace 里的文件，把这套库搬出去会导致渠道发不出图。
- `scripts/*.py` 是仓库代码，不是草稿：**不要手改**，更不要用 shell 重定向/heredoc 整段重写（中文会写成 GBK，文件当场变成二进制、脚本再也跑不起来）。脚本行为不符合预期时，把命令、报错和源包特征报告给用户，由维护者改。
