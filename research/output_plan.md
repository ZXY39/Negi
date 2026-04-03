# 泛二次元女性角色 Top 20 生成方案

> 版本：1.0  
> 更新日期：2026-02-11  
> 核心需求：产出一份基于全球综合热度的过去一年泛二次元女性角色 Top 20 榜单及可交付 Excel。

---

## 一、Excel 文件结构设计

### 1.1 工作表概览

| 工作表名 | 用途 | 行数 |
|---------|------|------|
| Top20_榜单 | 核心榜单数据 | 21（含标题行） |
| 数据来源 | 各数据源说明与采集时间 | 若干 |
| 图片索引 | 图片文件与角色的对应关系 | 20 |

### 1.2 Top20_榜单 — 列设计

| 列号 | 列名（中文） | 列名（代码标识） | 宽度（px ≈ 字符×8） | 必填 | 说明 |
|-----|------------|---------------|-------------------|------|------|
| A | 排名 | `rank` | 9 (60) | ✅ | 1~20，整数 |
| B | 中文名 | `name_zh` | 22 (160) | ✅ | 官方中文译名或通用译名 |
| C | 英文名/罗马音 | `name_en` | 28 (200) | ✅ | 罗马音优先；如有通用英文名则补充 |
| D | 日文名 | `name_jp` | 22 (160) | ✅ | 平假名/片假名/汉字原文 |
| E | 来源作品/IP | `source_ip` | 30 (220) | ✅ | 动画/游戏/漫画名 |
| F | 人设简介 | `character_bio` | 42 (300) | ✅ | 80字以内角色描述（性格+定位） |
| G | 日语配音（CV） | `cv_jp` | 22 (160) | ✅ | 声优名；无则填"无" |
| H | 代表性名台词（日） | `quote_jp` | 40 (280) | ⬜ | 最具辨识度的台词 |
| I | 代表性名台词（中） | `quote_zh` | 40 (280) | ⬜ | 中文翻译 |
| J | 代表性名台词（英） | `quote_en` | 40 (280) | ⬜ | 英文翻译 |
| K | 排名理由/热度证据 | `rank_reason` | 50 (360) | ✅ | 30-60字摘要：搜索量/二创数/社区投票/社交媒体提及 |
| L | 图片来源URL | `image_url` | 40 (280) | ✅ | 图片的直链或本地相对路径 |
| M | 图片来源备注 | `image_note` | 30 (220) | ✅ | "官方海报""动画截图""粉丝艺术（已授权）"等 |
| N | 角色图片（嵌入） | `image_embedded` | — | ✅ | 使用 `add_image` 嵌入 |

### 1.3 图片列布局设计

```
+----+-----+------+-----+  视觉布局
| N  | A   | B         |  → N列（约32行高）放置角色图片
+----+-----+------+-----+     与 A(排名)+B(中文名) 同行
| 🖼️  | 1   | 玛奇玛      |     图片尺寸建议宽200px × 高280px
+----+-----+------+-----+
```

**行高设置**：每个角色数据行设置为 **200像素（约150行高，单位 1行≈1.33px）**，以确保图片有足够空间展示。

### 1.4 使用 openpyxl 嵌入图片的方法

```python
from openpyxl import Workbook
from openpyxl.drawing.image import Image as OpenpyxlImage
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side

wb = Workbook()
ws = wb.active
ws.title = "Top20_榜单"

# 1) 加载图片对象
img = OpenpyxlImage("path/to/character_01.jpg")

# 2) 设置图片尺寸（像素）
img.width = 120       # 宽度（Excel 单位接近像素）
img.height = 160      # 高度

# 3) 添加到指定单元格（锚定在 N 列）
# openpyxl 图片锚定在单元格左上角，会覆盖后续单元格
ws.add_image(img, "N2")  # 第2行 = 排名1 的数据行

# 4) 设置行高以容纳图片
ws.row_dimensions[2].height = 120  # Excel 行高单位

# 5) 保存
wb.save("output.xlsx")
```

**注意事项**：
- 图片格式支持：PNG, JPEG, GIF, BMP
- 图片锚定：`ws.add_image(img, "N2")` 将图片左上角锚定在单元格 N 的顶部
- 如果图片路径无效，`openpyxl` 会抛出异常，建议加 try/except 兜底
- 列宽不足以完全展示时，Excel 打开仍能查看图片（图片是浮动层）

---

## 二、文字总结文档结构（Markdown）

### 文件路径
`C:\Users\ZGASQ\Desktop\泛二次元女性角色Top20_文字总结.md`

### 2.1 文档大纲

```markdown
# 泛二次元女性角色 Top 20 分析报告（2025-2026）

## 一、概述
- 2025-2026年度泛二次元女性角色热度整体趋势
- 关键数据指标总结（搜索量、二创增长率、社群活跃度）
- Top 20 门槛值与头部集中度

## 二、Top 20 榜单速览
- 表格：排名 / 角色名 / 来源 IP / 热度指数
- 冠军角色深度解读（1-3段）
- 前十名与后十名的分野

## 三、IP 分布分析
- Top 20 所归属 IP 的数量与集中度
- 单 IP 多角色上榜情况（如某作品占 3+ 席位）
- 新番 IP 与经典长青 IP 的比例
- 商业运营对角色热度的影响

## 四、作品类型分布
- 动画 vs 游戏 vs 漫画 vs 轻小说 vs Vtuber/虚拟主播
- 各类型占比饼图/表格
- 类型趋势解读（如"手游角色占比上升"）

## 五、地区/平台传播特征
- 中日韩欧美热度差异
- Twitter/X、Pixiv、YouTube、Bilibili、TikTok 等平台传播特征
- 中文圈 vs 日文圈的偏好差异
- 跨平台破圈效应分析

## 六、为什么这些角色进入前 20？
- 共性特征拆解（人设属性、叙事位置、声优加成、商业推广力度）
- 2025-2026 年度事件驱动（新番播出、游戏开服、周年庆）
- 情感共鸣与时代心理投射

## 七、争议角色分析
- 榜单中引发讨论/质疑的角色（2-4个案例）
- 争议维度：数据来源偏差、饭圈效应、短期热度 vs 长期人气
- 如何客观理解"热度"与"经典角色"的区别

## 八、方法论与局限
- 数据采集框架概述
- 多维度权重体系说明
- 局限性声明（语言覆盖、平台 API 限制、时间窗口等）
- 方法论附录链接

---
*报告生成日期：2026-02-11*  
*数据来源：详见《数据来源与方法说明》*
```

---

## 三、Python 生成脚本框架

### 3.1 脚本文件路径
`D:\NewProjects\G3KU\research\generate_top20.py`

### 3.2 模块架构

```
generate_top20.py
├── CONFIG          # 全局配置（路径、列宽、行高、图片尺寸、样式）
├── DataModels      # TypedDict / dataclass 定义角色数据结构
├── DataLoaders     # 从 JSON/CSV 加载 Top 20 数据
├── ExcelBuilder    # Excel 生成核心类
│   ├── create_header()        # 写入标题行 + 样式
│   ├── write_row()            # 写入单行数据
│   ├── embed_image()          # 嵌入图片到 N 列
│   ├── set_column_widths()    # 设置所有列宽
│   ├── set_row_heights()      # 设置全部数据行行高
│   └── format_cells()         # 文本对齐、字体、冻结窗格等
├── SummaryWriter   # 生成 Markdown 文字总结
└── main()          # 入口：加载数据 → 生成 Excel → 生成 Markdown
```

### 3.3 关键 API 调用方式

#### 列宽设置
```python
# 方式 1：逐个设置
ws.column_dimensions["A"].width = 8
ws.column_dimensions["B"].width = 22
# ...

# 方式 2：批量字典
COLUMN_WIDTHS = {
    "A": 8, "B": 22, "C": 28, "D": 22, "E": 30,
    "F": 42, "G": 22, "H": 40, "I": 40, "J": 40,
    "K": 50, "L": 40, "M": 30, "N": 20,
}
for col_letter, width in COLUMN_WIDTHS.items():
    ws.column_dimensions[col_letter].width = width
```

#### 行高设置
```python
# 标题行
ws.row_dimensions[1].height = 30
# 数据行（2~21行）
for row_num in range(2, 22):
    ws.row_dimensions[row_num].height = 130  # 容纳图片
```

#### 图片嵌入
```python
import os
from pathlib import Path
from openpyxl.drawing.image import Image as OpenpyxlImage

def embed_image(ws, image_path: str, cell_ref: str, width: int = 120, height: int = 160):
    """安全地嵌入图片，路径不存在时跳过"""
    if not os.path.exists(image_path):
        ws[cell_ref] = "[图片加载失败]"
        return
    try:
        img = OpenpyxlImage(image_path)
        img.width = width
        img.height = height
        ws.add_image(img, cell_ref)
    except Exception as e:
        ws[cell_ref] = f"[图片错误: {e}]"
```

#### 单元格样式
```python
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

HEADER_FONT = Font(name="微软雅黑", bold=True, size=11, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="2F5496")  # 深蓝色表头
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
DATA_ALIGN = Alignment(horizontal="left", vertical="top", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin")
)

def apply_header_style(ws, row_num: int = 1):
    for cell in ws[row_num]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER
```

### 3.4 依赖安装

```bash
# 使用项目虚拟环境
& 'D:\NewProjects\G3KU\.venv\Scripts\python.exe' -m pip install openpyxl Pillow
```

- `openpyxl`：Excel 读写与图片嵌入
- `Pillow`（PIL）：openpyxl 处理图片的底层依赖

---

## 四、文件输出路径

| 文件 | 绝对路径 | 格式 | 说明 |
|-----|---------|------|------|
| **Excel 榜单** | `C:\Users\ZGASQ\Desktop\泛二次元女性角色Top20_2025-2026.xlsx` | `.xlsx` | 含图片、格式化表格 |
| **文字总结** | `C:\Users\ZGASQ\Desktop\泛二次元女性角色Top20_文字总结.md` | `.md` | Markdown 分析报告 |
| **方法论附录** | `C:\Users\ZGASQ\Desktop\数据来源与方法说明.md` | `.md` | 数据来源、权重计算、局限性 |
| **图片目录**（可选） | `C:\Users\ZGASQ\Desktop\top20_characters\` | 文件夹 | 20张角色图片，命名 `01_角色名.png` 等 |
| **本方案** | `D:\NewProjects\G3KU\research\output_plan.md` | `.md` | 本设计文档 |

---

## 五、数据字段规范

### 5.1 角色数据结构（TypedDict）

```python
from typing import TypedDict, Optional

class CharacterQuote(TypedDict):
    jp: str
    zh: Optional[str]
    en: Optional[str]

class CharacterEntry(TypedDict):
    rank: int                    # 1~20
    name_zh: str                 # 中文名
    name_en: str                 # 英文名/罗马音
    name_jp: str                 # 日文名
    source_ip: str               # 来源作品/IP
    character_bio: str           # 人设简介（≤80字）
    cv_jp: str                   # 日语CV
    quote: Optional[CharacterQuote]  # 名台词（三语）
    rank_reason: str             # 排名理由/热度证据（30~60字）
    image_url: str               # 图片来源URL
    image_note: str              # 图片来源备注
```

### 5.2 格式约束

| 字段 | 约束 |
|-----|------|
| `rank` | 整数 1~20，不可重复 |
| `name_zh` | 不超过 30 字符 |
| `character_bio` | 不超过 80 字符 |
| `rank_reason` | 30~60 字符，必须包含至少一个数据维度 |
| `image_url` | 有效 URL 或本地绝对路径 |
| `image_note` | 标明版权归属或使用声明 |

### 5.3 热度数据维度（用于 rank_reason）

- 🔍 **搜索引擎**：Google Trends、Yahoo! Japan 搜索趋势
- 🎨 **二创数量**：Pixiv 投稿数、Twitter/X 标签使用量
- 📊 **社区投票**：Anime!Anime!、Charapedia 等年度评选
- 🔥 **社交提及**：Twitter/X、微博、Reddit 提及频率
- 📺 **新作影响**：2025-2026 年新播出/新开服带来的热度

---

## 六、脚本执行流程

```
开始
 │
 ├─ 1. 加载配置 & 验证 openpyxl 已安装
 │
 ├─ 2. 读取 Top 20 数据（JSON 格式）
 │     └─ 验证：rank 为 1~20 且不重复
 │
 ├─ 3. 创建 Excel Workbook
 │     ├─ 写入标题行 + 应用样式
 │     ├─ 逐行写入 20 个角色数据
 │     ├─ 逐行嵌入角色图片
 │     ├─ 设置列宽 & 行高
 │     ├─ 冻结首行
 │     └─ 保存到 Desktop
 │
 ├─ 4. 生成 Markdown 文字总结
 │     └─ 保存到 Desktop
 │
 ├─ 5. 生成方法论附录
 │     └─ 保存到 Desktop
 │
 └─ 6. 输出完成日志
      └─ 列出所有已生成文件及路径
```
