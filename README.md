# bili-sub-archive —— B 站 UP 主内容归档工具

**v0.1.1 · Windows 手动运行的 Python 命令行工具 · 2026-09-29**

输入一个 B 站 UID 与自己的 Cookie，把该账号**当前可见**的动态、视频、专栏按条目归档到本地：
动态生成单张长 PNG，专栏转 Markdown，视频下载分 P 并提取文字稿（平台字幕优先、本地 ASR 兜底），
再按需生成逐视频总结与 Mermaid 思维导图。

---

## 快速开始

```powershell
cd E:\bili-sub-archive                              # 换成你的目录
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[ui,media]"                # 归档核心零依赖；这两组只影响终端美化与图片/视频下载

$env:BSA_COOKIE = "SESSDATA=...; bili_jct=...; buvid3=..."

bsa check --uid 1039025435                     # 逐项自检：Python、依赖、配置、登录态
bsa sync --uid 1039025435 --latest 5 --dry-run # 先看会选中哪些条目，不落盘
bsa sync --uid 1039025435 --latest 5           # 真正归档
```

`sync` 一个命令同时承担"首次采集"与"增量重跑"：再跑一次只会补没做完的步骤，
不重复下载、不重复调用模型。失败或有配置变更时，用 `retry` 定向补做（见 5.4）。

装好后会得到 `bsa` 命令（全名 `bili-sub-archive`），等价于 `python -m bili_sub_archive`。

> **改名说明（SubVideo → bili-sub-archive）**
> 本项目原名 **SubVideo**，现名 **bili-sub-archive**，命令是 **`bsa`**。
> 为平滑过渡，这三样旧痕迹仍可用一个版本：旧命令 `subvideo`（会提示改用 `bsa`）、
> 旧环境变量前缀 `SUBVIDEO_*`（作为兜底，优先级低于 `BSA_*`）、产物里
> `<!-- subvideo:summary:begin/end -->` 旧标记（读取兼容，新产物写 `bili-sub-archive`）。
> 唯一不再可用的是 `python -m subvideo`，请改用 `bsa` 或 `python -m bili_sub_archive`。

### 按需跳转

| 我想…… | 看 |
| --- | --- |
| 先跑起来 | 上面的「快速开始」 |
| 知道它做什么、不做什么 | 1 能力与边界 |
| 装依赖 / 排环境问题 | 2 系统要求、3 安装 |
| 配 Cookie、UID、模型、各类开关 | 4 配置 |
| 日常采集 / 补做 / 机读输出 | 5 使用 |
| 产物长什么样 | 6 输出结构 |
| 脚本要判断成败 | 7 退出码 |
| 想知道哪里靠不住 | 8 已知限制、10 故障排查 |
| 改代码 / 跑测试 | 11 自检与验收、12 文档索引 |

### 文档

| 文档 | 用途 |
| --- | --- |
| 本文件 | 安装、配置、使用、已知限制（交付说明） |
| [config.example.toml](config.example.toml) | 配置模板（只含占位符，无凭据；覆盖全部配置键，有测试锁死它不漂移） |
| [prompts/summary.toml](prompts/summary.toml) | 总结 prompt 示例（真 TOML，四个角色可只写一部分） |
| [docs/archive/](docs/archive/README.md) | v0.1.0 开发过程归档：需求、开发方案、逐阶段实施与验证记录、当时的验证工具（**冻结，不再维护**） |

> **它是给个人本地使用的工具。** 不提供公开分享、付费内容破解或登录权限绕过；
> 充电内容只在 Cookie 对应账号本身有权限、且平台实际提供时处理，无权限的条目如实记录原因。
> **不提供本地视频画面 OCR**（决策依据见[组件验证记录](docs/archive/stage0-component-verification.md)第 2.1 节）。

---

## 1. 能力与边界

| 类别 | 做 | 不做 |
| --- | --- | --- |
| 发现 | 动态、视频、专栏三类分页列举；日期范围筛选；三类**合计**取最新 N 条 | 不承诺"指定 UID 下所有充电内容必然完整列出" |
| 动态 | `content.md`、原图 `images/`、单张长 PNG（含 UP 名/头像/时间/正文/配图） | 不采集评论区；不把长图分页 |
| 专栏 | `article.md`（标题/段落/列表/图片顺序）、`images/` | 不渲染网页样式 |
| 视频 | 各分 P 独立下载（并发/续传/校验）、平台字幕、无字幕时可选本地 ASR、合并文字稿 | 不拼接多 P 为单文件；不处理互动视频与直播回放 |
| 总结 | OpenAI 兼容接口，长稿分块不丢尾段，自定义 prompt，模型/prompt 指纹 | 未配置模型时不生成，也不伪造 |
| 导图 | `mindmap.mmd`（确定性序列化）+ `mindmap.png`（mermaid-cli） | PNG 失败不影响 `.mmd` 与总结 |
| 运行 | 手动 `sync` / `retry` / `check`；重复运行按平台 ID 判重、只补未完成步骤 | 不做定时任务；不做 GUI |

---

## 2. 系统要求

| 组件 | 是否必需 | 说明 |
| --- | --- | --- |
| Windows 10/11 + PowerShell | 必需 | 本机实测 Windows + Python 3.14.7 |
| Python **3.11+** | 必需 | 归档核心**只用标准库**，不需要任何第三方包 |
| Pillow | 建议 | 动态长 PNG；缺了 `render` 记 `failed(dependency_missing)`，可装好后 `retry` |
| yt-dlp | 建议 | 视频分 P 下载；缺了 `media` 记 `failed`，元数据与文字稿仍照常 |
| FFmpeg / ffprobe | 建议 | DASH 音视频合流、ASR 提音频、媒体可播放校验；缺失时只能下渐进式单流 |
| Node.js ≥ 18.19 或 ≥ 20 | 可选 | 只有渲染 `mindmap.png` 需要（`mmdc`） |
| mermaid-cli（`mmdc`） | 可选 | 缺了 `.mmd` 照常生成，`mindmap.png` 记 `failed`，装好后 `retry --steps mindmap` |
| faster-whisper | 可选 | 仅在你开启 `--asr` 时需要；**默认关闭，关闭时不下载模型、不跑识别** |
| `openai` SDK | 可选 | 装了走 SDK，没装自动改用标准库直连同一端点，功能等价 |
| rich | 可选 | 终端彩色、面板表格与实时进度；**没装则退化为纯文本**，功能与退出码完全一致（见 5.6） |
| OpenAI 兼容端点 | 可选 | 只有总结/导图需要；不配置则这两步记 `skipped(llm_not_configured)` |

---

## 3. 安装（Windows PowerShell）

### 3.1 取代码并建虚拟环境

```powershell
cd E:\bili-sub-archive                       # 换成你的目录
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

### 3.2 按需安装 Python 依赖

```powershell
pip install -e .                     # 只归档动态/专栏：纯标准库，零依赖
pip install -e ".[media]"            # + yt-dlp、Pillow（视频下载与动态长图）
pip install -e ".[summary]"          # + openai SDK（可选；不装也能用标准库直连）
pip install -e ".[asr]"              # + faster-whisper（仅 --asr 时需要）
pip install -e ".[ui]"               # + rich（终端彩色与实时进度；不装也能用，纯文本）
pip install -e ".[media,summary]"    # 也可以一次装多组
```

> 装了 `openai` SDK 会在环境里多出 `httpx2` 依赖链（见[组件验证记录](docs/archive/stage0-component-verification.md)第 2.3 节）。
> 想保持依赖面最小，就**不要**装 `summary` 组 —— 总结默认用标准库直连。
> 同理，`rich` 自己会带 `markdown-it-py` 与 `pygments`；**不装 `ui` 组**时输出退化为纯文本，
> 归档与总结能力一点不少（见 5.6）。

### 3.3 外部可执行文件（不通过 pip 安装）

```powershell
winget install Gyan.FFmpeg           # ffmpeg + ffprobe（装完重开终端使 PATH 生效）
node --version                       # 需要 v18.19+ 或 v20+
npm install -g @mermaid-js/mermaid-cli
```

安装 mermaid-cli 时 `puppeteer` 会**下载 Chromium**（首次较慢，Windows 上最常见的失败点）。
若公司网络/代理导致失败，可先跳过下载并复用本机 Chrome：

```powershell
$env:PUPPETEER_SKIP_DOWNLOAD = "1"
npm install -g @mermaid-js/mermaid-cli
$env:PUPPETEER_EXECUTABLE_PATH = "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

FFmpeg / mmdc 不在 PATH 时，也可以直接在配置里写绝对路径（见 `[download] ffmpeg_path`、`[mindmap] mmdc_path`）。

### 3.4 验证安装

```powershell
bsa check --uid 1039025435          # 依赖表 + 配置 + 登录态 + 作者信息
bsa check --uid 1039025435 --scan-output   # 额外扫描产物里有无凭据泄漏
bsa --version                       # bili-sub-archive 0.1.1（命令 bsa）
```

`check` 会逐项列出 Python 版本、依赖（含缺失项的**安装命令**）、配置摘要、Cookie 来源、
登录态、作者信息、输出目录可写性，**不发起批量下载**。

---

## 4. 配置

### 4.1 两个配置文件

```powershell
Copy-Item config.example.toml config.toml          # 可入库的常规配置
Copy-Item config.example.toml config.local.toml    # 不入库：Cookie、LLM 密钥、个人路径
```

优先级（后者覆盖前者）：

```text
默认值  <  config.toml  <  config.local.toml  <  环境变量 / 命令行参数
```

`config.example.toml` **只含占位符**，覆盖全部配置键（有测试锁死模板与代码不漂移）。
指定 `--config <文件>` 时只读该文件，不再读默认的两个。

### 4.2 凭据

| 凭据 | 推荐来源 | 也支持 | 绝不 |
| --- | --- | --- | --- |
| Cookie | 环境变量 `BSA_COOKIE` | `config.local.toml` 的 `[account] cookie`、`require.txt` | 写入产物 / 日志 / 异常输出（统一过 `Redactor`） |
| LLM 密钥 | 环境变量 `BSA_LLM_API_KEY` | `config.local.toml` 的 `[summary] api_key` | 同上；本地端点通常不需要密钥 |
| UID | `--uid` | `BSA_UID`、`[account] uid`、`require.txt` 的 `mid` | —— |

```powershell
$env:BSA_COOKIE = "SESSDATA=...; bili_jct=...; buvid3=..."
$env:BSA_LLM_API_KEY = "sk-..."      # 只有云端端点需要
```

密钥也可以直接写进 `config.local.toml`（该文件不入库）：

```toml
[summary]
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
api_key = "sk-..."        # 环境变量 BSA_LLM_API_KEY / OPENAI_API_KEY 优先级更高
```

`config.local.toml`、`require.txt`、`output/`、`.test_tmp/`、`.smoke_sync/` 已在 `.gitignore` 中排除。
`check --scan-output` 会回扫产物，发现任何 Cookie/密钥**值**即以退出码 1 报错。

### 4.3 环境变量一览

| 变量 | 作用 |
| --- | --- |
| `BSA_COOKIE` / `BILI_COOKIE` | Cookie（优先级最高） |
| `BSA_UID` / `BILI_UID` | 目标 UID |
| `BSA_OUTPUT_DIR` | 输出目录（覆盖配置） |
| `BSA_LLM_BASE_URL` / `BSA_LLM_MODEL` | 总结端点与模型 |
| `BSA_LLM_API_KEY` / `OPENAI_API_KEY` | 总结密钥 |
| `BSA_MMDC` | `mmdc` 可执行文件路径 |
| `BSA_REQUEST_INTERVAL` | 请求间隔秒数（取值非法时忽略并告警） |
| `BSA_FSYNC` | 设为 `0` 关闭落盘 fsync（**只用于测试提速**） |

> 旧前缀 `SUBVIDEO_*` 是改名前的名字（SubVideo 时期），仍被接受一个版本：
> 同一个变量同时给了新旧两个名字时，以 `BSA_*` 为准。`BILI_*` / `OPENAI_*`
> 是更早就有的通用兜底，优先级最低。

### 4.4 常改的配置项

| 段 | 键 | 默认 | 说明 |
| --- | --- | --- | --- |
| `[account]` | `uid` | 示例值 | 目标 UP 主 |
| `[output]` | `dir` / `download_images` / `image_workers` | `output` / `true` / `4` | 输出目录与配图并发 |
| `[scope]` | `dynamic` / `video` / `article` | 全 `true` | 三类内容单独开关 |
| `[filter]` | `from` / `to` / `latest` | 空 | 默认筛选（命令行优先） |
| `[request]` | `interval_seconds` | `1.2` | **风控敏感**：实测 0.9~1.2s 较稳，小于 0.5 会告警 |
| `[download]` | `video_workers` / `segment_workers` / `quality` | `3` / `4` / `1080p` | 视频并发与清晰度上限 |
| `[asr]` | `enabled` / `model` | `false` / `small` | 本地转写开关（关闭时不跑识别） |
| `[render]` | `width` / `max_height` | `1080` / `20000` | 动态长图；超过高度上限会**明确告警**，不静默截断 |
| `[summary]` | `enabled` / `base_url` / `model` / `api_key` / `chunk_chars` / `max_chunks` | `true` / 空 / 空 / 空 / `6000` / `40` | 端点留空 = 未配置，记 `skipped(llm_not_configured)`；`api_key` 只填在 `config.local.toml`（或走环境变量），本地端点留空 |
| `[mindmap]` | `enabled` / `max_nodes` / `max_depth` / `label_chars` | `true` / `60` / `3` / `24` | 受控大纲上限 |
| `[storage]` | `lock_stale_hours` | `6` | 上次运行被强杀后写锁可自动抢占的时长 |

全部键与注释见 [config.example.toml](config.example.toml)。

> 模板里的 `[summary] max_tokens` 写的是 **8192**（代码内置默认 `2048`）：推理模型的思考 token
> 也计入这个上限，2048 经常在写完正文前用光，表现为「模型返回内容为空」或摘要断在半句、大纲缺失。
> 用非推理模型时 2048 够用；用推理模型请保持 8192 或更高。

---

## 5. 使用

### 5.1 首次采集 / 增量重跑（同一个命令）

```powershell
# 指定日期范围（北京时间，含当日）
bsa sync --uid 1039025435 --from 2026-01-01 --to 2026-09-23

# 三类合计取最新 20 条
bsa sync --uid 1039025435 --latest 20

# 先看会选中什么（不创建条目目录、不下载；仍会更新 index.json 与 _runs 运行记录）
bsa sync --uid 1039025435 --latest 20 --dry-run

# 再次运行同一命令 = 增量：已完成的步骤跳过，不重复下载、不重复调用模型
bsa sync --uid 1039025435 --latest 20
```

### 5.2 按需开关与调参

```powershell
bsa sync --uid 1039025435 --latest 20 --types dynamic,article   # 只要动态与专栏
bsa sync --uid 1039025435 --latest 20 --no-media                # 不下视频媒体，只要元数据与文字稿
bsa sync --uid 1039025435 --latest 20 --no-images               # 不下载动态/专栏配图原图
bsa sync --uid 1039025435 --latest 20 --no-render               # 不生成动态长 PNG
bsa sync --uid 1039025435 --latest 20 --no-summary --no-mindmap # 不要总结与导图
bsa sync --uid 1039025435 --latest 20 --asr --asr-model small   # 无字幕的分 P 走本地 ASR
bsa sync --uid 1039025435 --latest 20 --asr-language zh         # 指定识别语言（空串 = 自动判断）
bsa sync --uid 1039025435 --latest 20 --video-workers 2 --segment-workers 4 --quality 720p
bsa sync --uid 1039025435 --latest 20 --interval 1.5 --max-pages 20
```

### 5.3 总结与导图

```powershell
$env:BSA_LLM_API_KEY = "sk-..."
bsa sync --uid 1039025435 --latest 5 `
  --summary-base-url https://api.example.com/v1 --summary-model gpt-4o-mini

bsa sync --uid 1039025435 --latest 5 --summary-prompt prompts/summary.toml
bsa sync --uid 1039025435 --latest 5 --mmdc "C:\Users\me\AppData\Roaming\npm\mmdc.cmd"
```

自定义 prompt 两种写法都支持，缺哪节就用哪节的内置默认（也可只写一节）：

```toml
# prompts/summary.toml —— 真 TOML（多行文本用三引号）
system = "你是资深中文内容分析师……"
map    = "第 {index}/{chunks} 部分（分 P：{pages}）：\n{transcript}\n请提取 3~8 条要点……"
reduce = "以下是分段要点：\n{points}\n请输出 ## 摘要 与 ## 大纲……"
full   = "以下是完整文字稿（{title}，{chars} 字）：\n{transcript}\n……"
```

```text
# prompts/summary.txt —— 等价的 INI 风格（任意扩展名）
[system]
你是资深中文内容分析师……
[map]
第 {index}/{chunks} 部分（分 P：{pages}）：{transcript}
```

占位符 `{transcript}` `{points}` `{chars}` `{chunks}` `{pages}` `{title}` `{author}` `{index}`；
`map`/`full` 必须带 `{transcript}`、`reduce` 必须带 `{points}`（缺了直接报配置错误，不会静默发出空内容）。
写完即算"prompt 变更"：输入指纹变化，下次 `sync`/`retry` 会自动重做该条目的总结与导图。
**只发送文字稿**，不发送 Cookie、媒体文件或原视频地址。

### 5.4 补做（失败或被配置变更影响的步骤）

```powershell
bsa retry --uid 1039025435 --steps summary,mindmap   # 只补总结与导图
bsa retry --uid 1039025435 --steps media             # 装好 yt-dlp/FFmpeg 后补下载
bsa retry --uid 1039025435 --steps transcript --asr  # 开启 ASR 后补文字稿
bsa retry --uid 1039025435 --steps mindmap           # 装好 mmdc 后只补渲染
bsa sync  --uid 1039025435 --latest 20 --force       # 忽略状态，强制重跑
```

改 prompt / 换模型 / 换分块参数会改变**输入指纹**，`retry` 会自动把"已完成但已过时"的
总结与导图重新纳入，其余步骤不动；渲染失败只影响 `mindmap`，`.mmd` 与总结原样保留。
配置覆盖类开关（`--asr` / `--no-subtitle` / `--mmdc` / `--summary-*` / `--no-media` 等）
在 `sync` 与 `retry` 上都可用；筛选类开关（`--from` / `--to` / `--latest` / `--types`）只在 `sync` 上，
`--steps` 只在 `retry` 上。

### 5.5 机器可读输出

```powershell
bsa sync --uid 1039025435 --latest 20 --json | Out-File run.json -Encoding utf8
```

`--json`、`-v/--verbose`、`--quiet`、`--config`、`--color`、`--no-progress`
写在子命令**前面或后面**都生效。

运行摘要（成功/部分完成/跳过/权限不足/失败计数、请求数、风控次数、条目级结果与原因）
同时写入 `<作者目录>\_runs\<时间戳>.json`，并回填 `index.json` 的 `last_run`。

### 5.6 终端输出（配色、进度与降级）

默认**什么都不用配**：在交互终端里自动彩色化、画实时进度；一旦把输出重定向到文件或管道，
自动退化为纯文本（可 grep、无 ANSI 转义）。

```powershell
bsa sync --uid 1039025435 --latest 20 --color=never   # 明确不要颜色
bsa sync --uid 1039025435 --latest 20 --color=always  # 强制颜色（含重定向）
bsa sync --uid 1039025435 --latest 20 --no-progress   # 关掉进度条
```

| 开关 / 变量 | 作用 |
| --- | --- |
| `--color=auto`（默认） | 交互终端彩色，重定向/管道无色 |
| `--color=always` | 强制彩色；**不写值**（只写 `--color`）等价于 `always` |
| `--color=never` | 永不输出 ANSI 转义；结果符号退化为 `[ok] / [~] / [=] / [x]` 这类纯 ASCII |
| `--no-progress` | 不画进度条（`--quiet` 也视为关闭） |
| `NO_COLOR` | 环境变量非空即抑制彩色（`--color=always` 优先于它） |
| `CLICOLOR_FORCE` | 环境变量非 `0`/空即强制彩色 |

- **`--color` / `--no-progress` 与 `--json` / `-v` 同规格**：写在子命令前面或后面都生效。
- **两个流分开**：运行摘要与 `check` 报告在 stdout；日志与实时进度在 stderr。
  所以 `bsa sync > out.txt` 时文件里是干净的摘要，进度仍然显示在终端上。
- **`--json` 永远是纯 JSON**：装饰只作用于人类可读分支，机读输出与 `_runs/*.json`、
  `index.json`、`metadata.json` 一字不改。
- **实时进度**覆盖：条目级（`归档条目 3/6`）、分 P 下载字节（`BV1xx… P01`）、
  ASR 转写段数（`P01 本地转写 42 段`）、配图张数。下载仍在配置的并发数内跑，
  进度只读取 yt-dlp 的进度回调，不改变下载行为。
- **进度先收尾、再输出结果**：完成后的条目级进度会留在运行摘要**上方**，
  绝不会反过来印在摘要后面；分 P/配图/转写这类子任务完成即从进度区撤下，
  一次跑完不会在屏幕上堆成一片进度条。
- **没装 rich 也能用**：彩色与进度需要 `pip install -e ".[ui]"`；
  未安装时自动使用内置纯文本皮肤（版式、信息量、退出码完全一致）。CTRL+C 中断后
  进度条会被正确收尾，光标不会留在隐藏状态。
- **旧版控制台（conhost）**打不开 VT 时自动关闭彩色并提示；Windows Terminal、VS Code
  终端、PowerShell 7 都正常。
- 重定向与管道输出统一为 **UTF-8**（`errors="replace"`）：不会因为 `✔` 这类符号在
  cp936 下把整次运行打断。

---

## 6. 输出结构

```text
output/
  <UID>_<UP名称>/
    index.json                      条目索引（可由各条目 metadata.json 重建）
    _runs/20260923T181220_0800.json 每次运行的结构化摘要
    2026-09-23_动态_摘要_<动态ID>/
      metadata.json                 链接/作者/时间/步骤状态/错误码/格式版本（不含凭据）
      content.md                    正文与配图引用（相对路径）
      images/                       原图 + avatar.*
      render.png                    单张长 PNG
    2026-09-23_视频_标题_<BV号>/
      metadata.json
      videos/P01.mp4 …              各分 P 原格式，不拼接
      transcript/P01.txt|.srt|.subtitle.json
      transcript.txt / transcript_timed.srt   合并稿（标注 P 号与来源）
      summary.md                    正文夹在 bili-sub-archive:summary:begin/end 标记之间
      mindmap.mmd / mindmap.png
    2026-09-23_专栏_标题_<专栏ID>/
      metadata.json / article.md / images/
```

每步状态取 `pending / running / done / skipped / failed`，并带 `reason`
（如 `no_transcript`、`llm_not_configured`、`dependency_missing`、`permission_preview_only`），
这是 `retry` 与"失败可重试"清单的机读依据。条目身份是 `(类型, 平台ID)`；
标题变化不会重命名目录，同秒条目按类型与平台 ID 稳定排序。

---

## 7. 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 全部成功 |
| 1 | 未预期错误 |
| 2 | 配置 / 参数 / 输出目录错误（含 UID 不存在、权限不足、稿件不可见） |
| 3 | 缺少凭据或登录态失效 |
| 4 | 运行完成但有条目失败 / 权限不足 / 部分完成 |
| 5 | 遇到风控且退避重试无效，已停止并提示人工处理（不绕过） |
| 130 | 用户中断（Ctrl+C）——已完成产物保留，可 `retry` 补做 |

---

## 8. 已知限制（交付说明）

### 8.1 平台侧（实测结论）

| # | 限制 | 影响 |
| --- | --- | --- |
| 1 | 已删除与无权限同为 `62002`，接口层**不可区分** | 状态原样记"稿件不可见（62002）"，不写成"权限不足" |
| 2 | 充电视频无权限时接口不报错，只给试看片段 | 记 `permission=preview_only`，`media` 记 `skipped(permission_preview_only)`，**绝不下试看当正片** |
| 3 | 充电条目仅部分可从列表发现（公开列表与充电子集是不同翻页窗口） | 运行摘要如实标注额外扫描成本与差异；不承诺全量 |
| 4 | 未登录时字幕接口静默返回空数组 | 适配层在未登录时直接以登录态错误终止，不把"空"当"无字幕" |
| 5 | 本样本 UP 的 44 篇专栏全是新格式 opus | 旧格式 `read/cv` 分支只有离线样本证据，上线前建议用有旧格式专栏的作者做一次在线验收 |
| 6 | `max_pages` 默认 40，极端账号可能翻不完 | 扫描统计记 `stopped_by=max_pages` 并在摘要告警 |
| 7 | 发现阶段取不到发布时间的条目不计入 N 候选 | 摘要给出条数与"重跑可补做"，不猜时间 |

### 8.2 本机未实测项（**不要当成已验证**）

| # | 未实测 | 已有证据 | 交付前建议 |
| --- | --- | --- | --- |
| 1 | 真实第三方模型的兼容性/限流/长上下文 | 离线替身（401/429/5xx/超时/截断/非法 JSON） | 用真实端点跑一次 `sync --latest 1` |
| 2 | `mindmap.png` 的真实渲染 | `.mmd` 结构检查 + mmdc 替身 + PNG 头校验 | `npm i -g @mermaid-js/mermaid-cli` 后 `retry --steps mindmap` |
| 3 | 联网视频下载与本地 ASR 的端到端 | 离线替身（分片失败重试、断点续传、无字幕、多 P、缺 ffmpeg） | 在装好 yt-dlp + FFmpeg 的机器上跑一次含视频的 `sync` |
| 4 | `openai` SDK 路径 | 替身模块覆盖；SDK 缺失会自动降级为标准库直连 | 可选：`pip install ".[summary]"` 后再跑一次总结 |

### 8.3 其他工程限制

- **分块按字数而非 token**：与模型真实上下文有偏差；超预算时先"要点再压缩"再合并，并如实写说明。
- **`insufficient_text` 是终态**：正文少于 `[summary] min_chars` 不生成总结，补齐文字后用 `--force --steps transcript,summary` 重做。
- **无 ffmpeg 时只能下渐进式单流**：清晰度可能低于 `quality`，摘要会说明"装好 FFmpeg 后可 retry"。
- **`ffprobe` 不可用时降级为魔数校验**：只证明"结构上像媒体"，`verify_note` 如实写明，不冒充已验证。
- **依赖缺失时 `media`/`render` 记 `failed`**：每次 `sync` 会重试一次该步骤，这是"可补做"的正确语义。
- **动态长图超过 `max_height` 会截断**：但会明确告警，不静默处理。

---

## 9. 隐私与凭据

- Cookie、LLM 密钥、含充电内容的本地归档都视为敏感数据；**日志与异常输出统一过 `Redactor`**，
  产物里只允许出现 Cookie 的**字段名**，绝不出现值。
- 归档默认只保存在本机。只有你显式配置了总结端点后，**文字稿**才会发送给你选定的服务；
  不发送 Cookie、媒体文件或原视频地址（`metadata.json` 只记端点主机名）。
- 本地 ASR 全程离线；**关闭时既不下载模型、也不跑识别**，更不会把媒体发给云端识别服务。
- 平台请求有间隔与并发上限；遇到风控/限流即退避，明确风控则停止并提示人工处理，
  **不做验证码绕过、权限破解或账号轮换**。
- 建议 Cookie 只放环境变量或 `config.local.toml`；`require.txt` 属遗留兼容路径（明文，已 gitignore）。

---

## 10. 故障排查

| 症状 | 原因与处理 |
| --- | --- |
| 退出码 3，提示登录态失效 | Cookie 过期或缺失。重新登录 B 站取 `SESSDATA` 等字段，设 `BSA_COOKIE` |
| 退出码 5，风控停止 | 请求过快。加大 `--interval`（如 1.5~2.0）、减少 `--max-pages`，稍后再跑；`retry` 补做 |
| `media` 记 `failed(dependency_missing)` | 缺 yt-dlp/FFmpeg。`pip install ".[media]"` + `winget install Gyan.FFmpeg`，重开终端后 `retry --steps media` |
| `render` 记 `failed(dependency_missing)` | 缺 Pillow。`pip install ".[media]"` 后 `retry --steps render` |
| `mindmap` 记 `failed(dependency_missing)` | 缺 `mmdc`。装 Node.js + mermaid-cli，或用 `--mmdc <路径>`；`.mmd` 已在手 |
| `mindmap` 记 `failed(render_failed)`，mmdc 报 `Could not find Chrome` / `node.launch` | mermaid-cli 的 puppeteer 没装到 Chromium（官方下载源被网络阻断时常见）。两条路：① 装浏览器——`cd "$env:APPDATA\npm\node_modules\@mermaid-js\mermaid-cli"` 后 `npx puppeteer browsers install chrome --base-url https://cdn.npmmirror.com/binaries/chrome-for-testing`；② 免下载——写一个 puppeteer 配置指向系统已装的 Edge/Chrome（`{"executablePath": "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"}`），再把 `[mindmap] puppeteer_config` 指过去，然后 `retry --steps mindmap` |
| `summary` 记 `skipped(llm_not_configured)` | 没配端点/模型。填 `[summary] base_url` 与 `model`（或环境变量）后 `retry --steps summary,mindmap` |
| `summary` 记 `failed(llm_auth_error)` | 密钥无效或服务端要求鉴权；检查 `BSA_LLM_API_KEY` |
| `summary` 记 `failed(llm_response_invalid)`，说明"模型返回内容为空" | 推理模型（如 `deepseek-v4-flash`）的思考 token 也算进 `max_tokens`，2048 会在写完正文前用光。把 `[summary] max_tokens` 提到 8192（或换非推理模型）后 `retry --steps summary,mindmap` |
| `summary` 产物只有摘要、`mindmap` 用派生大纲 | 输出被长度上限截断（`summary.md` 里会记 `finish_reason=length`）。提高 `[summary] max_tokens`，或让自定义 prompt 收紧摘要字数 |
| `summary`/`mindmap` 记 `skipped(no_transcript)` | 该视频无平台字幕且未开 ASR。需要时 `sync --asr` 或 `retry --steps transcript --asr` |
| 动态长图中文显示方块 | 缺中文字体。`[render] font = "C:\\Windows\\Fonts\\msyh.ttc"` |
| 终端没有颜色、看不到进度条 | 按设计降级了，逐项排查：① 输出被重定向/不是交互终端（`--color=always` 可强制）；② 没装 rich —— `pip install -e ".[ui]"`；③ 旧版 conhost 打不开 VT（会打印一行提示，换 Windows Terminal / VS Code 终端即可）；④ 环境里设了 `NO_COLOR`；⑤ 用了 `--no-progress` 或 `--quiet` |
| 目录被写锁占用 | 另一次运行未退出，或上次被强杀（默认 6 小时后可抢占，可调 `[storage] lock_stale_hours`） |
| 提示"无法为 xxx 分配目录：同名目录过多" | 该条目残留了 100 个同名目录，手工清理后重跑 |

---

## 11. 自检与验收

全量离线测试（**不联网、不调用真实模型、不运行 mermaid-cli、不调用 ffmpeg**）：

```powershell
python -m unittest discover -s tests -t .
```

当前基线：**473 项用例全部通过**。其中 1 项（`test_render_wiring.py` 的真实响应渲染）
依赖 `docs/archive/stage0-evidence/` 里的脱敏样本，该目录按 `.gitignore` 不入库，
缺失时自动跳过（输出里的 `s`）。

测试套件锁死的是**交付面**，防止"代码改了文档没改"：

| 模块 | 用例 | 锁什么 |
| --- | --- | --- |
| `tests/test_delivery.py` | 32 | 交付契约：`config.example.toml` 覆盖全部配置字段、README 用到的开关都真实存在、README 相对链接可解析、版本号在 `pyproject.toml` 与 `bili_sub_archive.__version__` 之间一致、退出码映射稳定、仓库无真实凭据、`.gitignore` 覆盖凭据与产物 |
| `tests/test_offline_flow.py` | 47 | 端到端编排：发现 → 筛选 → 归档 → 索引/状态 → 重跑不重复 → `retry` 补做（模型、ffmpeg、mmdc 全部注入替身） |
| `tests/test_summarize.py` | 92 | 总结与导图单元：分块、受控大纲、prompt 三种写法、LLM 客户端（SDK/标准库/替身）、mmdc 调用、指纹与重做 |
| `tests/test_transcript.py` | 53 | 文字稿：平台字幕解析、SRT/纯文本序列化、ASR 兜底、跨 P 合并与来源标注 |
| `tests/test_ui.py` | 37 | 终端呈现层：能力判定（`--color`/`NO_COLOR`）、非 TTY 不写 ANSI、rich 缺失或渲染失败时降级、进度句柄 no-op、方括号不被 rich 当样式标签吞掉、CLI 程序名与旧命令名提示 |
| `tests/test_parse.py` | 35 | 响应解析：富文本与段落类型、两种 `modules` 形状、统一 Item 输出、充电权限判定 |
| `tests/test_media.py` | 29 | 媒体下载：格式选择、错误分类、产物校验、并发/续传/补做 |
| `tests/test_render.py` | 28 | 动态长图排版：换行算法、字体解析、端到端渲染 |
| `tests/test_config.py` | 27 | 配置：TOML 分层、环境变量与命令行覆盖、校验与错误提示 |
| `tests/test_client.py` | 21 | 传输层：WBI 签名、限速/退避、错误分类、图片下载与 URL 升级 |
| `tests/test_paths.py` | 20 | 路径与落盘：名称清理、作者目录复用、原子写入、单写者锁 |
| `tests/test_filters.py` | 14 | 筛选：日期范围（北京时间闭区间）、跨类最新 N、平局顺序、置顶排序 |
| `tests/test_redact.py` | 14 | 脱敏：凭据值绝不出现在文本/对象/产物里，字段名保留 |
| `tests/test_store.py` | 14 | 状态与索引：条目分配、步骤状态转移、索引重建、运行摘要落盘 |
| `tests/test_render_wiring.py` | 10 | 长图渲染**接线**：真实阶段 0 响应 → 渲染项 → 长 PNG（样本缺失时自动跳过） |

v0.1.0 开发期间使用的验收工具（需求第 6 节逐条核对、授权在线验收、动态长图/导图单项检查、
本机替身模型服务）已随过程记录归档到 [`docs/archive/tools/`](docs/archive/README.md)，
**不再维护**，需要时按[归档说明](docs/archive/README.md)自行调整路径后运行。

> **覆盖缺口**：离线套件对"真实 HTTP 链路"只覆盖到客户端单元层
> （`summarize.llm` 的标准库客户端配注入 transport），编排层用的是 `FakeChatClient`；
> 原先那条"编排 + 真实 `urllib` 客户端 + 本机替身模型服务
> （`docs/archive/tools/stage3/local_llm_stub.py`）"的用例已随工具归档移除。
> 需要验证真实链路时，可按归档说明起替身服务后手工跑一次。

---

## 12. 文档索引

```text
README.md                                    安装、使用、已知限制（交付说明）
config.example.toml                          配置模板（只含占位符）
prompts/summary.toml                         总结 prompt 示例（真 TOML）
bili_sub_archive/                                    产品代码
tests/                                       离线测试（473 项，见 11）
docs/archive/README.md                       开发过程归档索引（冻结，不再维护）
docs/archive/REQUIREMENTS.md                 需求与验收标准
docs/archive/DEVELOPMENT_PLAN.md             开发方案与阶段门槛
docs/archive/stage0-platform-verification.md 接口白名单、11 条契约、充电可见性实测
docs/archive/stage0-component-verification.md 依赖许可证、维护状态、Python 3.14 兼容性
docs/archive/stage1|2|3|4-*.md               各阶段实施与验证记录
docs/archive/tools/                          当时的验证工具源码（不再维护）
docs/archive/stage0-evidence/                阶段 0 脱敏响应样本（不入库）
```

### 代码结构

```text
bili_sub_archive/
  __main__.py     bsa 入口
  cli.py          命令行：子命令装配、输出模式、退出码
  runner.py       编排：发现 → 筛选 → 归档 → 状态/索引 → 运行摘要
  config.py       TOML + 环境变量 + 命令行三层合并与校验
  credentials.py  Cookie / UID / LLM 密钥的来源与优先级（含 require.txt 遗留路径）
  deps.py         可选依赖探测与安装提示（check 的数据源）
  models.py       Item、EntryState、RunSummary 与步骤状态机
  store.py        index.json / metadata.json / _runs 的读写与写锁
  paths.py        目录名清理、作者目录定位、原子写入、单写者锁
  filters.py      日期范围、跨类最新 N、平局顺序
  discover.py     三类分页发现、去重、扫描统计
  errors.py       错误分类、异常类型、退出码映射
  redact.py       凭据脱敏（日志/异常/产物统一过一遍）
  log.py          日志与 UTF-8 stdio
  ui.py           终端呈现层（彩色/面板/进度；rich 可选，缺失自动降级）
  timeutil.py     北京时间换算、日期解析与范围
  bili/           平台适配：api.py 白名单接口、client.py 限速/WBI/退避、parse.py、wbi.py
  archive/        归档器：dynamic.py、video.py、article.py、images.py、htmlmd.py
  media.py        yt-dlp 下载、分片并发/续传、ffprobe 校验
  transcript/     字幕提取、SRT、ASR 兜底、跨 P 合并
  render/         动态长图渲染（Pillow + 字体探测）
  summarize/      LLM 客户端、分块、prompt、受控大纲、Mermaid 与 mmdc
```
