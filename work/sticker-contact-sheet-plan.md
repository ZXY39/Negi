# 表情包拼图（contact sheet）改造计划

状态：**待确认**。确认前不动代码。
前置文档：`work/sticker-admin-ui-plan.md`（已落地的管理界面与 P0–P5）、本文件取代其中"精确取图/语义检索"路线。
实测依据：`temp/sheet-probe/`（探针脚本、9 张拼图、14 份模型回复；验收后删除）。

---

## 0. 目标

把"精确取图"从**文本语义检索**改为**拼图视觉选号**：模型调用工具拿到某分类的整张拼图（每格带序号徽标），当轮写 `[[sticker:<包>/<分类>/<序号>]]` 发送该格。
同时按"精简"原则删除图片级文本结构（caption/tags/entry_id/sha256/检索工具），把入库收敛为"分类形状导入 + 注册制"。

## 1. 不变量（机制级，验收逐条钉）

1. **序号 = 文件名**：`<分类>/<NNN>.<ext>`，三位零填充；铸号 = 分类内已注册最大号 + 1；**append-only，删除留空洞，永不重排**。序号永远指向同一张图；图被删 = 不发（fail-closed），不是错发。
2. **可见性真相 = 注册表**：目录只证明"文件存在"；渲染、索引解析、类别随机车道只认**已注册**文件。直接往文件夹丢文件**不生效**，出现在 UI"未注册"列表与工具响应的 pending 里。
3. **一分类一拼图且穷尽（≤36）**：单分类注册上限 `MAX_IMAGES_PER_CATEGORY = 36`（实测干净点，1256²，弱模型 5/5）。注册入口程序性拦住第 37 张；**超额文件保持未注册**（UI 未注册列表可见），渲染与解析永不超 36，因此不需要任何截断分支。≤36 时该分类所有已注册图都在拼图里。
4. **即用即读**：拼图只注入"取图那一跳"的请求尾巴（overlay），**不进耐久历史、不进稳定前缀**。cache key 只读 `model_messages + tool_schemas + refs`（`main/runtime/react_loop.py:726`），因此注入/不注入都不影响缓存身份。
5. **非多模态降级**：工具返回点名当前模型配置的提醒文本（复用 `g3ku/content/tool.py:59` 形态）+ "仍要发就用类别语法，系统随机挑"；类别车道 `[[sticker:<包>/<分类>]]` 永久保留且不依赖视觉。
6. **通知仅分类级**：图片注册/删除不改钉住头、不发通知；分类增删或描述变化才发 per-pack 分节（现有机制，不改）。

## 2. 实测基线（本计划所有默认值的来源）

- 选号准确率（模糊意图→序号）：deepseek-flash 9/12/16/24 格 20/20；sensenova-6.8-flash-lite 9/12/16 格 15/15、24 格 3/5。
- 24 格唯一确认的可读性失败 = **tile 内有效主体尺寸过小**（黑画布 + 居中小 emoji）；**内容 bbox 裁剪 + 等比 fit 放大**实测修复（sensenova 该题翻转正确）。
- 整图 1648²（tile320）相对 1048² **零收益**；源分辨率 29/93 张 ≤240px、12 张 ≤64px，加像素不产生信息。
- 容积：36 格 1256² 弱模型 5/5；49 格 1464² 4/5（最细线索失配）→ cap 定 36，**不建分页、不建截断**。
- 三张同跳（93 图）7/9：跨张视觉竞争使同组基线题 5/5→3/5 → **不做多张同视野、不做分页**（分页已被"cap 36 + 穷尽"取代）。
- 渲染配方：tile 200px、内容 bbox 裁剪（灰度差阈值 24 二值化，GIF 黑底抖动噪声必须阈值化）、fit 放大（`Image.thumbnail` 只缩不放，必须显式 resize）、等比 letterbox、深色圆角底 + 白字徽标（字号 ∝ tile）、整图边长 = `cols×200+(cols+1)×8`、JPEG q85（PNG 在字节上限内优先）。

## 3. 落盘与注册表

```
<workspace>/stickers/<pack>/
  manifest.json        # {"theme":…, "categories":{"<key>":{"description":…}}}  瘦manifest
  registry.json        # {"schema":1,"categories":{"<cat>":{"<NNN>.<ext>":{"sha256":…,"registered_at":…}}}}
  <category>/<NNN>.<ext>
  _<…>/                # 隐身车道：不渲染、不受 36 约束；set_category_enabled 变可见时若 >36 拒绝
<workspace>/.g3ku/sticker-sheets/<sheet_id>.png   # 拼图缓存；绝不放 stickers/ 内（会被当表情包扫入）
```

- `registry.json` 为新增唯一注册表；`semantic/` 目录约定废除。
- 注册时记 sha256；渲染/解析时字节哈希不符 → 该图按未注册处理（drop + UI 标"内容已变更，需重新注册"）。
- **bootstrap 迁移**：新代码首次扫描对存量包幂等注册现有文件（按文件名排序铸号），**每分类至多 36 张**，超额留在未注册。验收钉死：迁移后 deepseek-girl-pack 93 张全注册（现有可见分类最大 9 张，不触顶）、各分类编号连续无空洞。
- 注册入口（三处，同一函数 `registry.register(...)`，36 上限在这一处强制）：
  1. 导入 commit（§5）；2. UI 上传 / "注册未注册文件"按钮；3. 新工具 `register_stickers(pack, category?)`。

## 4. 标记语法与解析

- 新增 `INDEX_MARKER_RE = ^\[\[sticker:(seg)/(seg)/(\d{1,3})\]\]$`（`g3ku/stickers/marker.py:38-42` 旁）；解析顺序 id→**index**→category→short。`MARKER_RE` 锚定两段，三段数字不冲突。
- `_pick_by_index`：查 registry → 找 `<NNN>.*` → 文件存在且 sha 符 → 发送；否则整行吞掉 + `[表情包未发出：…]` + `sticker_send_failed` 审计（复用 `marker.py:210-219` 既有车道）。
- 删除 `ID_MARKER_RE` / `_pick_by_id` / id 分支。

## 5. 导入（两阶段，只认分类形状）

1. **detect**：源 = `<源>/<子目录>/<图片>`；AstrBot 包把 `memes/` 当源根即可。**不做** manifest 解析、previews、semantic_metadata、flat→`_review`、`--classify-map`、theme_samples。返回：候选分类、各几张、与已有包/分类的重名情况、超 36 的项。
2. **commit**：调用方给 `{源目录: (目标分类, 描述)}` 映射 → 建分类、铸号、写 registry 与 manifest。**映射缺失的目标分类不写入**；将使分类超 36 的映射拒绝并列出。内容 sha256 去重仅在注册时用于跳过同类别相同字节，**不落盘**。
- 幂等：同源重跑 0 新增。

## 6. 工具与渲染

- `tools/show_sticker_category/`：`build()` 恒注册（无开关）。`execute(pack, category)`：
  - 非多模态 → 提醒文本（§1-5）；
  - 分类 0 张已注册 → 文本说明；1 张 → 文本"直接用类别语法"；
  - 否则渲染/命中缓存 → 返回 `content_open` 同形状 payload（`{'type':'image_url',…}` 由装配层生成，`react_loop.py:9384-9390`）+ `how_to_send` + `overflow` + `registered_count`。
- 渲染规格见 §2 配方；cap `MAX_IMAGES_PER_CATEGORY = 36`；缓存键 = hash(已注册列表 + 渲染规格版本)。
- 删除 `tools/search_stickers/`、`g3ku/agent/tools/sticker_search.py`、`g3ku/stickers/semantic.py`、`skills/meme/scripts/caption_pack.py`、`references/caption-policy.md`、`semantic_search_enabled()`（`g3ku/stickers/config.py:26-37`）与配置项 `stickers.semantic_search`、UI 精确取图开关（`org_graph_stickers.js:545-546` 及设置区 DOM）。

## 7. 分期与落点

**P0 拆除与提醒替换**
- 删 §6 删除清单全部；删 `marker.py` id 分支。
- `g3ku/stickers/catalog.py:255/280-283`：删 `STICKER_SECTION_BUDGET` 全有全无；`_build_sections` 恒带描述。
- 新增 `STICKER_INJECTED_CHAR_WARN = 16000`；`GET /resources/stickers/packs`（`main/api/admin_rest.py:2114`）payload 加 `injected_chars / injected_limit / over_injected_limit`（度量 = `render_catalog_lines`（`catalog.py:286`）拼接长度）。
- 前端表情包页顶部 banner（仅 over 时）："当前注入表情包有点多，需要适当停用"。只提醒，不删不截。
- 描述截断位置修正（实测：30 分类总长 689 字符、最长 37、平均 23；四面齐写的富描述构造极限 61–71）：**渲染侧不截断**（`catalog.py:265` 的 `_flatten(category.description, 20)` 改为只压空白不砍长度）；**写入侧上限 = 单一常数 `CATEGORY_DESCRIPTION_MAX_CHARS` = 120（已定案，不提升到 300）**。该常数由**单一清洗函数**强制，三条写路径共用：导入 commit、admin API/UI、注册工具——导入 commit 直接写 manifest、绕过 admin 闸，上游 AstrBot 协议允许描述到 2000 字（`skills/meme/references/pack-format.md:31`），故导入超限 = 截断 + 报告行列出，不静默。聚合护栏仍是 16000 提醒（只提醒不拦）。导入超长描述的一次性浓缩留作 P3 可选项，默认不建。

**P1 注册制与索引解析**
- 新增 `g3ku/stickers/registry.py`（读写/铸号/sha 校验/36 上限/bootstrap 迁移）。
- `marker.py` 索引语法 + `_pick_by_index`；类别随机车道改只抽已注册。
- `admin.py`：`store_uploads`（:480）改走注册；新增 `register_pending(pack, category?)`；`scan_admin_packs` 暴露未注册列表与 overflow。
- 路由：`POST /resources/stickers/packs`（建包）、`POST /resources/stickers/packs/{pack}/register`。

**P2 渲染器与工具 + 门槛验收**
- 新增 `g3ku/stickers/sheet.py` + `tools/show_sticker_category/` + 非多模态提醒。
- 36 格准确率已有实测基线（弱模型 5/5，`temp/sheet-probe/reply-36-sv.txt`），本阶段不另设门槛；会话 overlay E2E 在 P5 统一做。

**P3 导入两阶段化 + skill 重写**
- `skills/meme/scripts/import_pack.py` 678→约 300 行（§5）；`skills/meme/SKILL.md` 重写：**写明 36 上限**、**注册制（放文件不生效，须工具或界面注册）**、两阶段导入、类别车道兜底。

**P4 UI**
- 建包、多选上传（走注册）、未注册列表 + 注册按钮、overflow 标记、16000 banner。
- 启停改造：删 `admin.py:269` 排序键里的 `not item.enabled`（包级同改，只按目录名/key）；删 `org_graph_stickers.js:114/143/185` 的 `status-disabled` 徽标与 `:144-146` 停用提示段；保留 `:155/:202` 原地按钮文案互换与 `:193` 卡片降饱和；**不新增状态元素、不移动卡片**。

**P5 文档、测试、运行时验收**
- `docs/architecture/web-and-admin.md`、`runtime-overview.md`、`external-agent-api.md`、`config-and-models.md` 同步（真相分层、索引语法、16000 提醒、36 上限与描述 40 截断）。
- 测试：registry/铸号/迁移幂等、索引解析 fail-closed、渲染缓存键、导入两阶段、admin API、前端 store；删 `tests/resources/test_sticker_semantic.py`。
- 运行时验收（重启 + 真会话，单测不算）：索引发送成功一张；非多模态会话验证提醒与类别兜底；迁移后序号连续；UI 启停前后对同一卡片量 `getBoundingClientRect` 位置不变（实测基线验收）。
- 补做此前因网关 429 未完成的**会话 overlay E2E**（content_open → 当跳注入 → 索引发送）。

## 8. 风险与回退

| 风险 | 处置 |
|---|---|
| 存量分类 >36 时迁移只注册前 36 | 超额留在未注册列表（显式可见），UI 提供拆分/注册操作；不静默丢弃 |
| 注册制漏注册 = 图在目录却不可见 | UI 未注册列表 + 工具 pending 双显式；bootstrap 迁移幂等且有验收 |
| 16000 只提醒不拦 → 钉住头可无限增长 | 运营护栏写进文档；banner 文案引导停用 |
| 非多模态部署精确车道不可用 | 类别车道兜底（§1-5） |
| 网关 tpm 429（实测发生过） | 验收错峰或依赖 retry；拼图 ≤384KB/张 |
| 动图取首帧丢失动态笑点 | 已知损失，写进 SKILL.md |

## 9. 交接

- 探针产物 `D:/Project/Negi/temp/sheet-probe/`（gitignore 内）：渲染器/探针/评分脚本/拼图/回复；P5 验收后删除。
- 共享工作树纪律：只 `git add` 本计划涉及文件；不碰 `work/sandbox-trust-plan.md`；提交用 `git -c user.name="ZXY39" -c user.email="1045273950@qq.com"`。
- 文件写入一律 Edit/Write，禁 shell heredoc（中文 GBK 事故既往）。
- 本计划确认后才动代码；每批完成即 commit。

---

## 10. 落地记录（2026-10-27）

分支：`main`（`feat/sandbox-trust` 已合入并删除，另合并了远端 `6b56ea47`）。

| 阶段 | 提交 | 内容 |
|---|---|---|
| P0 | `52fe45c7` | 拆语义层（semantic/sticker_search/caption 全删，−578 行）；删 `semantic_search` 配置与 UI 开关；删 2000 全有全无；描述渲染不截断；写入 120 单一清洗函数；16000 口提醒 |
| P1 | `80ba1938` | `registry.py`（可见性真相、铸号 append-only、36 上限、幂等 bootstrap、符号链接越界防护）；索引标记 + fail-closed 解析；随机车道只抽已注册；建包/注册路由；启停不排序 |
| P2 | `90fa135e` | `sheet.py` 渲染器（tile200/crop+fit/letterbox/徽标/≤36/1256²/内容哈希缓存）+ `show_sticker_category`；沙箱白名单 |
| P3 | `f1fc6d4b` | 导入两阶段（`--detect` / `--commit --map`，只收分类形状）；`register_stickers` 工具；SKILL/pack-format 重写 |
| P4 | `367e44bc` | 建包、注册待处理、未注册缩略图、16000 banner；删已停用徽标与提示段 |
| 文档 | `15f5c810` | 四份 architecture 同步 |
| 修复 | `5c3a79e6` | 工具 build(runtime) 收到 SimpleNamespace 被 dict() 打死 → repair 态；改兼容两种形状 |

### 与计划的偏差

1. **迁移期发现并修掉一个铸号 bug**：`_INDEX_RE` 原写 `\d{3,}`，把社区包常见的 13 位时间戳文件名当成"已编号"保留，索引超出标记语法 1–3 位而不可发送。改为恰好三位；真实库重跑后 138 张全部注册（86 可见 + 52 隐身）、编号三位。
2. `write_registry` 对 Windows `WinError 5` 短暂重试（AV/索引器持有句柄，实测必现一次）。
3. 删除了已无消费者的 `catalog.pack_images()`，把它的符号链接越界判据搬进 `registry._inside()`。
4. 分类启用 >36 的拒绝规则没有单独实现：注册上限 36 已使其结构上不可能。
5. 描述上限落地为 **120**（用户定案，不提升 300）。

### 验收状态

| 项 | 结果 |
|---|---|
| Python 贴纸测试（packs/admin/api/registry/sheet/import） | 117 passed, 2 skipped |
| 前端 JS 测试 | 773/775；2 项失败经 detached worktree 复核为**基线既有**（`bindStickerPage is not defined`，模型路由测试） |
| ruff | 通过 |
| 服务重启 + 资源加载 | 35 tools；`show_sticker_category` / `register_stickers` 均 available+callable |
| API 契约 | settings 返回 `description_max_chars=120 / injected_chars=819 / limit=16000 / over=false`，无 `semantic_search`；packs 返回 `pending/registered/over_limit` |
| 界面（浏览器） | 3 包无"已停用"徽标；隐身分类按 key 原位穿插不重排；每分类「N / N 张已注册」；描述全量（含 37 字长描述）；新建包表单在；发送策略只剩「每轮最多」；精确取图开关消失 |
| 真实库渲染 | happy 9 格、sad 8 格、work 3 格、_laugh 12 格均正确（横幅 1–9，小 emoji 裁剪后撑满） |
| **会话端到端（拼图→序号发送）** | **通过**。远端 `8589d399`（合并为 `ac313c36`）修掉了"批次可执行清单跨跳"导致的 `missing merged tool result`。实测：模型调用 `show_sticker_category` 后**逐格描述了整张拼图**（1 拿羽毛掸子的鲸鱼娘 / 2 游泳圈 / 3 粉色小人 / 8 女仆趴在黑发角色上 / 9 张嘴大笑的黄色生物，与真实拼图一致），选 5 号写出 `[[sticker:deepseek-girl-pack/happy/5]]`；出站解析为 `happy/005.gif` 附件，转录中标记被替换为图片，记忆复核载荷尾部带 `[已发表情包：deepseek-girl-pack/happy/5]`，无新增 `sticker_send_failed` |

### 交接

- 曾阻塞的会话 E2E 已由远端 `8589d399`（`fix(frontdoor): scope tool-batch executable list to one hop`）修复并合并；验收已通过，无需再动运行时。
- 探针产物 `temp/sheet-probe/` 仍在（gitignore），确认后可删。
- 仓库自身记忆 `memory/MEMORY.md` 里仍留着 `search_stickers`/`caption_pack` 的旧教训条目，是否清理由用户决定（属 agent 记忆，不擅自改）。
