# 洗图技能 (Picture Washing)

此技能仅作为流程编排器使用。请将长 Prompt 保存在参考文件 (refs) 中，并使用 `read_file` 逐步加载。

## 必需输入

- `product_image`: 产品图片 URL / 路径 / base64
- `reference_images`: 一个或多个参考图片
- `ratio`: 输出比例，例如 `1:1`, `3:4`, `4:3`, `16:9`, `9:16`
- `product_description`: 产品卖点和事实
- `amazon_site`: 目标站点，例如 `amazon.com`, `amazon.co.uk`, `amazon.de`

## 可选输入 (必须先进行确认)

- `extra_requirements`: 额外的文案 / 布局 / 颜色约束
- `style_template_images` 或 `style_description`

如果缺失任何必需输入，请立即停止并返回缺失字段列表。

## 渐进式披露顺序 (必须遵守)

1. 读取 `picture-washing/design-specifications.md` 并生成可复用的设计规范。
2. 对于每个 `reference_image`，独立执行以下子步骤：
- 读取 `picture-washing/analyze-composition.md` 以生成参考图构图分析。
- 读取 `picture-washing/merge-prompt.md` 将产品事实 + 设计规范 + 用户需求合并为 Draft Prompt。
- 读取 `picture-washing/compliance-check.md` 以生成最终符合合规要求的生成 Prompt。
- 读取 `picture-washing/picture-washing-skill.md` 并调用 `picture_washing` 工具。

不要在一个步骤中预加载所有参考资料。仅在当前步骤需要时才加载相应文件。

## 执行与失败策略

- 独立处理每个参考图片。
- 不要因为单个图片的错误而停止整个流程；继续处理剩余图片。
- 最终输出必须包含：
- `success_items`: 图片级成功记录，包含最终 Prompt 和返回的 URL。
- `failed_items`: 图片级失败记录，包含失败步骤和标准化的错误。
- `summary`: 总数 / 成功 / 失败 计数以及重试建议。
