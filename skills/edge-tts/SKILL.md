---
name: edge-tts
description: 用 Microsoft Edge 的在线语音合成（edge-tts）把文字生成 MP3 语音文件，供回复、朗读、配音等交付发出。
---

# Edge TTS 在线语音合成

把一段文字生成一个 MP3 语音文件。走微软 Edge 阅读灯的在线服务，**不需要 API key**，需要本机能够访问 `speech.platform.bing.com`。产出的语音是**交付物**，按「文件发送」的 Markdown 语法发出。

这是本运行时的**默认语音合成车道**：用户要语音而没有点名其他合成服务/模型时用它。代价是首次使用要在本机装一条命令（第 1 节），且合成过程联网。

## 1. 先探车道，命中哪条用哪条

```bash
edge-tts --version                              # 1) 已装进 PATH
<当前 python> -m edge_tts --version             # 2) 已装进当前解释器
uv tool run --from edge-tts edge-tts --version  # 3) 隔离运行，不动应用 venv
```

三条都不通才下载（这一步花流量，先跟用户说一句再生成）：

```bash
uv tool install edge-tts
```

- `uv` 不在 PATH 时用绝对路径：Windows `C:\Users\<用户>\.local\bin\uv.exe`，其他系统 `~/.local/bin/uv`。
- 拉包长时间零输出（默认 pypi 直连会静默卡住）时换镜像：给命令加环境变量 `UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/`。
- **不要 `pip install` 进应用自己的 venv**：服务在跑的时候往里写包会留下半截 import 的窗口。

## 2. 生成一条

```bash
edge-tts --voice zh-CN-XiaoxiaoNeural --text "要念的内容" --write-media "C:\绝对\路径\问候.mp3"
```

- 长文本改用 `--file 讲稿.txt`（先用 `filesystem_write` 把稿子落成 txt），不要把整段塞进 `--text`。
- **必须显式给 `--voice`**：不给时默认是 `en-US-EmmaMultilingualNeural`，会用英文腔念中文。
- **负数参数只能写成 `--rate=-10%` 这种连写形式**；`--rate -10%`（带空格）会被 argparse 当成选项而直接报错。语速/音量/音高分别是 `--rate` `--volume` `--pitch`，取值 `+20%`、`-10%`、`+2Hz`。
- **音色名写错不会报"未知音色"**：它返回 `NoAudioReceived`，并把 `--write-media` 指过去的文件写成 **0 字节**。所以发出去之前先看一眼文件字节数，0 字节就是没生成，不要当成功交付。

## 3. 中文音色与性别（本机 `--list-voices` 实抄）

全量 324 个音色，中文这一族共 14 个。选音色按下面的表点名，不要凭印象编名字。

| 音色 | 性别 | 适用 | 特点 |
|---|---|---|---|
| `zh-CN-XiaoxiaoNeural` | 女 | 新闻、小说 | 温暖，中文默认首选 |
| `zh-CN-XiaoyiNeural` | 女 | 卡通、小说 | 活泼 |
| `zh-CN-YunjianNeural` | 男 | 体育、小说 | 热情，解说感 |
| `zh-CN-YunxiNeural` | 男 | 小说 | 阳光，中文男声首选 |
| `zh-CN-YunxiaNeural` | 男 | 卡通、小说 | 少年音 |
| `zh-CN-YunyangNeural` | 男 | 新闻 | 专业、稳重 |
| `zh-CN-liaoning-XiaobeiNeural` | 女 | 东北方言 | 幽默 |
| `zh-CN-shaanxi-XiaoniNeural` | 女 | 陕西方言 | 明亮 |

粤语走 `zh-HK-*`（HiuGaai/HiuMaan 女、WanLung 男），台式国语走 `zh-TW-*`（HsiaoChen/HsiaoYu 女、YunJhe 男）。其他语言现查：`edge-tts --list-voices`。

## 4. 输出格式与落点

- **只有 MP3 一种**：微软侧固定 `audio-24khz-48kbitrate-mono-mp3`（24 kHz / 48 kbps / 单声道），edge-tts 没有格式开关。扩展名一律写 `.mp3`。
- 实测尺寸：24 字约 29 KB（≈5 秒）；同句按 `--rate=-10%` 放慢后 13 字约 21 KB。
- 语音文件要落在**工作区内**：渠道回复只把工作区里的文件当附件发出，工作区外的路径不会被投递。
- 一条回复最多带 **4 个**附件、单个文件 **≤20 MiB**；超出的引用不会变成文件消息，只会留成一条可点击链接文本。

## 5. 发出

一条语音一条语法，文件名写清这是什么：

```markdown
![早间问候.mp3](C:\Users\me\out\早间问候.mp3)
```

正文里给一句这是什么就够了，不要把念的那段文字在正文里再抄一遍。

## 6. 失败时怎么说

| 现象 | 判读 |
|---|---|
| `NoAudioReceived` + 0 字节文件 | 音色名不在 `--list-voices` 里，或参数写法不对（见第 2 节两条） |
| 拉包零输出、卡住 | 默认 pypi 直连不通，换 `UV_DEFAULT_INDEX` 镜像 |
| 连接/超时类报错 | 本机访问不了 `speech.platform.bing.com`（代理或 DNS），这条能力当前不可用 |

任何一种都如实告诉用户"语音没生成"并说明卡在哪一步，然后按用户的决定改发文字；不要静默把语音需求降级成一段文字交付。
