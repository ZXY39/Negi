# 表情包图库管理

本文只在**增删图库、导入外部表情包、校准描述**时读。**发送表情包不需要读本文**：可用清单（每个包的主题 + 分类键 + 分类描述）已经钉在契约头里。

## 目录与注册表

```
<workspace>/stickers/<pack>/
  manifest.json    # {"theme": 一句话, "categories": {"<键>": {"description": …}}}
  registry.json    # 已注册文件（模型可见）与它们的 sha256
  <category>/<NNN>.<ext>
```

- **目录只说明文件存在，注册表才决定可见**。直接拷进文件夹的图**不会生效**，必须注册（工具 `register_stickers` 或「表情包」页的注册按钮）。
- 注册 = 取下一个序号重命名（`001.gif`、`002.png`…），append-only，删除留空洞，**永不重排**。
- 单个分类最多 **36 张**，第 37 张在注册处被拒。
- 键：`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`；`_` 开头的包/分类整体隐身（不进清单、不可解析）。中文名不能做键，中文放描述。
- 禁用名：`build`、`dist`、`node_modules`、`.venv`、`venv`、`site-packages`、`__pycache__`，以及 `tmp_` / `temp_` / `.tmp_` 开头的名字。

## 加一张图（用户在对话里发来的图）

1. 源图通常在 `<workspace>/.g3ku/external-uploads/<session>/`；不确定放哪类时先 `content_open` 看一眼。
2. `filesystem_copy` 到 `stickers/<pack>/<分类>/`（文件名随意）。
3. **调用 `register_stickers(pack="<pack>", category="<分类>")`** —— 不注册就不会生效。
4. 新分类要在 `manifest.json` 的 `categories` 里补描述（≤120 字），界面也能改。

## 导入外部表情包（两阶段）

只接受**分类形状**：`<源>/<分类目录>/<图片>`。AstrBot 包直接指到仓库根即可，脚本会自动下到 `memes/`；平铺图堆会被拒绝，先归类。

```
# 1) 探测：列出源分类、张数、是否会超 36
python skills/meme/scripts/import_pack.py <源> --detect

# 2) 落盘：每个源分类都必须给描述，缺一个就整批拒绝
python skills/meme/scripts/import_pack.py <源> --commit --map map.json --theme "一句话主题"
```

`map.json`（键是源目录名，可改名；描述 ≤120 字，超了截断并在报告里说明）：

```json
{"happy": {"description": "开心大笑、庆祝"},
 "stock": {"category": "stocks", "description": "讨论股票基金，低频"}}
```

`<源>` 可以是本地目录、`.zip` 或 git URL（`git clone --depth 1`，大包很慢；被中断会留 `temp/sticker-import-*`，脚本下次运行会清超过 6 小时的，**不要连开三次重试**）。

脚本自己处理：解压/clone 到 `temp/`、路径安全、后缀白名单、20MiB 上限、同分类 sha256 去重、幂等重跑（0 新增）、按序号注册、写 manifest、输出短报告。

**导入之后**：报告里会给出每类新增数与主题；描述可在「表情包」页核对。模型侧下一轮会收到该包的清单变化通知，不用另行通知。

## 删一张图 / 停用一个包

- 单张：`filesystem_delete`。不可逆，删前先 `filesystem_stat`。删掉的序号留空洞，不重排。
- 暂时不想被选中：把分类目录或整个包目录改名为 `_` 开头（如 `_awkward`、`_atri`），恢复再改回来。启停不改顺序、不加状态徽标。
- 一个包删空后目录会从契约头消失；`manifest.json` 不用动。

## 边界

- 契约头里的清单是**钉住时点的快照**：两次重印之间增删，会在下一轮收到 `[RUNTIME_NOTICE]` 的 `sticker_pack:<包>` 分节，以那条为准。
- 图库在 workspace 内：渠道会话只能发 workspace 里的文件。
- 描述是模型选分类的唯一依据（拼图只管分类内选哪张），写"什么时候用"，别写"图里有什么"。
- `scripts/*.py` 是仓库代码，不是草稿：**不要手改**，更不要用 shell 重定向/heredoc 整段重写（中文会写成 GBK，文件当场变二进制）。脚本行为不符合预期时，把命令、报错与源包特征报告给用户。
