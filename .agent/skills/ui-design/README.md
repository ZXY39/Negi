# Claude Code UI/UX 设计插件

全面的 UI/UX 设计插件，涵盖移动端（iOS、Android、React Native）和 Web 应用，包含现代设计模式、无障碍设计和设计系统。

## 功能

### 核心能力

- **设计系统**: 令牌架构、主题化、多品牌系统
- **无障碍设计**: 符合 WCAG 2.2 标准，包容性设计模式
- **响应式设计**: 容器查询、流式布局、断点设置
- **移动端设计**: iOS HIG、Material Design 3、React Native 模式
- **Web 组件**: React/Vue/Svelte 模式、CSS-in-JS
- **交互设计**: 微交互、动作、过渡效果

## 技能 (Skills)

| 技能                         | 描述                                             |
| --------------------------- | ------------------------------------------------ |
| `design-system-patterns`    | 设计令牌、主题化、组件架构                       |
| `accessibility-compliance`  | WCAG 2.2、移动端无障碍、包容性设计               |
| `responsive-design`         | 容器查询、流式布局、断点                         |
| `mobile-ios-design`         | iOS 人机交互指南、SwiftUI 模式                   |
| `mobile-android-design`     | Material Design 3、Jetpack Compose 模式          |
| `react-native-design`       | React Native 样式、导航、动画                    |
| `web-component-design`      | React/Vue/Svelte 组件模式、CSS-in-JS             |
| `interaction-design`        | 微交互、动作设计、过渡                           |
| `visual-design-foundations` | 字体排版、色彩理论、间距、图标                   |

## 代理 (Agents)

| 代理                      | 描述                                             |
| ------------------------- | ------------------------------------------------ |
| `ui-designer`             | 主动式 UI 设计、组件创建、布局优化               |
| `accessibility-expert`    | 无障碍分析、WCAG 合规性、补救措施                 |
| `design-system-architect` | 设计令牌系统、组件库、主题化                     |

## 命令 (Commands)

| 命令                             | 描述                                           |
| -------------------------------- | ---------------------------------------------- |
| `/ui-design:design-review`       | 审查现有 UI 的问题并提供改进建议               |
| `/ui-design:create-component`    | 使用合适的模式引导组件创建                     |
| `/ui-design:accessibility-audit` | 审核 UI 代码的 WCAG 合规性                     |
| `/ui-design:design-system-setup` | 使用令牌初始化设计系统                         |

## 安装

```bash
/plugin install ui-design
```

## 使用示例

### 设计审查

```
/ui-design:design-review --file src/components/Button.tsx
```

### 创建组件

```
/ui-design:create-component Card --platform react
```

### 无障碍审核

```
/ui-design:accessibility-audit --level AA
```

### 设计系统设置

```
/ui-design:design-system-setup --name "Acme 设计系统"
```

## 涵盖的关键技术

### Web

- CSS Grid, Flexbox, 容器查询
- Tailwind CSS, CSS-in-JS (Styled Components, Emotion)
- React, Vue, Svelte 组件模式
- Framer Motion, GSAP 动画

### 移动端

- **iOS**: SwiftUI, UIKit, 人机交互指南 (HIG)
- **Android**: Jetpack Compose, Material Design 3
- **React Native**: StyleSheet, Reanimated, React Navigation

### 设计系统

- 设计令牌 (Style Dictionary, Figma Variables)
- 组件库 (Storybook 文档)
- 多品牌主题化

### 无障碍设计

- WCAG 2.2 AA/AAA 合规性
- ARIA 模式和语义化 HTML
- 屏幕阅读器兼容性
- 键盘导航

## 生成的产物

该插件在 `.ui-design/` 中创建产物：

```
.ui-design/
├── design-system.config.json    # 设计系统配置
├── component_specs/             # 生成的组件规范
├── audit_reports/               # 无障碍审核报告
└── tokens/                      # 生成的设计令牌
```

## 要求

- Claude Code CLI
- Node.js 18+ (用于设计令牌生成)

## 许可

MIT 许可证

