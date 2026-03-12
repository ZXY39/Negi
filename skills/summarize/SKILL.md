# 文本摘要

快速 CLI 工具，用于总结 URL、本地文件和 YouTube 链接。

## 何时使用 (触发词)

当用户询问以下任何内容时，立即使用此技能：
- “使用 summarize.sh”
- “这个链接/视频讲了什么？”
- “总结这个 URL/文章”
- “转录这个 YouTube/视频” (尽力提取字幕；不需要 `yt-dlp`)

## 快速开始

```bash
summarize "https://example.com" --model google/gemini-3-flash-preview
summarize "/path/to/file.pdf" --model google/gemini-3-flash-preview
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto
```

## YouTube：摘要 vs 转录

尽力提取转录内容 (仅限 URL)：

```bash
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto --extract-only
```

如果用户要求的转录内容非常大，先提供一个精简摘要，然后询问需要展开哪个章节/时间范围。

## 模型与密钥

为您选择的提供商设置 API 密钥：
- OpenAI: `OPENAI_API_KEY`
- Anthropic: `ANTHROPIC_API_KEY`
- xAI: `XAI_API_KEY`
- Google: `GEMINI_API_KEY` (别名: `GOOGLE_GENERATIVE_AI_API_KEY`, `GOOGLE_API_KEY`)

如果未设置，默认模型为 `google/gemini-3-flash-preview`。

## 常用参数

- `--length short|medium|long|xl|xxl|<chars>` (长度)
- `--max-output-tokens <count>` (最大输出 token 数)
- `--extract-only` (仅提取，仅限 URL)
- `--json` (机器可读格式)
- `--firecrawl auto|off|always` (后备提取方案)
- `--youtube auto` (如果设置了 `APIFY_API_TOKEN`，则使用 Apify 后备)

## 配置

可选配置文件：`~/.summarize/config.json`

```json
{ "model": "openai/gpt-5.2" }
```

可选服务：
- `FIRECRAWL_API_KEY` 用于被屏蔽的网站
- `APIFY_API_TOKEN` 用于 YouTube 后备
