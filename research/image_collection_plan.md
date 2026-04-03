# 角色图片收集方案

> 用于 Top 20 泛二次元女性角色榜单的头像/宣传图收集
> 目标输出目录：`D:\NewProjects\G3KU\research\images\`

---

## 1. 图片获取方案

### 1.1 来源优先级（从高到低）

| 优先级 | 来源 | 说明 | URL 构造策略 |
|--------|------|------|-------------|
| **P0** | 官方 X/Twitter 账号 | 角色公布图、周年纪念图、生日贺图 | 搜索 `"[角色名/作品名]" + "Twitter/X"` → 访问官方账号 → 图片标签页 |
| **P0** | 作品官方网站 | 角色介绍页面、Press Kit | 搜索 `"[作品名] official website" + "character"` → 查找 Character/Staff 页面 |
| **P1** | Anime News Network (ANN) | 作品条目中的角色图片和新闻配图 | `https://www.animenewsnetwork.com/encyclopedia/anime.php?id=[ID]` |
| **P1** | MyAnimeList (MAL) | 作品/角色页面封面和角色头像 | `https://myanimelist.net/character/[ID]/[角色名]` |
| **P2** | AniDB | 角色详情页缩略图与大图 | `https://anidb.net/character/[ID]` |
| **P2** | 萌娘百科 / Wikipedia | 中文/英文百科角色词条信息图 | `https://zh.moegirl.org.cn/[角色名]` |
| **P3** | Pixiv 官方画师 | 作品官方账号发布的授权插图 | 搜索 `"[作品名]" + "Pixiv" + "official/公式"` |

### 1.2 URL 查找策略

**Step 1：确定角色英文名/日文原名**
- 使用 MyAnimeList 搜索框确认标准英文名：`https://myanimelist.net/character.php?q={角色名}`

**Step 2：优先从官方渠道获取**
```
# Google 搜索模板
site:twitter.com "{角色名 OR 作品名}" OR site:x.com "{角色名 OR 作品名}"
"[作品名]" official website character
"[作品名]" press kit character image
```

**Step 3：从 ANN / MAL 获取**
```
# ANN 作品页
https://www.animenewsnetwork.com/search?q={作品名} → 进入作品页 → Character 标签

# MAL 角色页
https://myanimelist.net/character.php?cat=character&q={角色名}
```

**Step 4：从 AniDB 获取**
```
https://anidb.net/character?adb.search={角色名}&do.char=character&do.search=Search
```

**Step 5：萌娘百科 / 百度百科**
```
搜索: "{角色名} 萌娘百科" 或 "{角色名} wikipedia"
```

### 1.3 版权风险与应对策略

| 风险类型 | 说明 | 应对策略 |
|----------|------|----------|
| **版权侵权** | 角色图片通常版权归制作委员会/动画公司/游戏公司所有 | 仅用于调研/报告等内部用途，不商用传播 |
| **Fair Use（合理使用）** | 美国版权法 §107 允许为评论、报道、研究目的有限度使用 | 在 Excel/报告中明确标注图片来源、© 版权方、作品名 |
| **传播限制** | 部分官方角色图禁止二次编辑或再上传 | 嵌入 Excel 时不进行裁剪/调色等实质性修改 |
| **来源标注** | 必须可追溯 | 每张图片在 Excel 中附带 Image URL 和 Source 字段 |
| **删除响应** | 版权方要求删除时需配合 | 保留图片来源记录，收到 DMCA 等通知后 48 小时内替换或删除 |

**最佳实践**：
- 优先使用作品 Press Kit / 宣传素材页面提供的官方素材
- 所有图片来源必须记录在 Excel 的 `Image URL` / `Source` 列
- 不用于盈利性展示、不上传至公共社交平台

---

## 2. 图片尺寸与质量要求

### 2.1 推荐尺寸

| 用途 | 推荐尺寸 | 说明 |
|------|----------|------|
| Excel 嵌入 | **256×256 px** 或 **300×300 px** | 嵌入 Excel 单元格时清晰可辨且不过大 |
| 报告展示 | **500×500 px** | 如需较大显示（如 PDF 报告），可提供高清版 |
| 原始存档 | **原始尺寸** | 下载时保留原始大图，另存一份缩略版供 Excel 使用 |

### 2.2 格式要求

| 格式 | 优先级 | 说明 |
|------|--------|------|
| **PNG** | ★★★★★ | 无损压缩，适合角色线稿、透明背景图片 |
| **JPEG** | ★★★★ | 有损但文件小，适合照片式宣传图、背景复杂图 |
| **WebP** | ★★★ | 如源站点提供可接受，需转换为 PNG/JPEG 后存入 |

**不推荐**：GIF（动图无法在 Excel 中展示静态效果）、BMP（文件过大）、SVG（矢量图在二次元角色场景中较少）

### 2.3 质量验收标准

- **分辨率**：短边不低于 200 px，推荐短边 ≥ 400 px
- **清晰度**：角色面部五官可辨识
- **构图**：角色为主体占画面 ≥ 50%，避免全身远景全身图
- **色彩**：无明显压缩色块、水印遮挡面部

---

## 3. 图片下载与存储策略

### 3.1 存储目录结构

```
D:\NewProjects\G3KU\research\images\
├── {角色名}_{作品名}.png          # 主图（256x256 或 300x300 Excel 用）
├── {角色名}_{作品名}_full.png     # 原始大图存档
└── (如有多张备选) {角色名}_{作品名}_alt1.png
```

### 3.2 文件命名规范

```
格式: {角色名}_{作品名}.{ext}
```

- 角色名：优先使用 **官方英文名**，无英文名时用 **日文罗马字**
- 作品名：使用 **官方英文缩写/简称**（如 JJK 代表 Jujutsu Kaisen）
- 扩展名：`.png` 或 `.jpg` / `.jpeg`
- 字符处理：
  - 空格用 `_` 替代
  - 特殊字符（`/`, `:`, `?`, `"`, `<`, `>`, `|`, `*`, `\`）全部删除
  - 全部转小写

**示例**：
- `marin_kitagawa_mwdk.png`（《更衣人偶坠入爱河》Marin Kitagawa）
- `yor_forger_spyxfamily.png`（《间谍过家家》Yor Forger）
- `mai_sakuraja_bunny_girl_senpai.png`→ `mai_sakurajima_bunny_girl_senpai.png`

### 3.3 下载脚本方案（PowerShell + Python）

#### 方案 A：手动下载

1. 根据 Section 1.2 的 URL 查找策略找到图片 URL
2. 右键 → 图片另存为 → 保存到 `D:\NewProjects\G3KU\research\images\`
3. 按命名规范重命名

#### 方案 B：Python 脚本批量下载（推荐）

```powershell
# 使用项目 Python 环境执行
& 'D:\NewProjects\G3KU\.venv\Scripts\python.exe' download_images.py
```

脚本需要：
- 输入 CSV/Excel 包含角色列表和已找到的 Image URL
- 使用 `requests` 库下载
- 调用 PIL/Pillow 缩放到 256x256
- 自动按命名规范保存
- 跳过/记录 404 或下载失败的条目

### 3.4 图片可用性与清晰度验证

| 验证项 | 方法 | 通过标准 |
|--------|------|----------|
| **URL 可达性** | `requests.get(url, timeout=10)` 返回 200 | HTTP 200 |
| **Content-Type** | 检查 response header 的 `Content-Type` 是否以 `image/` 开头 | 是 |
| **文件大小** | 检查 `len(response.content)` > 5KB | > 5KB |
| **图片可解析** | PIL `Image.open()` 不报异常 | 成功打开 |
| **最小尺寸** | `image.width >= 200 and image.height >= 200` | 满足 |
| **清晰度（可选）** | 使用 OpenCV 计算拉普拉斯方差 > 阈值 | 方差 > 100 |

**验证脚本伪代码**：
```python
from PIL import Image
import requests, io

def validate_image(url, min_size=200, min_bytes=5000):
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200: return False, "HTTP " + str(resp.status_code)
    if len(resp.content) < min_bytes: return False, "Too small"
    img = Image.open(io.BytesIO(resp.content))
    if min(img.size) < min_size: return False, f"Size {img.size}"
    return True, f"OK ({img.size}, {len(resp.content)} bytes)"
```

---

## 4. 备用方案

### 4.1 如果官方大图不可获取

| 场景 | 备用来源 | URL 查找方式 |
|------|----------|-------------|
| 原 X 账号已删除/设为私有 | **作品官方 Facebook 页面** | 搜索 `"[作品名] official Facebook"` |
| 作品官网关闭 | **ANN Encyclopedia 存档** | `https://www.animenewsnetwork.com/encyclopedia/` |
| MAL 缩略图太小 | **AniDB** 或 **TMDB** | AniDB: 搜索角色页的大图；TMDB: 人物页 |
| 动画角色但只有截图 | **动画截图数据库** (screenshots.park) | 搜索 `"[作品名] screenshot" character "{角色名}"` |
| 游戏角色 | **官方游戏 Wiki / Fandom** | 搜索 `"[游戏名] wiki" "{角色名}"` |

### 4.2 完全找不到合适图片的处理

按以下顺序递进处理：

1. **扩大关键词搜索**：尝试日文原名、别名、罗马字变体
2. **使用作品群像图**：如果角色独立图找不到，从作品海报/群像图中裁剪角色部分（标注"取自群像图"）
3. **使用同作品其他角色图替代占位**：标注"图片待确认"

### 4.3 图片缺失标记

Excel 中用以下字段标记：

| Excel 列 | 内容 |
|----------|------|
| `Image URL` | `NOT_FOUND` 或 `N/A - 使用群像图` |
| `Image Status` | `✅ 已获取` / `⚠️ 质量不足` / `❌ 未找到` / `🔲 使用占位图` |
| `Notes` | 说明原因（如"官方图已删除，已用 ANN 存档图替代"） |

---

## 5. 执行清单总结

| 步骤 | 操作 | 工具/平台 | 预计耗时/角色 |
|------|------|-----------|---------------|
| 1 | 确认 Top 20 角色名称 | 现有调研数据 | - |
| 2 | 按优先级查找各角色图片 URL | Google / X / MAL / ANN / AniDB | 3-5 min |
| 3 | 记录 URL 到 CSV | 手动或脚本 | 1 min |
| 4 | 按命名规范下载并缩放 | PowerShell 脚本 / 手动 | 2 min |
| 5 | 验证图片质量 | 脚本 `validate_image()` | 自动 |
| 6 | 更新 Excel 中的 Image URL 和 Status | Excel 编辑 | 1 min |

---

## 附录：角色图片获取速查模板

对每个角色，按以下模板快速查找：

```
角色: {角色名} (英文/日文: {})
作品: {作品名}

[P0] 官方 X:  site:twitter.com "{作品名}" OR site:x.com "{作品名}" character
[P0] 官网:    "{作品名}" official site character
[P1] MAL:     https://myanimelist.net/character.php?q={角色名}
[P1] ANN:     https://www.animenewsnetwork.com/encyclopedia/ -> search {作品名}
[P2] AniDB:   https://anidb.net/character?adb.search={角色名}
[P2] 萌百:    site:moegirl.org.cn "{角色名}"
[P3] Pixiv:   site:pixiv.net "{角色名}" {作品名}

保存路径: D:\NewProjects\G3KU\research\images\{角色名}_{作品名}.png
```
