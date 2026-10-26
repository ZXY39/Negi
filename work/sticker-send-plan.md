# 表情包发送（agent 选图 → 出站）改造计划

日期：2026-10-26
状态：**待确认，未动代码**
取证基线：`main` @ `c0deaa00`；标 † 的是子代理读数，尚未逐行复核，动对应阶段前先复核。

## 一句话结论

Negi 不需要新造"标记 → 图片 → 消息链"的发送实现：**正文里的本地图片 markdown 已经会被出站管线抽成附件**。表情包能力 = 在两条出站管线各加一次**纯文本改写**，把 `[[sticker:分类]]` 换成 `![](绝对路径)`，其余全部复用现有通路。

## 背景与问题

现状（已核）：

1. 渠道车道：`message_end` → `sanitize_channel_outbound_text` → `_extract_media_attachments` → `reply.final`（`g3ku/runtime/external_events.py:232-286`）。抽取器把 markdown 图片/链接里的本地文件转成 `{name,mime_type,size,url}` 附件，正文里替换成 alt（`g3ku/runtime/api/ceo_media.py:246-301`）。
2. Web 车道：`ceo.reply.final` 前调 `rewrite_assistant_media_content`（`g3ku/runtime/api/websocket_ceo.py:1855-1866`），本地图换成会话内缩略图 + 签名 URL（`ceo_media.py:178-212`）。
3. 官方 QQ 真把附件发成富媒体：group/c2c 先 `file_data` base64 上传，再 `msg_type=7`（`g3ku/qq_official/bridge.py:823-919`）。

所以缺口只有三个，外加一个**与表情包无关、但会被表情包放大的安全缺陷**。

缺口：

- 模型不知道"有哪些图"，只能瞎猜路径；猜错静默不发。
- 没有"分类内随机取一张"这一层，模型被迫精确点名文件，对表情包场景过严。
- 两条车道遇到解析不了的 markdown 都**原样保留**（`ceo_media.py:191-192`、`286-287`），失败形态是用户看到一坨 `![](C:\...\x.png)`。

安全缺陷：

- `_resolve_local_source` 接受**任意绝对路径**，只排除 `#`、`data:`、内部 API URL 与 URI scheme（`ceo_media.py:84-98`）。
- `extract_local_media_attachments` 不看 mime、不看根目录：任何存在的本地文件都会变成渠道附件。
- 即：工具结果或网页里出现 `![](C:\...\token.txt)` 并被模型原样带出，就会作为文件消息发到群里。**这条今天就是活的**，与表情包无关。

## 目标行为（验收口径）

1. 模型在正文最末写 `[[sticker:<包>/<分类>]]`（每行一个，默认最多 1 行），出站后变成真实图片；正文不残留任何标记。
2. 未知/拼错的包或分类、跨包重名的短形态 → **整行剥掉、不发图、记 warning**；绝不把标记或原始路径漏给用户。
3. 纯表情回复（正文只有标记）也能发出：渠道侧 text 为空、attachments 非空；Web 侧正文本身就是图片 markdown，不会空。
4. 流式期间（Web `ceo.reply.delta`）不显示标记，也不显示半个标记。
5. 发送表情包的那一轮**不增加任何工具跳数**：清单常驻头部钉住块，模型直接写标记。
6. 两次重印边界之间，头部的表情包清单**逐字节不变**——增删图不作废前缀缓存；期间的变化只体现在 `[RUNTIME_NOTICE]` 增量块里。
7. 增量块在压缩后自动消失（头部已吸收全量），不需要清理任务。
8. 只有 frontdoor 聊天车道生效；节点执行车道不注入。
9. 模型能区分包：头部每个包带一句主题描述，按包分组渲染分类。
10. 导入一个 AstrBot 包后，无需人工干预即可被模型选中并发送；`previews/` 不出现在清单里。

## P0 先修（独立可上，不依赖表情包）

### P0-1 附件描述补 `kind`

- 位置：`g3ku/runtime/api/ceo_media.py:273-279`，`mime_type` 已算好，同处补 `"kind": "image" if mime.startswith("image/") else "file"`。
- 原因：OneBot 桥按 `attachments[].kind == "image"` 决定发 `[CQ:image]`（`bridges/qq-onebot/qq_onebot_bridge/dispatcher.py:260-266`），而生产端从不写 `kind` → 今天所有图片走 `upload_private_file` 当**文件**发，图片分支不可达。官方 QQ 不看 `kind`（按 mime，`bridge.py:843`），不受影响。
- 验收：OneBot 私聊/群聊收到 PNG 时命中 `[CQ:image]` 分支。

### P0-2 出站路径白名单（按车道分严，用户拍板：渠道严、网页宽）

调用图已核，两个集合天然不交叉，所以白名单可以按车道给，不需要一个全局开关：

| 函数 | 车道 | 调用点 |
|---|---|---|
| `rewrite_assistant_media_content` | Web | `websocket_ceo.py:907`、`:1101`、`:1858` |
| `extract_local_media_attachments` | 渠道（回合） | `external_events.py:185` |
| `extract_local_media_attachments` | 渠道（主动推送 drain） | `g3ku/shells/web.py:376` |
| `rewrite_media_links_signed` | 渠道 | `external_events.py:171`（唯一调用点） |

改法：`_resolve_local_source(raw_src, allowed_roots=None)`（`ceo_media.py:84-98`），`None` = 不限（Web 保持现状）；两个渠道函数传白名单根。白名单只需两根：`workspace_path()` 与本会话 upload dir —— 表情包库定在 `<workspace>/stickers/`（见 P1-1），已被 workspace 覆盖，不额外开根。drain 那一处 `entry.session_key` 在提取之前就已解析（`shells/web.py:_route_external_outbound`），拿得到会话。

**必须一起堵的洞（只堵附件转换不够）**：

- `rewrite_media_links_signed` 会把附件转换后**剩下的** markdown 引用改写成签名查看链接写进正文；而 `get_ceo_media_original`（`ceo_media.py:304-325`）只验 HMAC、不验会话与根目录，任何有效 token 都能取到那个路径。
- 桥侧还会把投递失败的附件**降级成签名 URL 文本行**：`bridge.py:937`、`dispatcher.py:295`。
- 所以渠道用户拿到的是一个可下载任意本地文件的链接。白名单必须落在 `_resolve_local_source` 内部（两个渠道函数都走它），而不是只落在 `extract_local_media_attachments` 的转附件分支里。
- 顺带：drain 车道只调提取、不调签名改写，主动推送的正文里可能残留原始 `![](C:\...)`。收紧后残留变成不可解析的 markdown，可接受；若要干净，在 drain 里补一次 `strip_unresolvable_media_refs`。

验收：
1. 渠道会话 `![](C:\Windows\win.ini)` 既不产生附件、也不产生签名链接（正文里不得出现 `/api/ceo/media/original?token=`）。
2. 同一份文本走 Web 车道，签名链接照常可点开。
3. workspace 内文件、会话 upload dir 在渠道侧都正常（表情包库在 `<workspace>/stickers/`，已被 workspace 覆盖，不额外开根）。
4. 附件投递失败降级成链接时，越界路径不得出现在降级行里。

### P0-3 网页车道动图被压成静态（用户确认 QQ 收 gif/webp 后暴露）

- 现状：网页车道走 `rewrite_assistant_media_content` → `_build_thumbnail`（`ceo_media.py:144-164`）：PIL `img.load()` 只取首帧、RGBA 铺白底、存 JPEG。`.gif`/`.webp` 都在 `_RASTER_SUFFIXES`（`:40`）里 → **动图在网页端只显示静态首帧，透明背景变白**。
- 渠道车道不受影响（发原图字节，见 P1c 校验 3）。
- 改法（初版改错过一次，实测修正）：**把原图字节原样复制**到会话 upload 目录的 `inline_media/anim_<digest>_<name>.<ext>`，markdown 用副本路径做 src、签名原图 URL 放 title 槽。**不能**直接把 src 换成签名 viewer URL——前端只内联"能自己服"的本地 src，viewer URL 会让图片退化成一个写有 `image` 的可点文本（平台实测踩到）。
- 代价：动图多一份副本（表情包通常几十到几百 KB）。超大图仍走缩略图。
- 验收：网页会话里发一张动图，`<img>` 的 src 命中 `inline_media/anim_*.gif`，动画在播；PNG 仍走 `thumb_*.jpg`。

## 模型侧契约：只写 `包/分类` 键，不写路径、不读图

澄清一个容易误解的点（也是本方案与"让模型自己 `![](路径)`"的分界）：

- **模型永远不写绝对路径。** 它只在正文最末写 `[[sticker:atri/happy]]`；路径是运行时在 `resolve_markers` 里查目录、随机取一张、再改写成 `![](<abs>)` 的。模型看不到也不需要看到目录布局，所以 P0-2 收紧之后表情包依然能发。
- **发送时不读图。** 没有任何车道会在发图前把图片喂给模型。模型对图的了解全部来自**文字元数据**，分三档：

| 档 | 模型知道什么 | 谁产生 | 发送时的额外模型调用 |
|---|---|---|---|
| P1 分类车道 | 分类键 + 一行描述（`happy｜轻松、愉快、庆祝`） | 目录名 + 可选 `manifest.json` 的 category description | 0 |
| P1.5 人工/一次性标注 | 每张图的 caption + tags | 入库时人填，或批量跑一次视觉模型后落盘 | 0 |
| P2 语义车道 | 每张图的 caption + tags，向量检索出候选 ID | 入库时视觉模型写一次，永久复用 | 1 次工具往返（不读像素） |

- P1 的代价是**模型不知道随机取到的那张到底画了什么**。提示词必须写明"分类只说明大致用途，不代表具体画面、配字或人物"，否则模型会替那张图编内容。这不是措辞问题：分类车道里图和句子确实可能对不上，只能靠描述写得准 + 分类粒度别太粗来压。
- P1.5 是最划算的升级：不引入向量索引、不加工具、不破 `tools[]` 字节稳定，只是把"每张图一句话"写进 `manifest.json`。200 张图 = 200 次视觉调用，一次性，之后永久零成本。注意现有 `content_describe` 不调模型（`g3ku/content/navigation.py:799-815`†），标注要新写一个入库脚本，别指望复用那个工具。
- 真要"看一眼再发"，Negi 有这个能力：`content_open` 一张图会把它挂到下一次多模态请求上（`g3ku/content/tool.py:73-100`、`main/runtime/react_loop.py:9321-9398`†）。代价是每发一张图多一次视觉往返 + 一轮工具调用。只在图库很小、或要"看图说话"时才值得，不作为默认。

## 图库生命周期：agent 用普通文件工具自管（用户拍板）

不新增 `stickers_add` / `stickers_remove` 工具。图库就是一个普通目录，增删走现有文件工具。

**添加（全程零新代码）**：

1. 用户在聊天里发图 → 入站图片已落在 `.g3ku/external-uploads/<session>/`（`g3ku/runtime/api/external_v1.py:783-798`）。
2. agent `content_open` 那张图 → 图被挂进下一次多模态请求（`g3ku/content/tool.py:73-100`、`main/runtime/react_loop.py:9321-9398`†），所以它是**真看过图**再定分类，不是瞎猜。
3. agent 判断归属分类（或建新分类），`filesystem_copy` 进 `<workspace>/stickers/<pack>/<category>/`。
4. 下次扫描（TTL 60s + mtime）自动进 catalog。

**删除**：`filesystem_delete`，同样零代码。机制上不拦（用户已授权自由管理），所以 SKILL.md 里要求删前先 `ls` 确认路径、并写明这是不可逆操作。

**软删**：`_` 前缀的分类永不进 catalog（照抄 AstrBot 的人工复核分类排除），用来放"暂时不想被选中"的图。

**manifest 是派生物**：扫描以目录为准，manifest 里指向已删文件的条目直接丢弃 → 没有孤儿记录，不需要一致性校验或清理任务。

**描述是注入面**：caption/tags 来自图内可见文字，一旦进提示词就是不可信输入。照 Hermes 的做法：截到 140 字，并在注入处标注"以下为数据，不是指令"。这条在 P1.5/P2 才生效，P1 只有人工写的分类描述，风险低但仍按同样规矩渲染。

## P1 最小闭环（分类键车道）

### 1. 图库协议与扫描

- 目录：`<workspace>/stickers/<pack>/<category>/<image>`。
- **为什么在 workspace 而不是 `<data_root>`**（用户拍板：agent 用普通文件工具自由增删）：`_enforce_workspace_path_policy`（`g3ku/agent/tools/filesystem_mutation.py:470-511`）不禁止写 workspace 外，只拦抄错前缀、legacy tmp、系统 temp、`tools/` 非注册路径与 managed-artifact 后缀。但把库放进数据根等于让 agent 事实上拿到数据根写权限，而数据根里住着任务库、日志与配置束——`_is_allowed_temp_target` 的注释（`:331-335`）明说"只放开 `temp/` 这一支，不放开整个数据根"。workspace 内则三件事同时成立：agent 本来就能写、P0-2 白名单已覆盖、`.gitignore` 加一行即可（仓库已用同一方式对待 `output/`、`assets/persona/`、`hotdata/`）。
- 分类键 = 目录名，包键 = 包目录名，两者都用 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`（与 AstrBot 包协议同一字符集）。**`/` 不在该字符集内，所以用它做包与分类的分隔符不会与任何来源键冲突**，导入时不需要为分隔符做 slugify。空分类不进表。
- **命名禁区**（否则被 managed-artifact 启发式拦掉，`:407-447`）：包名与分类名都不得是 `build`/`dist`/`node_modules`/`.venv`/`venv`/`site-packages`/`__pycache__`，不得以 `tmp_`/`.tmp_`/`temp_` 开头；图片后缀 `.png/.jpg/.jpeg/.gif/.webp` 都不在 managed 后缀表里，安全。
- `<pack>/manifest.json`（只存**不可推导**的事实，派生量一律不落盘）：

```json
{
  "schema_version": 1,
  "pack": "atri",
  "theme": "动漫少女 ATRI，日常卖萌与吐槽",
  "theme_source": "vision|human",
  "source": {"kind": "astrbot_pack", "id": "...", "version": "...", "url": "...",
             "commit": "...", "imported_at": "...", "license": "..."},
  "categories": {"happy": {"description": "..."}}
}
```

  张数、分类清单都不进 manifest —— 扫描目录就有，落盘只会产生第二份会漂移的事实。
- **不落 INDEX.md**：清单只有一个渲染出口，就是头部钉住块（见 P1-3）。再生成一份文件就是第二份会漂移的事实。管理侧要看全量直接 `ls` 目录 + 读 manifest。
- **权威关系**：目录是唯一真相，manifest 是派生物（只存不可推导的 theme / description / source）。解析器只认目录；头部快照过期最坏的后果是模型选了个已不存在的键 → fail-closed（剥掉标记、不发图），不会发错图。
- 新增 `g3ku/stickers/catalog.py`：`scan_packs() -> [Pack(pack, theme, [(category, description, count)])]`，按目录 mtime 进程内缓存 60s。**扫描与渲染分离**：扫描结果供头部渲染、增量 diff、解析器校验三处共用同一份数据。
- 取图：`random.choice`，每次 `is_file()` + `st_size > 0`。

### 1b. 粒度定档（据同类项目实测）

| 项目 | 粒度 | 数量级 |
|---|---|---|
| AstrBot meme_manager | 人工分类 | 内置 19；社区包 10–21 |
| SillyTavern expressions | 人工分类（GoEmotions） | 28，LLM 选，需 JSON enum + Fuse 模糊匹配才稳 |
| Open-LLM-VTuber | 人工分类 | 个位数（8 表情 + 7 动作） |
| Telegram / Hermes | emoji 键 | 写入侧 1–20 emoji/张，但 Bot API 读取侧只回 1 个；Hermes 明确否决纯 emoji（"the same 😂 can be a laughing cat or a crying frog"），补了描述就退化成逐图标签 |
| Slack / Discord | 无分类 | 平铺名字键 |

- 定档：**单包约 20 类、一类一行描述、单类图片数控制在个位数到十几张**。单包超过 ~40 类不要再加类，直接上 P2 语义检索。
- 包数不受同一上限约束：模型是**先选包（看主题）再选分类**两步决策，每步的候选都在 10–20 量级，不是 45 个键平铺。P1d 说明这怎么落地。
- 反面样本：ATRI 包 551 张里 happy 90 / teasing 56 / action 22，类内分布严重偏斜 → 随机取图必然"分类对、图不贴切"。这是分类车道的固有代价，靠描述写准 + 控制类内张数压，压不掉的部分由提示词禁止模型臆断图内容。

### 2. 标记解析（纯函数，新增 `g3ku/stickers/marker.py`）

- 正式形态：`[[sticker:<包>/<分类>]]`，正则 `^\[\[sticker:([A-Za-z0-9][A-Za-z0-9._-]{0,63})/([A-Za-z0-9][A-Za-z0-9._-]{0,63})\]\]$`，只认**独占一行**。
- 容忍短形态 `[[sticker:<分类>]]`：该分类名在**恰好一个**包里存在时才解析；跨包重名（`atri/happy` 与 `panda/happy`）视为歧义 → 剥掉整行 + warning，**不猜**。这条容忍让模型漏写包名时仍能工作，同时歧义必然 fail-closed。
- `resolve_markers(text) -> (text, [Path])`：命中的标记**整行删除**，图片路径走返回值——**不是**替换成 `![](path)`。理由：`extract_local_media_attachments` 的 `stash()` 在 alt 为空时返回 `source.name`（`ceo_media.py:281`），替换成 markdown 会让渠道正文里凭空多出一行文件名（`atri_0421.gif`）。
- 不可解析（键不存在、跨包重名的短形态）→ 删整行 + warning。**任何分支都不把标记原样返回。**
- `strip_markers_for_stream(text) -> text`：给累计全文用，删完整标记行 + 扣住末尾"可能是半个标记"的行（`[[`、`[[sti`、`[[sticker:atri/ha` …）。delta 是累计全文，所以这个函数无状态、幂等，不需要解析器状态机。

### 3. 模型怎么知道有哪些图：清单常驻头部钉住块（用户拍板，替换按需 load 方案）

发送路径 **0 额外跳数**。原方案（load skill + 读 INDEX.md）每轮多 2 跳，而且 skill 正文作为工具结果**不进缓存前缀**，实际比常驻更贵。改为复用仓库里已落地的头部钉住 + 按轮注入机制（`work/skill-roster-context-plan.md` 那套）。

现成机制（全部已核）：

- 头部钉住块由 `render_pinned_contract_text`（`g3ku/runtime/frontdoor/tool_contract.py:270-295`）渲染，已经并列放着 `candidate_skills (loadable with load_skill_context; id | name | description)` 一族行（`:284`）。
- 块头原文 `PINNED_CONTRACT_SNAPSHOT_NOTE`（`:40-41`）："this block refreshes only at compaction; later runtime notices supersede changed values" —— **常驻 + 通知覆盖增量，正是本方案要的语义，已经是既有契约。**
- 重印代数 `skill_roster_epoch_for` / `bump_skill_roster_epoch`（`:62-84`），与 `tools[]` 同源（`_frontdoor_bundle_recommit_boundary`，`_ceo_runtime_ops.py:3950`）；注释 `:54-56` 明说"两份名单共用一套『什么时候允许变』的口径"，加第三份是同一模式的延伸。
- 钉住条目双载体（session 属性 + 进程 LRU 表 `_PINNED_CONTRACT_STORES`，`:47-52`），头部文本在重印之间被冻结复用。
- 增量通知走 `KIND_RUNTIME_NOTICE` 家族（`main/runtime/mid_insert.py:207-223`）：`[RUNTIME_NOTICE]` 前缀、`POSITION_TAIL_BEFORE_TURN_OVERLAY`（尾部追加，不顶掉前缀）、`durable_until_compaction`、`compaction="drop"`、`strip_at=post-compaction`、已有 `echo_guard=strip_skill_roster_notice_echo`。

接线五步：

1. **头部**：`render_pinned_contract_text` 在 `candidate_skills` 之后加一族 `sticker_packs` 行，按包分组（形状见 P1d）。因为它是 `pinned_contract_text` 的一部分，自动继承"钉住时点全量、重印边界才变"——两次重印之间增删图，头部逐字节不动，前缀缓存不受影响。
2. **清单不进 `pinned_contract_revision_key`**（`:200`）。纳进去的话每次增删图都触发重印 = 一次全量前缀作废。清单变化只在既有重印边界（token 压缩 / 手动压缩 / 空闲窗口第一跳）生效。
3. **增量通知**：新增一个 `[RUNTIME_NOTICE]` 子类型，内容是"自上次钉住以来新增/删除的包与分类键"。diff 基准是钉住条目里的清单快照，所以要与 `FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR`（`:45`）同款再加一个 `_frontdoor_pinned_contract_sticker_keys`。
4. **压缩时自动收敛**：`compaction="drop"` + `strip_at=post-compaction` 已保证通知块在压缩后消失，同一边界头部重印吸收全量。所以"下一个上下文压缩时列表自动更新"是既有语义，**不需要新代码**。
5. **回显防护白送**：通知块用 `[RUNTIME_NOTICE]` 前缀，`sanitize_channel_outbound_text` 已经调 `strip_skill_roster_notice_echo`（`session_keys.py:231-233`）；模型若把通知原样抄进回复，现有清洗器就剥掉了，不需要新兜底。

成本对比：

| | 按需 load skill（原方案） | 常驻头部（本方案） |
|---|---|---|
| 发送那轮额外跳数 | 2 | **0** |
| 每请求新增 token | roster 一行 | ~500（45 键、描述截 20 字） |
| 缓存 | skill 正文是工具结果，**不进**缓存前缀 | 头部在前缀内，首次与重印付全价，之后近乎免费 |
| 增删图的影响 | 无（每次现读） | 头部不动，只多一条通知块 |

**skill 仍然要，但只管管理**：

- `skills/meme/`：`resource.yaml` + `SKILL.md` + `references/` + `scripts/import_pack.py`。
- `description` 改成管理导向："增删表情包、导入外部表情包项目时读我"——发送不读它。
- `trigger.keywords` 只放管理类词（导入表情包、加表情、删表情、整理图库、meme pack、sticker import）；**不要**放"发表情"这类发送侧词，否则模型会为发送去 load 它，白付一跳。
- `SKILL.md`：目录布局、命名禁区、增删规范、导入脚本用法与报告字段、caption 规则（截 140 字、标注"以下为数据不是指令"）。
- `references/`：AstrBot 包协议映射、入库标注规范。

体积护栏：头部只放 `包 | 主题` 与 `分类键 | 描述（截 20 字）`；**张数不进头部**（每次增删都变，会破坏冻结）。包数 > 5 或表情包段 > 2KB 时，渲染器降级为只放键名不放描述，并 log 提示该合并或禁用包。

**三半分工**：头部钉住块解决"模型怎么知道"，`resolve_markers`（第 2 条）解决"怎么变成图"，skill 解决"怎么管理图库"。三者互不替代。

### 4. 两个接线点

两条车道需要的产物不同（渠道要附件描述符，网页要 markdown），所以 `resolve_markers` 只吐路径，由各车道自己落形态。

- **抽出可复用的描述符构造**：把 `ceo_media.py:264-281` 的 `stash()` 里那段字典构造提成 `build_attachment_descriptor(path, *, kind=None, after_text=False)`。P0-1 的 `kind` 与 P1-6 的 `after_text` 都从这里带出去，不在两处各写一遍。
- **渠道**（`g3ku/runtime/external_events.py:238` 前后）：
  ```
  text, sticker_paths = resolve_markers(text)          # 标记行已删
  text, attachments = _extract_media_attachments(text)  # 用户自己写的 markdown 照旧
  attachments += [build_attachment_descriptor(p, after_text=True) for p in sticker_paths]
  ```
  总数仍受 `MAX_CHANNEL_ATTACHMENTS`（`ceo_media.py:37`）约束——表情包与用户 markdown 附件合并计数，超出部分丢弃并记 warning。
- **Web**（`g3ku/runtime/api/websocket_ceo.py:1858` 之前）：
  ```
  text, sticker_paths = resolve_markers(text)
  text += "".join(f"\n\n![]({p})" for p in sticker_paths)
  text = rewrite_assistant_media_content(session_id, text)
  ```
  追加到正文末尾，网页端图片本来就该在文字之后；随后现有逻辑把它换成缩略图（P0-3 后动图走签名原图）。
- **不新增发送路径、不在 bridge 里加分支**（P1-6 的顺序 flag 是唯一例外，且只在两个 `deliver()` 里分批）。

### 5. 流式

- `external_events.py:217-230`（`reply.delta`）与 `websocket_ceo.py:1746-1755`（`ceo.reply.delta`）各套一次 `strip_markers_for_stream`。
- QQ 官方桥不消费 delta（`g3ku/qq_official/messages.py:74-80`†），不受影响。

### 6. 发送顺序（可后置）

- 现状：两条桥都是附件先、正文后（`bridge.py:928-946`、`dispatcher.py:279-300`），表情包会出现在句子之前。
- 改法：附件描述加 `"after_text": True`（仅表情包来源），两条 `deliver()` 分两批发；正文为空时忽略 flag。
- 不改也能用，先接受"先图后文"。

### 7. 配额

- 官方 QQ 群主动消息有 15/min、1000/day 闸（`bridge.py:195`†）；一张表情包 = 一条额外消息。
- 默认 `stickers.max_per_reply = 1`，且只在被动回复窗口内发（`_passive_reply_kwargs` 已存在）。

## P1c 导入：外部表情包项目（独立可上）

用户会拿已经人工分好类的包来导入，典型是 AstrBot meme_manager 的包协议（`manifest.json` + `memes/<分类>/<图片>` + 可选 `previews/`、`semantic_metadata.json`）。目标不是写一个万能解析器，而是**一个规范落点 + 少量形状探测 + 认不出的形状一律不猜**。

### 效率：一条 exec 跑完，不走文件工具

- 落点是 skill 自带脚本 `skills/meme/scripts/import_pack.py`（惯例已有：`skills/contributor-codebase-analyzer/scripts/checkpoint.sh`，SKILL.md 里以 `./scripts/xxx` 调用）。agent 一条 `exec` 跑完 500–1000 张，秒级。
- 反面：走 `filesystem_copy` 逐张搬 = 每张一次模型往返，ATRI 那种 551 张的包等于 551 跳，不可行。这是"agent 自由管理"的唯一例外——**单张增删走文件工具，批量导入走脚本**。
- 命令形态：`python skills/meme/scripts/import_pack.py <src> [--pack NAME] [--theme "一句话"] [--force] [--dry-run] [--classify-map FILE]`；`--dry-run` 先出报告再落盘，避免导错 500 张再删。
- **报告必须短**：按分类聚合的计数 + 异常清单，不打印逐个文件名。工具结果超 6000 字符会被外置成 artifact、模型只看到 6 行预览（`g3ku/content/navigation.py:505-520`），输出体积得由脚本自己控制。
- 脚本经 `exec` 运行，**绕过 `_enforce_workspace_path_policy`**，所以脚本必须自限：只写 `<workspace>/stickers/`，解压与 clone 只落 `temp/`（`data_work_path('temp')`，也正好是 managed-artifact 规则唯一放行的落点）。

### 通用性：形状探测表

| 源形态 | 判据 | 处理 |
|---|---|---|
| AstrBot 包 | 根有 `manifest.json`（含 `schema_version`）+ `memes/` | 逐字段映射见下节；`previews/` **丢弃**（包预览图，不是可用表情） |
| 一层子目录图床 | 无 manifest，子目录内全是图 | 子目录名 → 分类键（slugify） |
| 平铺图堆 | 无 manifest，根目录全是图 | 默认进 `_review/`（永不进 catalog）并报数量；带 `--classify` 时跑视觉模型按**全库已有键**自动归类，低置信仍进 `_review/` |
| Telegram 导出 | `.tgs`/`.webp` + emoji 命名 | webp 转换；emoji 不当键，写进 description |
| 压缩包 | `.zip`/`.rar`/`.7z` | 先解到 `temp/` 再走上面几条 |
| git 仓库 URL | `https://github.com/...` | clone 到 `temp/`，用完即删 |

`_review/` 是通用性的兜底：认不出的形状进这里而不是瞎猜分类，复用 P1 的 `_` 前缀软删机制，不新增概念。

### AstrBot 包兼容（逐字段映射）

AstrBot 是最高价值输入：分类人工分好（社区包实测 10–21 类，正落在 1b 的档位）、每类带 1–2000 字人工描述、可选带逐图语义元数据。所以按它的包协议做一等适配，不做泛化猜测。

| AstrBot 包协议 | 我们的落点 | 处理 |
|---|---|---|
| `manifest.json:schema_version` | — | 只认 `1`；不认识的版本 → 拒绝导入并报告，不降级猜测 |
| `manifest.json:id` | 包目录名 | slugify 到我们的包键字符集；与已有包重名 → 默认拒绝，需 `--force` 或 `--pack` 改名 |
| `manifest.json:version` | `source.version` | 原样记，用于增量重导判断 |
| `manifest.json:categories[].description` | `categories.<键>.description` | 原样搬（1–2000 字）；头部块里截 20 字渲染，全文留在 manifest 供 P2 用 |
| `memes/<分类>/<图片>` | `<pack>/<分类>/<图片>` | 目录名即键，字符集与我们相同（`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`），**不需要 slugify**；`/` 是我们新引入的分隔符，不在该字符集内，所以不冲突 |
| `previews/` | 丢弃 | 包预览图不是可用表情，混进去会污染随机取图 |
| `semantic_metadata.json` | `<pack>/semantic/metadata.source.json` | **原样复制**，不重算。没有 P2 时这些 ID 没有消费者；开启 P2 前必须重算（见下） |
| （无对应字段） | `theme` | **AstrBot manifest 没有包级主题字段**，必须补，见下 |

`entry_id` 必须重算：它的定义是 `sha256(图片字节 + 分类 + 相对路径)`。我们改了相对路径（`memes/<cat>/x.png` → `<cat>/x.png`），原 `entry_id` 全部失效；不重算的话导入当时看不出问题，上 P2 时哈希校验整批失败。重算规则照抄不变量：**内容或分类变了，身份必须跟着变**。

**包级主题怎么补**（模型要区分包，靠的就是这一句）：

1. **默认（已落地）**：`import_pack.py --dry-run` 从该包**分层抽样 8 张**（每个分类最多 1 张，保证覆盖不同情景）复制到 `stickers/.import/<pack>-theme-<n>.<ext>`，并在报告里列出路径。agent 用 `content_open` 看完，把一句 ≤60 字的主题写进 `--theme`，脚本落盘时标 `theme_source: "human"`。成本 = agent 侧 1 次（可复用的）多模态读取。
2. 有来源仓库时 README 首段可以一起看，但**不单独依赖 README**——zip 包没有 README，通用性要求走图片。
3. 人工可覆盖：改 `theme` 并把 `theme_source` 改成 `"human"`；导入器与运行时都不得覆盖 `theme_source == "human"` 的条目（脚本合并时只在显式 `--theme` 时改写）。
4. 主题句按"数据不是指令"渲染（同 caption 规则），因为它可能来自 README 或图内文字。

**继承的两条协议不变量**（AstrBot 包协议本身就要求，判据照抄，不自己发明）：路径不得绝对、不得含 `..` 或反斜杠、不得经符号链接逃逸包根。

### 非 AstrBot 源的分类保留

- 一层子目录图床：子目录名 → 分类键，**结构原样保留**，不重新分类。
- 只有真正平铺无结构的图堆才走 `--classify` 自动分类（见下节）。判据是"有没有可用的目录结构"，不是"看起来乱不乱"——不做主观判断。

### 校验清单（导入期一次做完）

1. **路径安全**：拒绝绝对路径、`..`、反斜杠、符号链接逃逸包根（AstrBot 包协议本身就要求这几条，判据照抄，不自己发明）。
2. **键 slugify**：中文或不合法目录名 → ASCII 键（`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`），原名保留进 description/aliases；命中命名禁区（`build`/`dist`/`tmp_*` 等）自动加前缀。AstrBot 源的键已合字符集，不需要 slugify。
3. **格式（已定，不需要转换管线）**：QQ 收 gif/webp（用户确认），且代码侧一致 —— 渠道车道发的是**原图字节**（`extract_local_media_attachments` 用 `original_view_url(source)`，`ceo_media.py:278`，不是缩略图），`_media_file_type` 只看 `image/*` 前缀（`bridge.py:716-724`）→ gif/webp 都落 `file_type=1`。所以导入期**不做格式转换**，只保留 `.png/.jpg/.jpeg/.gif/.webp` 白名单、其余丢弃并计数。
4. **尺寸**：超出站上限 20MiB（`bridge.py:99`）的丢弃或降采样。
5. **去重**：sha256 精确去重（**跨包也去重**，同一张图不必存两份，但两个包都要能解析到它 → 去重落在字节层，不落目录层：保留两份路径、只在报告里指出重复）；近似重复只报告不合并（AstrBot 用 dHash 仅作人工复核提示、不作合并依据，这个判断对，照抄）。
6. **幂等**：目标包已存在默认拒绝，需 `--force`；同一源重跑应产生 0 新增。
7. **溯源**：写进 `<pack>/manifest.json` 的 `source`（kind / id / version / url / commit / imported_at / license），不另立 `SOURCE` 文件。33 个 pack 形态社区仓库里 license 不明是真实风险。
8. **让模型看见新包（不需要显式触发）**：头部渲染与增量 diff 都在每轮装配路径上（`scan_packs()` 结果 vs 钉住快照），脚本落盘后**下一轮装配自然产出 `[RUNTIME_NOTICE]`**。导入脚本自己不发通知——它跑在 `exec` 里，拿不到会话。

## P1d 多包共存与自动分类

用户会导入多个不同主题的包，彼此有重叠情景（高兴、尴尬…）。原则：**包和分类是两个正交维度，不压成一个键空间。**

### 物理层：两级目录天然保留来源结构

`stickers/<pack>/<category>/<image>`。AstrBot 包导入后是 `atri/happy/`、`panda/happy/`，各归各的包，不混。来源的分类结构完整保留，可整包删除、整包禁用。

### 模型看到的键空间：命名空间视图（用户拍板：模型要能区分包）

标记形态 `[[sticker:<包>/<分类>]]`（见 P1-2）。键总数会随包数增长（3 包 × 15 类 = 45），但**模型从不在一个平铺列表里面对 45 个键**——它做的是两步决策，每步候选都在 10–20 量级：

1. **选包**：头部块里每包一行 `包名 | 主题`。模型先按"这轮该用哪个风格/角色"选包。
2. **选分类**：只在选中那个包的缩进分组下看分类键与描述。

头部块的渲染形状（与既有 `candidate_skills` 同风格的纯文本行，按包分组，不是一张大表；张数不进头部，见 P1-3 体积护栏）：

```text
sticker_packs (emit `[[sticker:<pack>/<category>]]` as the last line; `pack | theme`, then indented `category | description`):
- `atri` | 动漫少女 ATRI，日常卖萌与吐槽
  - `happy` | 开心大笑、庆祝
  - `awkward` | 尴尬、被说中
- `panda` | 熊猫头，黑白搞笑，吐槽与自嘲
  - `happy` | 得意、坏笑
```

于是 SillyTavern 那个"28 类就要 enum + 模糊匹配"的问题不会出现：它是一步在 28 个平铺项里选，我们是先在 2–3 个主题里选、再在 10–20 个分类里选。

- **主题句是包级必填**，因为"区分包"要能区分的是主题，不是包名。`atri` 这个名字对模型没有信息量，"动漫少女 ATRI，日常卖萌与吐槽"才有。主题来源见 P1c 的 AstrBot 兼容节（AstrBot manifest 没有这个字段，要补）。
- 同一分类名在不同包里语义可以不同（`atri/happy` 是大笑、`panda/happy` 是得意），**这正是命名空间视图的价值**：描述各自写准，不互相污染。合并视图做不到这点。
- 短形态 `[[sticker:happy]]` 只在跨包唯一时容忍，重名即歧义 → fail-closed（P1-2）。
- **包级禁用**：包目录加 `_` 前缀（复用软删机制），该包整体不进头部块、不可解析。想固定风格就只留一个包。

### 参照：AstrBot 自己怎么处理多包（源码核实，`b09598e`）

它**不解决**这个问题——它把包维度从模型视野里删掉，再用三条机制兜住后果：

1. **分类键是全局单一命名空间**。`category_mapping` 是扁平 `{键: 描述}`，提示词里只有这一张表；过滤条件是"任一绑定包的 `memes/<键>/` 非空"（`main.py:903-914`）。多包同名分类合并成一个键。
2. **描述冲突按主包优先覆盖**：`resolver.py:266-270` 反序遍历 + `dict.update()`，注释写明 "primary takes precedence"，`pack_ids[0]` 是主包。**非主包的分类描述被静默丢弃。**
3. **取图跨包无差别随机**：`random.choice` over 并集，无权重、无按张描述。风格隔离交给"绑定"（`resolve_pack_ids(session_id, persona_id)`）——想让风格一致，操作者就别把不兼容的包绑在一起。

第 2 条是个静默失败，也是我们不抄它的理由：非主包的 `happy` 若是"得意"而主包是"大笑"，**提示词里只有主包的描述，而图是从两个包随机取的** → 模型读到 A 的描述、可能发出 B 的图。命名空间视图下两个描述各自成立，不会互相覆盖。

两条对我们有用的副产物：

- `runtime_category_mapping` 明确拒绝含 `/` 或 `\` 的分类名（`models.py:107-114`）→ **我们用 `/` 做包/分类分隔符与 AstrBot 导入零冲突**，这条从"推断"升级为"源码确认"。
- 导入 AstrBot 包时，它的 `categories[].description` 要**按包各自存**进我们的 manifest。AstrBot 自己在多包场景会丢掉非主包的描述，我们不丢——所以导入后的信息量严格多于源项目自身运行时所能用到的。

### 自动分类：只许从全库已有键里选

只有真正平铺无结构的图堆才走这条路（`import_pack.py --classify`）。

- **候选集 = 全库已有分类键的并集**，不是本包的键——新导入的包本身是空的，只看本包等于没有候选。跨包沿用同一套语义键有两个好处：短形态 `[[sticker:happy]]` 仍可解析（跨包同名时 fail-closed，见 P1-2），且不同包的同类情景在语义上对齐。
- 硬约束：**不许发明新键**。否则导 3 个杂包就长出 3 套私有分类，键数爆炸且新键没有描述、模型选不准。加新类必须是显式决定（建目录 + 写一行描述），由 agent 或人做，不由导入器做。
- 低置信 / 无合适键 → `_review/`，不猜。
- 批量 30 张一次（Hermes 的做法），500 张 ≈ 17 次调用，一次性成本。
- **实现路径已定（与初版有偏离）**：脚本**不自己调模型**。本地 OpenAI 兼容端点要 `require_external_api` 的外部令牌（`openai_compat.py:464` → `Depends(require_external_api)`），CLI 里配令牌不划算。改为把判断交回 agent：脚本 `--dry-run` 报告里给出 `theme_samples`（分层 8 张路径已复制到 `stickers/.import/`），agent 用 `content_open` 看图后带 `--theme` 定稿。agent 本身就是这条链路里的多模态模型，不需要第二套调用。
- 分类同理：`--classify-map` 接收 `{相对路径: 分类键}`，平铺图堆的归属由 agent 看图后给出；未映射的进 `_review/`。没有实现"脚本内跑视觉分类"，原因同上。

### 语义冲突：命名空间视图下大部分冲突自动消失

- **跨包同名不再是冲突**：`atri/happy`（大笑）与 `panda/happy`（得意）各有各的描述，互不污染。这正是选命名空间视图而不是合并视图的主要收益——合并视图下这两个语义会被压成一行糊掉的描述。
- 剩下的冲突只在**同一个包内**：自动分类把语义不同的图塞进了同一个键。导入器只**报告**（"`panda/happy` 里混进了 12 张自嘲图，考虑拆出 `selfmock`"），**不自动拆键**。拆键是分类学决定，自动做会让键空间随每次导入漂移，模型选择准确率无法稳定。

## P2 精确取图（已实现，默认关）

实现形状与初版有三处偏离，都是为了少建基础设施：

- **检索是词法而非向量**：几百张图的量级、查询由模型现写，对 `caption + tags + visible_text + category` 做词项/中文二元组重叠就够。接口仍是 `query -> candidates`，以后换向量后端不改调用方。
- **标记是 `[[sticker:<包>/@<id>]]`**：`@` 不在合法键字符集里，与分类键不可能混淆（初版裸写 12hex 会和"恰好长成十六进制"的分类名撞车）。
- **没有本轮候选表**：id 是内容哈希前缀（`sha256(bytes + category + relpath)[:16]`），伪造或过期的 id 要么唯一匹配失败、要么哈希对不上，两种都不发图。候选表能多拦的只有"前几轮见过的 id"，属口味问题不是安全问题；省掉它是为了不引入 per-turn 台账。

落图前校验链（任一不过即不发）：形态合法 → 包存在且未被 `_` 禁用 → id 前缀在该包元数据里唯一 → 分类非 `_review`/`_` 前缀 → 文件存在且落在包内 → 当前字节 sha256 与元数据一致。

- **工具**：`tools/search_stickers/`，`build()` 在 `stickers.semanticSearch` 关闭时返回 `None`（不注册），所以默认安装的 `tools[]` 与今天逐字节相同；打开它才付一次前缀作废 + 水合首跳。
- **元数据**：`<pack>/semantic/metadata.json`，由 `skills/meme/scripts/caption_pack.py` 从 agent 填的 `{relpath: {caption, tags}}` 生成；`entry_id` 与 `sha256` 由磁盘字节现算，脚本不接受调用方给的身份。
- **AstrBot 自带 `semantic_metadata.json`**：导入时仍原样复制为 `metadata.source.json`（结构未核实，不臆造）；要让 P2 用上，按上面的 `--map` 走一遍 `caption_pack.py` 重新计算。
- **验收**：`test_sticker_semantic.py` 覆盖身份随内容变化、搜索排序、id 唯一前缀、换图后拒绝、未知 id 整行剥掉。

## 不做

- **不新增"发图"工具、不新增 bridge 分支**：投递必须发生在正文定型之后，工具调用发生在生成之中；`send_message` 又只吃纯文本（`main/runtime/internal_tools.py:362-381`†），加图片等于再造一条侧效路径，还得解决幂等（现在没有 tool_call_id 去重）。
- **不加 per-turn nonce**：上一轮我建议过，读过代码后撤回。分类键被伪造的后果只是"用户让 bot 发了张图"；真正的注入风险是任意本地文件出站，那条由 P0-2 解决，nonce 只给模型多加一个抄错的机会。
- **不做辅助模型选图**：等 P1 跑一段，看漏发率和贴切度再说。
- **不做自动拆键**：见 P1d 语义冲突节。

## 风险

1. **纯表情回复的账本语义**：`record_outbound_message(text="", attachments=...)`（`external_events.py:265-271`）今天没有这种入参组合，ack 与重试要专门测。
2. **群聊配额**：每张表情包吃一次主动消息额度，长任务里容易撞 15/min。
3. **代码块里的伪标记**：`resolve_markers` 只认独占一行，代码块中的行也是独占行 → 会被误认。若不接受，加"前一行是 ``` 时跳过"的判据（两行）。
4. **图被换/删**：P1 只校验 `is_file()`，P2 才上 sha256。
5. **P0-2 的连带影响**：`_resolve_local_source` 同时服务 Web 缩略图与签名链接；白名单会让"workspace 外文件"从可点变成不可点，属预期收紧，但要写进变更说明。
6. **头部快照与目录漂移**：钉住快照在重印边界之间是冻结的，期间增删只体现在通知块里。若通知块被阶段裁撤（`mid_insert` 的 `strip_at` 语义），模型可能拿旧键发送 → 解析器只认目录，最坏是**漏发**（fail-closed），不会错发。反方向（新增的键模型还不知道）只持续到下一个重印边界。进程重启后钉住表（`_PINNED_CONTRACT_STORES`，仅进程内）失效 → 首跳重钉一次，直接把最新清单吸收进头部，通知块自然为空。
7. **包主题句质量**：视觉模型抽样 8 张生成的主题可能抓错重点（例如把角色包概括成画风）。缓解：`theme_source` 标记来源、人工可覆盖且不被回写；导入报告里把主题句回显给 agent，让它在同一轮里就能改。
8. **自动分类的错误是静默的**：`--classify` 把图归错类不会报错，只会在某天发出不贴切的图。缓解：默认不启用（要显式带 `--classify`）、低置信进 `_review/`、导入报告给出每类的张数分布让人一眼看出异常。
9. **头部体积随包数线性增长**：5 个包 × 20 类 = 100 行 ≈ 3KB 常驻前缀。在缓存里近乎免费，但每次重印要付全价，且占上下文预算。护栏见 P1-3（>5 包或表情包段 >2KB 时降级为只放键名不放描述）。真要支撑十个包以上，才考虑退回"头部只放包 + 主题、分类表放 skill 的 `references/` 按需 load"——那时才值得付那 2 跳。

## 交接

- 已逐行复核：`ceo_media.py` 全文、`external_events.py:150-300`、`session_keys.py:195-237`、`websocket_ceo.py:1710-1876`、`qq_official/bridge.py:710-959`、`qq-onebot/dispatcher.py:235-300`、`turn_overlay.py`、`message_builder.py:1505-1545` 与 `1935-1965`、`filesystem_mutation.py:320-521`、`openai_compat.py:150-200`、`tool_contract.py:40-84` 与 `270-305`、`mid_insert.py:180-250`、`skills/add-tool/*`。
- 标 † 的读数动 P2/流式前复核。
- 已拍板：渠道严/网页宽的路径白名单；agent 用普通文件工具自由增删；命名空间视图（模型要能区分包）；QQ 收 gif/webp 故不做格式转换；**清单常驻头部钉住块 + `[RUNTIME_NOTICE]` 增量，meme skill 只管管理**（替换早先的"发送时按需 load skill"方案，理由见 P1-3 成本对比）。
- 第一步只做 P0（三项：`kind`、路径白名单、网页动图例外），各自可独立验收，不引入新模块。P1 里最省力也最值钱的顺序是：先接头部渲染（复用 `render_pinned_contract_text`，不新增 epoch），再接解析器与两个接线点，最后接通知块。

## 落地记录（2026-10-26）

| 批次 | 提交 | 内容 |
|---|---|---|
| 计划 | `53fbf30f`（随用户 checkpoint 提交） | `work/sticker-send-plan.md` |
| P0 | `71f084e6` | `kind` / 渠道路径白名单 / 网页动图例外 |
| P1 核心 | `5cf1dd53` | `g3ku/stickers/{catalog,marker}.py` |
| P1 接线 | `ba6626aa` | 两条出站车道 + 流式扣留 + 桥侧 `after_text` |
| P1 头部 | `b3496194` | 钉住块 + `sticker_pack:*` 通知分节 + 渲染器 v3 |
| P1c | `bc5cff48` | `skills/meme/` + `import_pack.py` + 测试 |
| 修正 | `28e7cf83` | `stickers_root` 不再经 `web_ceo_sessions`（绕开基线既有循环导入）；`.gitignore` 根锚定 `/stickers/` |
| 修正 | `16f15c99` | 转录落盘前把标记换成图片 markdown（确定性选图）；动图改暂存原样副本 |
| 修正 | `51d58fbc` | 三个用户可见渲染面统一走 `_render_assistant_media`；会话列表预览去掉 markdown |
| 配置 | `3c9dfb26` | `stickers.maxPerReply`（调过才落盘 + 豁免前缀，老 config 照常加载） |
| P2 | `36f67f42` | 逐图元数据 + `search_stickers` + `[[sticker:<包>/@<id>]]` + `semanticSearch` 开关 |
| 覆盖 | `b41b3f02` | QQ 出站顺序/纯表情（假 botpy 驱动真桥）+ 网页流式帧抽成可测函数 |

### 验证边界：哪些必须真连 QQ

本机已可验证（不需要账号）：

- 出站事件形状：`attachments[].kind` / `after_text`（`test_external_api_events` / `test_ceo_media_middle_layer`）。
- **QQ 桥的真实代码路径**：用仓库自带的假 botpy + httpx MockTransport 驱动 `run_qq_official_bridge`，断言正文 `msg_type=0` 先发、附件经媒体 client 上传后以 `msg_type=7` 发、纯表情回合不发空文本（`b41b3f02`）。
- 网页三面（实时 final / in-flight 快照 / 历史行）都不含标记；会话列表预览只留纯文本。
- 动图走 `anim_*` 原样副本；PNG 走 `thumb_*`。
- 增量通知（`demo2` 只可能从 `[RUNTIME_NOTICE]` 得知）、skill 装载、头部缓存命中。
- 流式：`_reply_delta_frame` 是纯函数，单测覆盖完整标记/半截标记/纯标记帧。

**只能真连 QQ 验证的**（假 botpy 把这些全 mock 了）：平台是否接受 `file_type=1` 的 gif/webp、群聊富媒体是否吃主动消息额度、被动回复窗口（`msg_passive`）里媒体消息的实际表现、以及"文字在前、图在后"在真实客户端的观感。

工作树里现在有两套可直接灌给真 QQ 的测试数据：`stickers/demo`（PNG×2 + GIF×1）、`stickers/demo2`（PNG，用于验证通知）。接上 QQ 后只需在该渠道会话里发一句"夸我一句，最后附一张开心类表情包"，然后看桥日志的 `qq-official delivered c2c message (attachments=1)` 与客户端顺序。

### 平台会话实测（2026-10-26 23:08，web 车道，非 QQ）

前置：`stickers/demo/`（`happy` 两张 PNG + `awkward` 一张 GIF，manifest 带主题与分类描述）；`negi bootstrap` 优雅退出后重启。

| 检查项 | 结果 |
|---|---|
| 头部携带清单 | 用户只说"开心类"，模型自己写出 `[[sticker:demo/happy]]`——没提包名，说明清单确实在头部且被读到 |
| 标记不出站 | 实时帧与刷新后 DOM 里都没有 `[[sticker:` |
| PNG 渲染 | `<img>` src = `inline_media/thumb_*.jpg`（走缩略图，符合预期） |
| GIF 渲染 | `<img>` src = `inline_media/anim_*_a1.gif`（原样副本，动画保留） |
| 历史回放 | 刷新后两条回复都渲染成"文字 + 图片"，无标记 |
| 转录文件 | 新会话 `sessions/web_ceo-*.jsonl` 里 `[[sticker:` 计数为 0，assistant 行是 `![](...)` |
| 头部缓存 | 第二轮 `缓存命中 13.7k / 99.0%`，头部未破前缀 |

实测中发现并已修的两处缺陷（都不在初版计划里）：

1. **viewer URL 做 src 会让图片退化成可点文本**（P0-3 初版改法错误）→ 改为暂存原样副本。
2. **历史回放露出原始标记**（转录存的是模型原文）→ 在 `_persist_turn_transcript` 落盘前换成图片 markdown，并把选图改成"按回复文本确定性"，否则实时帧与转录各取一张、刷新后换图。

验收现状：`test_sticker_packs`（17）、`test_sticker_import_script`（6）、`test_ceo_media_middle_layer`（10，含动图副本）、`test_external_api_events`（含 3 条表情包链路用例）、`test_runtime_contract_notice`（头部冻结/只投一次）、`test_frontdoor_pinned_contract`（round-trip/冻结）全绿；OneBot 桥 38 条全绿。

**与初版的偏离（三条，都是"脚本不自己调模型"的连带）**：

1. `--classify` 改成 `--classify-map`：本地 OpenAI 兼容端点要外部 API 令牌（`Depends(require_external_api)`），CLI 配令牌不划算；分类判断交回 agent（它本来就是多模态），未映射的进 `_review/`。
2. 包主题不再由脚本跑视觉模型生成，改为脚本给出 8 张分层抽样、agent 看完带 `--theme` 定稿（`theme_source: "human"`）。
3. `semantic_metadata.json` 原样复制为 `semantic/metadata.source.json`，**不重算 `entry_id`**（缺 schema 证据，不臆造结构）。P2 开工前必须重算，否则候选表校验会整批失败。

**未实现 / 待办**：

- P2 已实现但**默认关**（`stickers.semanticSearch`）：没有 caption 的库检索不出东西，且加工具会让 `tools[]` 前缀作废一次。打开前先跑 `caption_pack.py`。
- `stickers.max_per_reply` 已接配置（`3c9dfb26`）。
- **平台（web）车道已实测通过**（见上表）。**QQ 桥的代码路径也已覆盖**（假 botpy 驱动真桥，`b41b3f02`）；**只剩平台接受度需要真账号**，见「验证边界」。
- 流式：网页侧已抽成 `_reply_delta_frame` 纯函数并有单测（完整标记/半截标记/纯标记帧）。浏览器中间帧仍没抓到，但那已经只是"同一函数有没有被调用"的问题，不再是"标记会不会漏"。
- 旧会话（本次实测前创建的 `web:ceo-96681f6af630`）转录里仍有 3 处历史标记——那是修复前的落盘数据，新会话已干净。需要的话在界面里删掉那个会话即可。

**与本次无关的既有失败**（在干净 HEAD 上同样失败，已用 detached worktree 复核）：`test_frontdoor_pinned_contract::test_continuation_seed_head_is_not_stacked_with_a_second_pinned_block`（缺 `persona_text` 参数）、`test_ceo_create_agent_runner::test_parse_create_async_task_result_recognizes_task_append_notice_guidance`（`duplicate` vs `append_notice`）。
