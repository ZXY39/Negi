---
name: ui-ux-pro-max
description: UI/UX 设计智能。包含 50 种样式、21 种调色板、50 种字体配对、20 种图表、9 种技术栈。
---
# ui-ux-pro-max

全面的 Web 和移动端应用设计指南。包含 67 种样式、96 种调色板、57 种字体配对、99 条 UX 准则以及涵盖 13 种技术栈的 25 种图表类型。具有基于优先级的推荐逻辑的可搜索数据库。

## 前置条件

检查是否安装了 Python：

```bash
python3 --version || python --version
```

如果未安装 Python，请根据用户操作系统安装：

**macOS:**
```bash
brew install python3
```

**Ubuntu/Debian:**
```bash
sudo apt update && sudo apt install python3
```

**Windows:**
```powershell
winget install Python.Python.3.12
```

---

## 如何使用此 Skill

当用户请求 UI/UX 相关工作（设计、构建、创建、实现、审查、修复、改进）时，请遵循以下流程：

### 步骤 1：分析用户需求

从用户请求中提取关键信息：
- **产品类型**: SaaS、电子商务、作品集、仪表盘、落地页等。
- **样式关键词**: 极简 (minimal)、活泼 (playful)、专业 (professional)、优雅 (elegant)、暗黑模式 (dark mode) 等。
- **行业**: 医疗保健、金融科技、游戏、教育等。
- **技术栈**: React、Vue、Next.js，或默认为 `html-tailwind`

### 步骤 2：生成设计系统 (必须执行)

**始终以 `--design-system` 开始**，以获取包含推理的全面建议：

```bash
python3 skills/ui-ux-pro-max/scripts/search.py "<product_type> <industry> <keywords>" --design-system [-p "项目名称"]
```

此命令将执行：
1. 并行搜索 5 个领域（产品、样式、颜色、落地页、字体）
2. 应用 `ui-reasoning.csv` 中的推理规则来选择最佳匹配
3. 返回完整的设计系统：模式、样式、颜色、字体排版、效果
4. 包含应避免的反向模式 (anti-patterns)

**示例：**
```bash
python3 skills/ui-ux-pro-max/scripts/search.py "beauty spa wellness service" --design-system -p "Serenity Spa"
```

### 步骤 2b：持久化设计系统 (Master + Overrides 模式)

要保存设计系统以便跨会话分层检索，请添加 `--persist`：

```bash
python3 skills/ui-ux-pro-max/scripts/search.py "<query>" --design-system --persist -p "项目名称"
```

这将创建：
- `design-system/MASTER.md` — 包含所有设计规则的全局唯一事实来源
- `design-system/pages/` — 用于存储特定页面覆盖设置的文件夹

**带有特定页面的覆盖：**
```bash
python3 skills/ui-ux-pro-max/scripts/search.py "<query>" --design-system --persist -p "项目名称" --page "dashboard"
```

这还将创建：
- `design-system/pages/dashboard.md` — 该页面相对于 Master 的偏离规则

**分层检索的工作方式：**
1. 构建特定页面（如 "Checkout"）时，首先检查 `design-system/pages/checkout.md`
2. 如果页面文件存在，其规则将 **覆盖** Master 文件
3. 如果不存在，则仅使用 `design-system/MASTER.md`

### 步骤 3：补充详细搜索 (根据需要)

在获取设计系统后，使用领域搜索获取更多细节：

```bash
python3 skills/ui-ux-pro-max/scripts/search.py "<keyword>" --domain <domain> [-n <max_results>]
```

**何时使用详细搜索：**

| 需求 | 领域 (Domain) | 示例 |
|------|--------|---------|
| 更多样式选项 | `style` | `--domain style "glassmorphism dark"` |
| 图表建议 | `chart` | `--domain chart "real-time dashboard"` |
| UX 最佳实践 | `ux` | `--domain ux "animation accessibility"` |
| 备选字体 | `typography` | `--domain typography "elegant luxury"` |
| 落地页结构 | `landing` | `--domain landing "hero social-proof"` |

### 步骤 4：技术栈指南 (默认: html-tailwind)

获取特定实现的最佳实践。如果用户未指定技术栈，则 **默认为 `html-tailwind`**。

```bash
python3 skills/ui-ux-pro-max/scripts/search.py "<keyword>" --stack html-tailwind
```

可用技术栈：`html-tailwind`, `react`, `nextjs`, `vue`, `svelte`, `swiftui`, `react-native`, `flutter`, `shadcn`, `jetpack-compose`

---

## 搜索参考

### 可用领域 (Domains)

| 领域 | 用途 | 示例关键词 |
|--------|---------|------------------|
| `product` | 产品类型建议 | SaaS, e-commerce, portfolio, healthcare, beauty, service |
| `style` | UI 样式、颜色、效果 | glassmorphism, minimalism, dark mode, brutalism |
| `typography` | 字体配对、Google 字体 | elegant, playful, professional, modern |
| `color` | 按产品类型划分的调色板 | saas, ecommerce, healthcare, beauty, fintech, service |
| `landing` | 页面结构、CTA 策略 | hero, hero-centric, testimonial, pricing, social-proof |
| `chart` | 图表类型、库建议 | trend, comparison, timeline, funnel, pie |
| `ux` | 最佳实践、反向模式 | animation, accessibility, z-index, loading |
| `react` | React/Next.js 性能 | waterfall, bundle, suspense, memo, rerender, cache |
| `web` | Web 界面准则 | aria, focus, keyboard, semantic, virtualize |
| `prompt` | AI 提示词、CSS 关键词 | (样式名称) |

### 可用技术栈 (Stacks)

| 技术栈 | 重点 |
|-------|-------|
| `html-tailwind` | Tailwind 工具类、响应式、a11y (默认) |
| `react` | 状态、Hook、性能、模式 |
| `nextjs` | SSR、路由、图像、API 路由 |
| `vue` | Composition API, Pinia, Vue Router |
| `svelte` | Runes, stores, SvelteKit |
| `swiftui` | 视图、状态、导航、动画 |
| `react-native` | 组件、导航、列表 |
| `flutter` | 组件、状态、布局、主题 |
| `shadcn` | shadcn/ui 组件、主题化、表单、模式 |
| `jetpack-compose` | Composables, Modifiers, State Hoisting, Recomposition |

---

## 示例工作流

**用户请求：** "做一个专业护肤服务的落地页"

### 步骤 1：分析需求
- 产品类型: 美容/水疗服务 (Beauty/Spa service)
- 样式关键词: 优雅 (elegant)、专业 (professional)、柔和 (soft)
- 行业: 美容/健康 (Beauty/Wellness)
- 技术栈: html-tailwind (默认)

### 步骤 2：生成设计系统 (必须执行)

```bash
python3 skills/ui-ux-pro-max/scripts/search.py "beauty spa wellness service elegant" --design-system -p "Serenity Spa"
```

**输出：** 包含模式、样式、颜色、字体、效果和反向模式的完整设计系统。

### 步骤 3：补充详细搜索 (根据需要)

```bash
# 获取动画和无障碍设计的 UX 准则
python3 skills/ui-ux-pro-max/scripts/search.py "animation accessibility" --domain ux

# 如果需要，获取备选的排版选项
python3 skills/ui-ux-pro-max/scripts/search.py "elegant luxury serif" --domain typography
```

### 步骤 4：技术栈指南

```bash
python3 skills/ui-ux-pro-max/scripts/search.py "layout responsive form" --stack html-tailwind
```

**最后：** 综合设计系统 + 详细搜索并实现设计。

---

## 输出格式

`--design-system` 标志支持两种输出格式：

```bash
# ASCII 框 (默认) - 最适合终端显示
python3 skills/ui-ux-pro-max/scripts/search.py "fintech crypto" --design-system

# Markdown - 最适合文档记录
python3 skills/ui-ux-pro-max/scripts/search.py "fintech crypto" --design-system -f markdown
```

---

## 获得更好结果的小贴士

1. **关键词要具体** - "healthcare SaaS dashboard" 优于 "app"
2. **搜索多次** - 不同的关键词会揭示不同的见解
3. **组合领域** - 样式 + 字体 + 颜色 = 完整的设计系统
4. **始终检查 UX** - 搜索 "animation"、"z-index"、"accessibility" 以了解常见问题
5. **使用 stack 标志** - 获取特定实现的最佳实践
6. **迭代** - 如果第一次搜索不匹配，请尝试不同的关键词

---

## 专业 UI 的常用规则

这些是经常被忽视的问题，会使 UI 看起来不够专业：

### 图标和视觉元素

| 规则 | 建议做 | 不要做的 |
|------|----|----- |
| **不要使用表情符号图标** | 使用 SVG 图标 (Heroicons, Lucide, Simple Icons) | 使用 🎨 🚀 ⚙️ 等表情符号作为 UI 图标 |
| **稳定的悬停状态** | 悬停时使用颜色/透明度过渡 | 使用会导致布局偏移的比例缩放 (scale) |
| **正确的品牌 Logo** | 从 Simple Icons 研究官方 SVG | 猜测或使用错误的 Logo 路径 |
| **一致的图标大小** | 使用固定的 viewBox (24x24) 以及 w-6 h-6 | 随机混合不同的图标大小 |

### 交互和光标

| 规则 | 建议做 | 不要做的 |
|------|----|----- |
| **指针光标** | 给所有可点击/可悬停的卡片添加 `cursor-pointer` | 在交互元素上保留默认光标 |
| **悬停反馈** | 提供视觉反馈 (颜色、阴影、边框) | 没有迹象表明元素是可交互的 |
| **平滑过渡** | 使用 `transition-colors duration-200` | 瞬时的状态改变或过慢 (>500ms) |

### 亮/暗模式对比度

| 规则 | 建议做 | 不要做的 |
|------|----|----- |
| **亮模式下的玻璃卡片** | 使用 `bg-white/80` 或更高透明度 | 使用 `bg-white/10` (太透明) |
| **亮模式文本对比度** | 正文使用 `#0F172A` (slate-900) | 正文使用 `#94A3B8` (slate-400) |
| **亮模式辅助文本** | 最小使用 `#475569` (slate-600) | 使用 gray-400 或更浅的颜色 |
| **边框可见度** | 亮模式下使用 `border-gray-200` | 使用 `border-white/10` (不可见) |

### 布局和间距

| 规则 | 建议做 | 不要做的 |
|------|----|----- |
| **悬浮导航栏** | 添加 `top-4 left-4 right-4` 间距 | 让导航栏卡死在 `top-0 left-0 right-0` |
| **内容内边距** | 考虑固定导航栏的高度 | 让内容隐藏在固定元素后面 |
| **一致的最大宽度** | 使用相同的 `max-w-6xl` 或 `max-w-7xl` | 混合使用不同的容器宽度 |

---

## 交付前检查清单

在交付 UI 代码之前，请验证以下事项：

### 视觉质量
- [ ] 没有使用表情符号作为图标 (使用 SVG 代替)
- [ ] 所有图标来自一致的图标集 (Heroicons/Lucide)
- [ ] 品牌 Logo 正确 (已从 Simple Icons 验证)
- [ ] 悬停状态不会导致布局偏移
- [ ] 直接使用主题颜色 (bg-primary) 而不是 var() 包裹器

### 交互
- [ ] 所有可点击元素都有 `cursor-pointer`
- [ ] 悬停状态提供清晰的视觉反馈
- [ ] 过渡平滑 (150-300ms)
- [ ] 焦点状态对键盘导航可见

### 亮/暗模式
- [ ] 亮模式文本具有足够的对比度 (最小 4.5:1)
- [ ] 玻璃/透明元素在亮模式下可见
- [ ] 边框在两种模式下都可见
- [ ] 交付前测试两种模式

### 布局
- [ ] 悬浮元素与边缘有适当间距
- [ ] 没有内容隐藏在固定导航栏后面
- [ ] 在 375px, 768px, 1024px, 1440px 下响应正常
- [ ] 移动端没有水平滚动条

### 无障碍
- [ ] 所有图像都有 alt 文本
- [ ] 表单输入有标签
- [ ] 颜色不是唯一的指示方式
- [ ] 尊重 `prefers-reduced-motion`
