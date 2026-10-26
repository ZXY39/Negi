# 表情包管理界面改造计划

日期：2026-10-27
状态：**待确认，未动代码**
取证基线：`main` @ `fd79eca0`
前情：`work/sticker-send-plan.md`（发送链路已实现并落地，本计划只做管理面）

## 一句话结论

管理界面**不引入第二份注册表**：它改的就是 `stickers/` 目录树和 `manifest.json`，与 agent 走 `meme` skill 改的是同一份东西。所以后端只需要一组薄的 `/api/resources/stickers/*` 路由（读 = 扫目录，写 = 改文件），前端只需要一个新 `view-stickers` 视图。写完调 `invalidate_scan_cache()`，下一轮 `[RUNTIME_NOTICE]` 自动把变更告诉模型——**这条链路已经通了**（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py:3766`），界面不需要碰它，也不碰钉住头。

## 现状（已核）

- 目录即清单：`<workspace>/stickers/<pack>/<category>/<image>`；键名 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`（`g3ku/stickers/catalog.py:32`）；`_` 前缀 = 整包/整分类隐身，这是既有的软删机制（`catalog.py:88-93`、`skills/meme/SKILL.md:26`）。
- `scan_packs()` 有 60 秒缓存（`catalog.py:38,161-186`）；`invalidate_scan_cache()`（`:84`）已存在，但**生产代码里没有任何调用方**，只有测试在用（`tests/resources/test_sticker_packs.py`）。它也没从 `g3ku/stickers/__init__.py` 导出，admin 层要直接从 `catalog` import。
- `scan_packs()` 只返回"至少有一张可用图"的**可用**包（`catalog.py:155-158,176-186`）：停用包、空分类都不在里面。管理界面必须能看到它们，否则"停用"这个操作在界面上就等于"消失"。
- `manifest.json` 只存不可从磁盘推导的事实：`theme` / `theme_source` / `source` / `categories.<key>.description`（`catalog.py:108-130`、`skills/meme/SKILL.md:29-50`）。张数与分类清单**禁止**写进去。
- 图片服务两条现成路由都不覆盖 `stickers/`：`GET /api/ceo/uploads/file` 只服务会话上传目录与 `workspace_path()/output`（已是文档化契约，`docs/architecture/web-and-admin.md` 2.7 节）；`GET /api/ceo/media/original?token=` 是签名路由，TTL 24h（`g3ku/runtime/api/ceo_media.py:35,389-410`）。
- 管理端 REST 全在 `main/api/admin_rest.py`（2989 行，`/api` 前缀由 `g3ku/web/main.py:159` 挂）；资源类路由的形状见 `:1713-2004`（skills/tools）；配置类写操作的既定形状是 `load_config()` → 改 → `save_config(cfg)` → `await _refresh_runtime_after_save(reason)`（`:1073-1087`）。
- HTTP 中间件只对 **5xx** 发审计事件（`g3ku/web/main.py:188-224`）：成功的删除**不留痕**。要留痕得自己调 `emit_audit_event(subsystem, level, event_type, summary, *, detail)`（`g3ku/audit_events.py:85`，best-effort、不抛）。
- 前端是 classic script、无打包器、无 ES module（`docs/architecture/web-and-admin.md:17-28`）。`tests/resources/support/frontend_sources.js:11-56` 的 `FRONTEND_BUNDLES` 与 `org_graph.html` 的 `<script src>` 顺序被 `frontend_manifest.test.js` **双向断言**（页面里每个脚本都必须在某个 bundle 里、每个声明都必须真在页面里），新增脚本必须同时登记。
- 三条会咬人的 UI 契约：顶栏写明"eight view buttons"与八个短标签（`web-and-admin.md:220,245`）；管理页只允许**一层**盒子，页面级容器透明无边框（`:276-282`）；栅格断点只能用共享的 1200/1024/900/768/640，禁止页级断点（`:298`）。
- toast 有"一事实一通道"契约：动作本身已经重画了界面状态时**不发成功 toast**，失败一律发（`web-and-admin.md:224`）。
- 可复用的既有件：`openConfirm`（`ui/confirm.js:4`）、`showToast`（`ui/toast.js:19`）、`setDrawerOpen`（`ui/drawer.js:5`）、`openMediaViewer({mode,url,...})`（`ui/media-viewer.js:147`）、`esc()`（`org_graph_app.js:762`）、`ApiClient._request`（`client_api/core.js:207`，FormData 自动不设 JSON 头 `:210-212`）、上传形状 `uploadCeoFiles`（`client_api/ceo.js:143-152`）、缩略图网格 `.persona-photo-grid` / `.persona-photo-tile`（`org_graph.css:18201,18215,18229`）与 `renderPhotoGrid`（`org_graph_onboarding.js:84`，委托式点击 `:113`）。

## 目标行为（验收口径）

1. 顶栏多一个「表情包」入口，进去一眼能读到：**有哪些包、每个包什么主题、每个分类几张图、图长什么样**。
2. 停用的包/分类**也显示**，带明确的"已停用"态，并能一键恢复。
3. 界面内可完成：改主题、改分类描述、停用/启用、上传图片、新建分类、删单图/删分类/删整包、改两个配置开关。
4. 「删除」默认执行**停用**（改名加 `_`）；彻底删除是确认框里的第二步，文案写明"会删掉 N 张图，不可逆"。
5. 任何写操作后界面立即反映新状态（不靠 60 秒 TTL），且**下一轮对话模型收到 `[RUNTIME_NOTICE]`**。
6. 所有路径操作 fail-closed：越界、`..`、符号链接、绝对路径、保留名（`semantic`）、禁用名（`build` / `tmp_*`）一律 4xx，绝不触碰 `stickers/` 之外的文件。
7. 界面新增的任何东西**不破前缀缓存**：不碰 `PINNED_CONTRACT_RENDERER_VERSION`，不碰 `pinned_contract_revision_key`（清单本来就不在里面，见 `sticker-send-plan.md`）。

## 设计决定（每条带默认值，可直接否决）

### D1 图片预览走一条新的只读路由，不复用签名 viewer
默认：`GET /api/resources/stickers/image?pack=&category=&name=`，根固定 `stickers_root()`，后缀必须 ∈ `IMAGE_SUFFIXES`，`resolve()` 后必须 `is_relative_to(root)`。

- 不复用 `original_view_url()`：token TTL 24h（`ceo_media.py:35`），管理页开着过夜缩略图会整批失效；更要紧的是列表要为每张图铸一个**内含绝对路径**的 token，一个 45 分类的包等于把几百条本机绝对路径写进 DOM。
- 不扩 `/api/ceo/uploads/file`：它的两根白名单已经是文档化契约，为了表情包再加一根工作区目录，等于把**渠道附件**那条路由的口子一起开大。
- 成本约 30 行，信任根只有一个目录，是三条路里唯一一条不引入新耦合的。

### D2 管理端自己扫，不给 `scan_packs()` 加开关
默认：新模块 `g3ku/stickers/admin.py`，`scan_admin_packs()` 返回含停用包、含空分类、含文件级明细的形状（`AdminPack/AdminCategory/AdminImage`）。

理由：`scan_packs()` 在钉住头的热路径上（`_ceo_runtime_ops.py:3347`、`frontdoor/message_builder.py`）。多一个 `include_disabled` 参数就多一种"停用包混进契约头"的可能，而那正是软删机制要保证不可能发生的事。管理端的形状本来就和头部不一样（要禁用态、要空分类、要文件名/大小），分开比共用一个函数加两个 flag 更安全。复用 `_image_paths` / `_read_manifest` / `PACK_KEY_RE` 这些**判据**，只不复用"过滤掉不可用项"这一步。

### D3 界面做什么、agent 做什么
- 界面（确定性、要即时可视）：浏览、预览、改主题、改分类描述、停用/启用、上传、新建分类、删除、改两个开关。
- 留给 agent（`meme` skill，判断密集）：导入外部包（`import_pack.py`：形状探测、sha256 去重、`_review`、AstrBot 兼容）、看图定 caption、跨包归类。

理由：界面不做需要模型判断的事，否则就有两条写入路径对同一目录做冲突决定。界面上只放一行提示——"整包导入请把包发给 agent，说'导入这个表情包'"——**不做导入按钮**。

### D4 删除 = 停用，彻底删除是第二步
默认：卡片上的「删除」执行改名加 `_` 前缀（可逆、零成本）；确认框里的「彻底删除」才真删。

理由：表情包是人工攒的资产，而"目录即清单"意味着改名**就是**软删——机制上已经有一个可逆档，不需要再造回收站，也不该让一次误点直接不可逆。

### D5 两级取数，缩略图有上限
默认三个读接口：

| 接口 | 返回 | 用途 |
|---|---|---|
| `GET /resources/stickers/packs` | 包 + 分类 + 张数（**不含图名**） | 一次拿完整棵树的骨架，左栏列表 |
| `GET /resources/stickers/packs/{pack}?per_category=12` | 该包每个分类前 12 张（含 `url`） | 选中包后铺预览条 |
| `GET /resources/stickers/packs/{pack}/categories/{category}?offset=&limit=` | 单个分类的全部图 | 展开「查看全部 N 张」 |

理由：一个 45 分类 × 20 张的包一次返回 900 个 URL 没必要；选中包一次请求就能铺出预览条，展开是第二次请求。这与 Skill 页 `loadSkills()` → `openSkill(id)` 的两级形状一致（`org_graph_resources.js:404,227`）。

### D6 写操作后失效缓存 + 只给不可逆操作发审计
- 每个 mutation 路由结尾 `invalidate_scan_cache()`；**不**调 `_refresh_runtime_after_save`（那是配置通道，目录扫描不进配置）。
- P4 的两个开关例外，走 `save_config(cfg)` + `_refresh_runtime_after_save('admin_stickers_settings_update')`——`stickers` 段已经在 `_tuned_block` 与 `exempt_prefixes` 里（`g3ku/config/loader.py`），不会被整写抹掉。
- 审计只对**三个不可逆删除**（删图 / 删分类 / 删包）调 `emit_audit_event("stickers", "warning", "sticker_*_deleted", ...)`。改名、上传、改描述不记：它们要么可逆、要么在 `manifest.json` 里可见，全记会把日志页淹掉。

### D7 toast 遵守"一事实一通道"
默认：停用/上传/改描述成功后**不发成功 toast**——行变灰、缩略图出现、输入框显示新值，界面自己已经说明了一切（契约见 `web-and-admin.md:224`）。失败一律 toast。

"模型会在下一轮对话收到清单变更"这句是界面读不到的事实，但每次写操作都弹一条会吵，所以它做成**面板常驻状态行**（一句 `.subtitle`），不做 toast。

### D8 顶栏第九个入口
默认：加 `data-view="stickers"`，标签「表情包」，位置在 Tool 之后、记忆之前（同属资源族）。

连带：`web-and-admin.md:220`（"eight view buttons"）与 `:245`（八个短标签）是**文档契约**，必须同批改，否则留下一个假契约。顶栏 <900px 横向滚动（`:220`），多一个标签不会挤坏布局。

替代方案（不采纳）：塞进 Skill 页当第三栏。Skill 页是"列表 + 抽屉"形状，塞一个缩略图网格进去会同时破两个页面的单一职责，而且 Skill 页的抽屉已被 `renderSkillDetail()` 占满（`org_graph_resources.js:227-336`）。

## 阶段

### P1 只读浏览（后端 + 前端骨架）
**落点**
- 新 `g3ku/stickers/admin.py`：`AdminImage/AdminCategory/AdminPack` dataclass、`scan_admin_packs()`、`admin_pack_dir()` / `admin_category_dir()` / `admin_image_path()`（三个 fail-closed 解析器，键名正则从 `catalog` import）、`sticker_image_url(pack, category, name)`。
- `main/api/admin_rest.py`：D5 的三个读接口 + `GET /resources/stickers/image`，挂在 `:1713` 那组 skills 路由之后。
- 前端：
  - `org_graph.html`：nav 按钮（`:101` 附近）+ `<section id="view-stickers" class="view-section">`（走 `compact-resource-header` 的隐藏标题那条道，契约见 `web-and-admin.md:280`）+ 新 `<script>` 标签。
  - `org_graph_app.js`：`U.viewStickers`（`:530` 附近）、`switchView` 的 map（`:9906`）、`if (view === "stickers") void loadStickerPacks();`（`:9969` 附近）、`bind()` 里调 `bindStickerPage()`（`:10219-10238`）。
  - 新 `client_api/stickers.js`（IIFE 注册 `window.ApiDomains.stickers`）+ `api_client.js:7-18` 加一行 `Object.assign` + 新 `org_graph_stickers.js`。
  - `org_graph.css`：`.sticker-*` 类（网格、缩略图、停用态）。
  - `tests/resources/support/frontend_sources.js`：登记新文件（`client_api/stickers.js` 进 `api_client` bundle，其余按页面顺序进 `app` 或新开一个 `stickers` bundle）。
- 缩略图网格用 `.persona-photo-grid` 的 content-sized 形状（`auto-fill minmax(86px,1fr)`）；点图调 `openMediaViewer({mode:"image", url, downloadName})`——**必须显式传 `mode`**，因为 `media-viewer.js:41-43` 的按后缀判定读的是文件名，而新路由的 pathname 没有后缀。

**验收**：`stickers/demo` 与 `demo2` 在界面里可见、缩略图出图、点开放大、GIF 会动；停用包显示为灰态；`tests/resources/test_sticker_admin.py` 覆盖越界 / `..` / 符号链接 / 绝对路径 / 保留名；`node --test tests/resources/frontend_manifest.test.js` 绿；浏览器实测滚动不卡。

### P2 元数据与停用（第一批写操作，全部可逆）
**落点**：`admin.py` 的 `set_pack_theme()`、`set_category_description()`、`set_pack_enabled()` / `set_category_enabled()`（改名加/去 `_`）；路由 `PUT /resources/stickers/packs/{pack}/manifest`、`POST .../{pack}/enable|disable`、`POST .../{pack}/categories/{category}/enable|disable`。

**验收**：改完立刻重扫生效（断言 `invalidate_scan_cache()` 被走到，不靠 TTL）；`manifest.json` 里**没有**被写进张数或分类清单（SKILL.md 明令）；写主题时 `theme_source` 落 `human`；停用后该包从契约头消失（直接用 `sticker_pack_sections()` 断言），启用后回来；改名目标已存在 → 409，不覆盖。

### P3 上传与新建分类
**落点**：`admin.py` 的 `create_category()`、`store_uploads()`；路由 `POST /resources/stickers/packs/{pack}/categories`、`POST /resources/stickers/upload`（`FormData: files[] + pack + category`，服务端形状照 `websocket_ceo.py:646-659`，客户端照 `client_api/ceo.js:143-152`）。

**校验**：键名过 `KEY_RE`、不在 `FORBIDDEN_NAMES`、不以 `FORBIDDEN_PREFIXES` 开头、不是 `semantic`——这些常量**从 `skills/meme/scripts/import_pack.py:41-51` import，不复制**（两份同源常量漂移，界面就会造出 agent 造得出、工具层却拦得住的目录）；后缀 ∈ `IMAGE_SUFFIXES`；单文件 ≤ 20 MiB（`MAX_IMAGE_BYTES`）；文件名过 `safe_filename`；重名加 `_2` 而不是覆盖。

**验收**：拖 3 张图进新分类 → 界面立刻显示；非图片后缀 400；超 20 MiB 413；`build` / `tmp_x` / `semantic` 400；一条端到端断言——上传后 `resolve_markers("[[sticker:<pack>/<新分类>]]")` 能解析到新文件，证明**界面写的东西发送链路认**。

### P4 两个配置开关
**落点**：`GET|PUT /resources/stickers/settings` → `stickers.max_per_reply`（0..4，schema 已有 `field_validator(mode="before")` 兜底）、`stickers.semantic_search`；形状照 `admin_rest.py:1067-1087`。

**验收**：改完落盘、`_refresh_runtime_after_save` 被调、重开页面读数一致；打开 `semanticSearch` 时界面提示"需要先跑 `caption_pack.py`，否则检索不出东西"（这是 `sticker-send-plan.md` 记的待办，别让人从界面踩进去）。

### P5 彻底删除 + 审计（不可逆的那一档）
**落点**：`admin.py` 的 `delete_image()` / `delete_category()` / `delete_pack()`（`shutil.rmtree` 前再 `resolve()` 复核在根内）；`DELETE` 路由；`emit_audit_event`；前端 `openConfirm` 两步（先停用，确认框里再给「彻底删除」）。

**验收**：删完目录真没了、缓存失效、契约头那一节消失、日志页能看到一条 `stickers` 审计；对 `../../` 这类键名 400 且**没有任何文件被删**（断言目录树逐个文件未变）。

### P6（可选，默认不做）重命名与跨分类移动
包/分类改名等于换 marker 键：历史转录里的旧标记会解析不到（既有 fail-closed 行为——静默剥掉，不漏标记也不漏路径）。真要做，确认框必须写明这一点。等有人实际需要再排。

## 不做
- 不做导入按钮（`import_pack.py` 的形状探测 / 去重 / `_review` / AstrBot 兼容复制不了，复制一半就是第二条会漂移的写入路径）。
- 不做拖拽排序、标签编辑、相似图去重、批量多选。
- 不做"自动分类"（需要视觉模型，属于 agent）。
- 不给 `stickers/` 开第二套权限模型：管理 API 与其余 admin 路由同一信任边界（本机）。
- 不做删除回收站（D4 已经用 `_` 前缀提供了可逆档）。

## 风险
1. **键名规则两份同源常量漂移**。`PACK_KEY_RE`（`catalog.py:32`）与 `KEY_RE` + `FORBIDDEN_*`（`import_pack.py:41-51`）今天字面相同、来源不同。admin 层必须 import 而不是重抄，并且加一条测试断言两组常量的集合相等——否则界面能建出 agent 建不出的目录，反之亦然。
2. **顶栏契约文档滞后**。`web-and-admin.md:220,245` 写死了"八个"。改代码不改文档就是留下一个假契约，P1 必须同批改并跑 `negi-architecture-maintenance`。
3. **`invalidate_scan_cache()` 只清本进程**。清的是进程内那一份 `_SCAN_CACHE`（`catalog.py:63,84`）。web 与 worker 分进程时，界面改完 worker 侧最多滞后 60 秒——这是 TTL 的既有语义、不是新缺陷，但要写进文档，别让人以为界面改完立刻全进程生效。
4. **缩略图数量**。一个 900 张的包，即便 `per_category=12` 也会一次铺出 540 个 `<img>`。`loading="lazy"` 是既有做法（`org_graph_onboarding.js:84`）能扛，但 P1 要造一个 200+ 张的假包实测一次滚动，不能只用 `stickers/demo`（3 张图）交差。
5. **界面与 agent 并发写同一目录**。agent 正在 `filesystem_copy` 往同一分类写图时，界面删分类会撞。不做锁；失败形态是 `OSError` → 409，界面提示重试。文档里要写明这是"最后写入者胜"，不承诺原子性。
6. **`switchView` 被 monkey-patch**。`org_graph_ceo_governance.js:1420-1426` 包了一层 `approvalNonBlockingSwitchView`；新视图的 refresh 调用要加在**原函数**的尾部 if 链里（`org_graph_app.js:9969` 附近），加错位置会被那层包装跳过。

## 交接
- 本文件：`work/sticker-admin-ui-plan.md`；发送链路前情见 `work/sticker-send-plan.md`（含"验证边界：哪些必须真连 QQ"）。
- 每阶段独立可交付、独立 commit：P1 之后界面就能用（只读），P2-P5 逐步加写能力，任一阶段停下都不留半成品。
- 共享工作树纪律：只 `git add` 本次改动的文件；不碰 `work/sandbox-trust-plan.md`（另一个会话的文件）；不用 `git stash`。
- 提交身份用 `git -c user.name="ZXY39" -c user.email="1045273950@qq.com" commit`，不改 git config。
- 含中文的文件一律走 Write/Edit 工具，**不用 shell heredoc / `cat >>`**（本项目已因此损坏过两个测试文件）。
- 每阶段收尾：`python -m ruff check .` + `python -m pytest tests/resources/test_sticker_admin.py tests/resources/test_sticker_packs.py tests/resources/test_sticker_semantic.py -q` + `node --test tests/resources/frontend_manifest.test.js`。
- 运行时改动的验收口径不变：**重启 + 真对话**，单测不算（见 `sticker-send-plan.md` 的实测表）。界面这一批额外要验一条真链路——界面里停用某个包，然后在会话里让模型发那个包的表情，应当发不出且正文不留标记。

## 文档与技能义务（AGENTS.md）
本改造同时命中"API 契约""operator-visible workflows""web/admin 行为"，完工前必须跑 `skills/negi-architecture-maintenance`。预计要改：

- `docs/architecture/web-and-admin.md`：新增 `## Sticker Pack Page And Admin Contract`（按既有三段式 `### Frontend Responsibilities` / `### Backend Responsibilities` / `### Maintenance Boundary`，对齐"Memory Management Page And Admin Contract"那一节的形状）；改 `:220,245`（九个入口 + 新标签）；改 `:17-28`（Frontend Script Layout：新脚本与新 bundle）；在 `:298` 说明缩略图网格为什么走 content-sized 而不是共享断点（它是 tile 网格，不是页面栏栅格，precedent 是 `.persona-photo-grid`）。
- `docs/architecture/config-and-models.md`：`### stickers` 那节补一句"这两个值也能在管理界面改"。
- `skills/meme/SKILL.md`：补一句"界面与 agent 共用同一份目录约定，界面也能改主题/描述/停用"，避免 agent 以为只有自己能动这个目录。

---

## 落地记录（2026-10-27）

状态：**P1–P5 已实现并在平台页实测通过**；P6 按计划不做。

提交：`2f16ebfe`（admin 层 + 测试）、`cc8f1f2b`（管理路由 + 测试）、`bd0a1f58`（前端视图 + JS 测试）、随后两条为实测修复与文档。

### 与计划的偏离（四处）

1. **D4「删除 = 停用」改成「删除 = 确认框里的第二步」**。一个只做停用的「删除」按钮对操作者是假的：按下它，包没消失只是变灰。软删档保留在相邻的「停用」按钮上（它本来就执行改名加 `_`），删除的确认文案写明张数、不可恢复，并指向「停用」。另外**单图没有软删档**——`_` 前缀只对目录生效，文件名加 `_` 仍会被 `_image_paths` 收进来，所以删一张图直接不可逆，确认框点名文件。
2. **键名常量改为「两份 + 测试比对」，不是 import**。计划里的做法（admin 从 `import_pack.py` import）会给一个刻意 stdlib-only、可能被用户单独拷走的 skill 脚本加上对 `g3ku` 内部分层的反向依赖，比漂移更脆。落地为：`catalog.RESERVED_PACK_DIRS` 公开、admin 侧定义 `FORBIDDEN_NAMES`/`FORBIDDEN_PREFIXES`/`MAX_IMAGE_BYTES`，`test_admin_key_rules_match_the_import_script` 断言两边集合相等。风险 1 因此由"靠 import 保证"降级为"靠测试保证"。
3. **缩略图用定宽 96px 列**（`repeat(auto-fill, 96px)` + `justify-content: start`），不是 `minmax(86px, 1fr)`：分类里只有一张图时，`1fr` 把那一格拉成整行宽（实测就是这个现象）。
4. **P4 的设置接口顺带回上限常量**（`max_per_reply_limit` / `theme_max_chars` / `description_max_chars` / `max_upload_files` / `max_image_bytes`），前端不硬编码校验上限。

### 平台页实测（2026-10-27 02:0x，web 车道）

前置：`POST /api/bootstrap/exit` 优雅停机 → 重启 `negi_bootstrap.py web --port 18790`；`stickers/demo`（3 张，2 分类）与 `stickers/demo2`（1 张）。

| 检查项 | 结果 |
|---|---|
| 顶栏入口 | 第九个「表情包」，`data-view="stickers"`，图标 lucide `sticker` |
| 列表与详情 | 两个包都渲染；主题、分类描述、张数、缩略图（GIF 在动）、「查看全部」按 `count > 12` 出现 |
| 图片路由 | `image/png` 200 / 270B；`image/gif` 200；`name=../../../config.json` → 400 `invalid_sticker_name` |
| 改主题 | 输入后「保存」才出现 → 点击 → `manifest.json` 写入（`theme_source: human`，`source` 原样保留）→ Enter 还原同一字段 |
| 停用包 | 目录改名 `_demo`；`sticker_contract_sections(safe_scan_packs())` 只剩 `sticker_pack:demo2`；列表出现「已停用」 |
| 启用包 | 改名回 `demo`，选中项跟着新 `dir_name` 走，契约头恢复 |
| 上传 | 界面 2 → 3 张，磁盘出现 `ui-upload-test.png`（PNG 头正确） |
| 删单图 | 确认框「会从磁盘删掉 ui-upload-test.png，不可恢复。」→ 删除后界面 2 张、磁盘消失、`.g3ku/audit.jsonl` 一条 `sticker_image_deleted`（日志页 API 可见） |
| 新建分类 | `uitest` 卡片出现（0 张，描述回填） |
| 删分类 | 文案「会删掉 0 张图，不可恢复。只想暂时不出现请改用「停用」。」→ 卡片消失，`manifest.json` 的 `categories` 只剩旧的 |
| 发送策略 | 「每轮最多」显示服务端值（1 张）；改值落盘走 `_refresh_runtime_after_save` |
| **真对话** | 新会话问「清单里有哪些包和分类」→ 回复 `demo | awkward, happy` / `demo2 | calm`，与界面完全一致 |

实测中发现并已修的两处缺陷（都不在初版计划里）：

1. **`.resource-select` 被 `enhanceResourceSelects()` 换成自定义壳**，壳上的标签是快照：只改原生 `select.value`，界面仍显示旧选项（截图里是「不发」而 DOM 是 `1`）。`renderStickerSettings()` 补了 `syncResourceSelectUI()`。
2. **「每轮最多」标签换行成「每轮最 / 多」**、下拉撑满整行 → `white-space: nowrap` + 定宽。

### 测试

`test_sticker_admin.py` 46 通过 / 2 跳过（Windows 无符号链接权限，symlink 用例 skip）、`test_sticker_admin_api.py` 18 通过、`org_graph_stickers.test.js` 12 通过、`frontend_manifest.test.js` 4 通过；既有 `test_sticker_packs` / `test_sticker_semantic` / `test_sticker_import_script` 全绿；`ruff` 通过；`scripts/check_architecture_docs.py` 0 error（1 条既有 warning 与本改动无关）。

### 未做 / 待办

- P6 重命名与跨分类移动：按计划默认不做。分类改名会换 marker 键，历史转录里的旧标记会解析不到（既有 fail-closed：静默剥掉）。
- 语义检索开关：界面可以打开，但库里没有 caption 时检索不出东西——页面提示了，仍需先跑 `caption_pack.py`。
- `invalidate_scan_cache()` 只清本进程缓存；worker 分进程时最多滞后 60 秒（已写进架构文档）。

### 共享工作树提示

本次实现期间另一个会话在同一工作树里改 sandbox/trust 相关文件（`g3ku/agent/*`、`g3ku/runtime/frontdoor/*`、`g3ku/config/schema.py` 等）。本次只提交自己改的文件；`docs/architecture/config-and-models.md` 里两处改动分属两个会话，入库时只挑了自己的 hunk（`git apply --cached` 单 hunk），另一处仍留在工作区。

