# 阶段 0 组件选型验证（许可证 / 维护状态 / Python 3.14 兼容性）

状态：已完成
日期：2026-09-23
依据：[DEVELOPMENT_PLAN.md](../DEVELOPMENT_PLAN.md) 第 2.1-5 项（"验证候选封装库的维护状态和许可证"）、第 2.2 节拟用组件表
配套：[阶段 0 平台可行性验证记录](stage0-platform-verification.md)

---

## 0. 方法与可信度声明

数据来源为**官方一手来源**：PyPI JSON API（`upload_time_iso_8601` 与逐版本的 wheel tag 列表）、npm registry `time` 映射、GitHub REST/atom、以及各项目官方文档。

完整核查报告（含逐版本 wheel tag 明细）保留在 [`stage0/evidence/component-verification-raw.md`](../stage0/evidence/component-verification-raw.md)。

> **可信度边界**：所有"cp314 可用"的结论都是从**已发布的 wheel tag 推断**（含 PEP 425 `abi3` 稳定 ABI 兼容），**并非在本机实际安装执行**。凡推断而来的结论，本文与第 5 节均标注为"推断"。第 4 节另行记录了在本机 Python 3.14.7 上真实执行的检查。

---

## 1. 结论总表

| 组件 | 最新版本 | 发布日 | 许可证 | Python 3.14 / Windows 风险 | 维护状态判定 |
| --- | --- | --- | --- | --- | --- |
| `yt-dlp` | 2026.8.19 | 2026-08-19 | Unlicense（PyPI/git）；独立二进制为 GPLv3+ 组合作品 | **无**（纯 Python wheel，classifier 明确含 3.14，另有 `yt-dlp.exe`） | 非常活跃（193k★，提交 2026-09-16，约月度发版） |
| `bilibili-api-python` | 17.4.2 | 2026-06-19 | PyPI 标 `GPL-3.0-or-later`，但仓库 LICENSE 已删除 | 非 wheel 问题（纯 Python，原生依赖均可 cp314/abi3 安装）；**3.14 运行未验证** | **仓库已归档（2026-07-06）+ 收到 B 站侵权告知函 —— 不采用** |
| `bilibili-api`（旧名） | 9.1.0 | 2021-11-24 | GPLv3+ | 未测试，classifier 仅 3.8/3.9 | 2021 年起废弃 |
| `httpx` | 0.28.1 | 2024-12-06 | BSD-3-Clause | **无** | 维护中但缓慢（约 21 个月无稳定版；1.0 在 dev） |
| `faster-whisper` | 1.2.1 | 2025-10-31 | MIT | **低**（ctranslate2 / av / onnxruntime 均有 cp314 win wheel；tokenizers 走 abi3） | 放缓（25.5k★，最后提交 2025-11-19） |
| `paddleocr` | 3.7.0 | 2026-06-11 | "Apache License 2.0"（无 SPDX 表达式） | ➖ **组件已取消**（见 §2.1） | 非常活跃（90k★，提交 2026-09-16），但本项目不再使用 |
| `paddlepaddle` | 3.3.1 | 2026-03-26 | "Apache Software License" | ➖ **组件已取消**（原为 Python 3.14 硬阻塞） | 中等（最近发版约 6 个月前），本项目不再使用 |
| `openai` | 3.19.0 | 2026-09-23 | Apache-2.0 | **无**；但会引入 **`httpx2`**（见 §2.3） | 极度活跃（2026-09 内 6 次发版） |
| `Pillow` | 12.3.0 | 2026-07-01 | MIT-CMU | **无**（已发布 cp314 win_amd64） | 健康（13.8k★，提交 2026-09-23，季度发版） |
| `imageio-ffmpeg` | 0.6.0 | 2025-01-16 | BSD-2-Clause（**随附二进制许可证未核实**） | **无**（`py3-none-win_amd64`，内含 `ffmpeg.exe`） | **停滞**（300★，自 2025-01-16 无更新；README 自己建议改用 PyAV） |
| `@mermaid-js/mermaid-cli` | 11.17.0 | 2026-09-02（npm） | MIT | 与 Python 无关；Windows 风险在 Puppeteer peer 依赖 + Chromium 下载 | 活跃（5.0k★，提交 2026-09-21） |
| `ffmpeg`（二进制） | gyan.dev 9.0.2 / git 2026-09-21；BtbN 与 yt-dlp 构建 2026-09-22 | 2026-09-19/21/22 | 依构建而定：gyan.dev 为 GPLv3；BtbN 同时提供 GPL 与 LGPL | **无**（外部二进制） | 持续重建（每日/每周） |

---

## 2. 两个必须落到决策上的结论

### 2.1 PaddleOCR 在 Python 3.14 上装不上 → 已决定取消本地 OCR

**结论：本地 OCR 已移出项目范围**（2026-09-23 决定）。不引入 PaddleOCR，也就不引入它的依赖链，因此本节记录的问题**不再需要解决**。以下保留原始核查结果与决策理由，作为存档。

原始发现：`paddleocr` 本身是纯 Python wheel，能解析；但它的依赖链 `paddlex[ocr-core]` → `paddlepaddle` 断了：

- 扫描 `paddlepaddle` **全部历史版本**：**任何平台都从未发布过 cp314 wheel**，上限为 cp313；
- `paddlepaddle` **完全不发布 sdist**（0 个），因此 pip **无法回退到源码构建**；
- 次级问题：`paddlex` 3.7.2 精确钉住 `PyYAML==6.0.2`，该版本既无 cp314 wheel 也无 abi3 wheel。

**本机实测确认**（Python 3.14.7，见 §4）：`pip install --dry-run --no-deps paddlepaddle` →
`ERROR: Could not find a version that satisfies the requirement paddlepaddle (from versions: none)`。

**决策**：取消本地 OCR，而不是为它换引擎、加旁路进程或降低 Python 版本。理由：

1. **实际收益低**：阶段 0 实测本项目样本公开列表前 10 条视频 **10/10 都带 AI 中文字幕**（见平台验证记录 3.3.3），文字稿的主路径是字幕，ASR 已是兜底；OCR 属于第三层冗余。
2. **代价高**：OCR 需要 `ffmpeg` 抽帧 + 本地推理模型 + 采样间隔与去重策略，是安装复杂度与资源消耗的主要来源之一（计划第 7 节风险表原本就把它列为"Windows 本地模型资源消耗/首次安装复杂"）。
3. **可随时加回**：视频画面文字属于增量能力，取消它不影响已有的字幕/ASR 文字稿、总结、导图链路。若后续确有需要，可另行评估 ONNXRuntime 系 OCR。

**影响面**（已同步到 `REQUIREMENTS.md` 与 `DEVELOPMENT_PLAN.md`）：

| 位置 | 变更 |
| --- | --- |
| 需求 §2 输入与运行方式 | 文字提取只保留"本地语音转写"开关 |
| 需求 §3.3.2 | 文字稿来源标注由"字幕/ASR/OCR"改为"字幕/ASR" |
| 需求 §4 输出结构 | 移除 `ocr.txt` |
| 需求 §6 验收 3、7 | 移除 OCR 相关验收项 |
| 需求 §7 已确认边界 | 新增"不提供本地视频画面 OCR" |
| 计划 §1 交付目标 | 明确"不提供本地 OCR" |
| 计划 §2.2 组件表 | 移除"本地 OCR"整行与 PaddleOCR 参考链接 |
| 计划 §3 数据流 / §3.3 产物 | 移除 OCR 环节与 `ocr.txt` 说明 |
| 计划 §4 CLI | 移除 `--ocr` 与"OCR 采样间隔" |
| 计划 §5 阶段 2 / §6 测试 / §7 风险 | 移除 OCR 相关条目 |

**顺带结果**：**Python 3.14 上不再有已知的依赖阻塞。** 项目可以继续用当前 3.14.7，不需要降到 3.13，也不需要为 OCR 准备旁路解释器。

### 2.2 `bilibili-api-python` 不采用 —— 支持自研适配层

不是 wheel 问题（它本身是纯 Python，原生依赖 `lxml` / `brotli` / `pillow` / `yarl` 均有 cp314 win wheel，`pycryptodomex` 走 `cp37-abi3`）。真正的问题是：

- 仓库 `Nemo2011/bilibili-api` **已归档**（GitHub `archived: true`），releases 页注明"archived by the owner on **Jul 6, 2026** … now read-only"；
- `main` 分支 README 已被替换为「**本仓库已停止维护并将永久关停**」，并附一份**上海市弘安律师事务所代表 B 站出具的侵权告知函**（2026-07-06）；
- **`LICENSE` 文件已删除**（HTTP 404），GitHub 许可证检测结果为 `license: none`；
- 最后一个版本 17.4.2 发布于 2026-06-19，最后提交 2026-07-06，提交信息为 `owari`。

**结论**：阶段 0 平台验证已经用约 250 行自研代码（`tools/stage0/client.py`）打通了全部 14 个所需接口，覆盖限速、WBI 签名、风控退避与错误分类；加上该库的法律状态不明与 GPL-3.0-or-later 的传染性，**维持"自研薄适配层"的选型**（详见平台验证记录第 5.3 节）。

### 2.3 `openai` 3.x 引入的是 `httpx2`，不是 `httpx`

`openai` 3.19.0 的依赖是 **`httpx2<3,>=2.12.0`**——与 `httpx` 是**两个不同的发行包**（`httpx2` 2.13.1，2026-09-23，BSD-3-Clause，纯 Python）。若自研适配层用 `httpx`，则项目里会**同时存在两套 HTTP 栈**。

**影响与建议**：这不是错误，但属于需要知情的体积/依赖面问题。可选：(a) 接受两套并存；(b) 自研适配层也统一用 `httpx2`；(c) 自研适配层保持标准库 `urllib`（阶段 0 已验证可行），则只多出 `openai` 这一条链。Chat Completions 在 3.19.0 文档中仍被明确支持（"supported indefinitely"）。

---

## 3. 其他值得记录的点

- **两个 abi3 陷阱**：`tokenizers` 0.23.2 与 `pycryptodomex` 3.23.0 **没有 cp314 tag**，但分别发布 `cp310-abi3-win_amd64` 与 `cp37-abi3-win_amd64`，通过稳定 ABI 在 3.14 上可安装。**不要把"没有 cp314 wheel"一律读成"不可用"**。
- **`imageio-ffmpeg` 停滞**：0.6.0 自 2025-01-16 起无更新，其 README 自己建议改用 PyAV。它的价值只剩"开箱带一个 `ffmpeg.exe`"（本机实测为 **ffmpeg 7.1-essentials gyan.dev 构建**，见 §4）。项目若要长期维护，应改为显式依赖系统/自备 ffmpeg，而不是依赖该 wheel 里的陈旧二进制。
- **ffmpeg 许可证**：ffmpeg 核心为 LGPL-2.1+，但含 libx264/libx265 等 GPL-only 组件时整包为 GPL。gyan.dev 构建为 GPLv3；BtbN 同时提供 GPL 与 LGPL 构建。若本项目要分发含 ffmpeg 的包，许可证需按所选构建核对。
- **不要使用 PyPI 上名为 `ffmpeg` 的包**（yt-dlp README 明确警告）。
- **mermaid-cli 的 Windows 风险点是安装期**：`puppeteer` 自 10.9.1 之后变为 **peerDependency**（`^23 || ^24 || ^25`），npm 7+ 会自动安装并**下载 Chromium**——这是 Windows/代理/杀软环境最常见的失败点。建议在安装指引中显式说明，或预置 `PUPPETEER_SKIP_DOWNLOAD` + 指定本机 Chrome。

---

## 4. 本机实测补充（Python 3.14.7 / Windows）

以下为在本机真实执行、可复现的检查，与上面的"推断"区分开：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Python | `python --version` | **3.14.7** |
| Node.js | `node --version` | **v24.18.0** —— 满足 mermaid-cli 的 `engines: ^18.19 \|\| >=20.0` |
| `paddlepaddle` 可安装性 | `python -m pip install --dry-run --no-deps paddlepaddle` | ❌ `ERROR: Could not find a version that satisfies the requirement paddlepaddle (from versions: none)` |
| `paddleocr` 可安装性 | `python -m pip install --dry-run --no-deps paddleocr` | wheel 本身可解析（3.7.0），但其 `paddlepaddle` 依赖不可满足 |
| `ffmpeg` 是否在 PATH | `Get-Command ffmpeg` | ❌ 不在 PATH |
| `ffmpeg` 可用来源 | `imageio_ffmpeg.get_ffmpeg_exe()` | ✅ `...\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe`，87.6 MB，`7.1-essentials_build-www.gyan.dev` |
| `mmdc`（Mermaid CLI） | `Get-Command mmdc` | ❌ 未安装 |
| whisper.cpp 二进制 | `Test-Path E:\bilivideo\tools\whisper\Release\whisper-cli.exe` | ✅ 存在（旧工程遗留，可复用） |
| 已安装的 Python 包 | `pip list` | `certifi` `charset-normalizer` `comtypes` `idna` `imageio-ffmpeg` `pillow` `PyYAML` `requests` `uiautomation` `urllib3` |

**对本机安装指引的含义**：阶段 4 的 Windows 安装说明需要覆盖 Python、FFmpeg（当前仅由 `imageio-ffmpeg` 间接提供，建议改为显式安装）、Node.js + Mermaid CLI（当前完全缺失）。ASR 侧已有 whisper.cpp 二进制与模型可复用（旧工程 `E:\bilivideo\tools\`）。

---

## 5. 明确未核实项

以下事项本次**没有**核实，不得在后续文档中当作已知事实：

1. `bilibili-api-python` 17.4.2 在 3.14 上是否真能运行（其 classifier 只到 3.13）—— 该库已排除，此点仅作记录。
2. `faster-whisper` 1.2.1 在 3.14 上是否真能运行（classifier 只到 3.11）—— 只核实了其依赖的 wheel 覆盖。
3. `imageio-ffmpeg` wheel 内随附 ffmpeg 二进制的**许可证声明**与确切版本（本机实测版本见 §4，但 wheel 元数据中无声明）。
4. 本机是否装有 MSVC 构建工具（仅在需要 sdist 回退时有影响）。
5. 除 §4 列出的检查外，**没有任何组件在 Python 3.14 上被实际安装或执行**。

> 原第 1、2 项（PaddlePaddle 官方索引是否存在 cp314 构建、`PyYAML==6.0.2` sdist 能否构建）已随**取消本地 OCR** 而失去意义，不再列为待核实项。

---

## 6. 对计划第 2.2 节的修订建议

| 职责 | 计划原方案 | 结论 |
| --- | --- | --- |
| B 站采集 | 独立 `BilibiliClient` 适配层；在 `httpx` 与封装库之间选型 | **确定为自研适配层**（`urllib` 或 `httpx`）。封装库 `bilibili-api-python` 已归档且法律状态不明，排除 |
| 视频下载 | `yt-dlp` Python API | 维持。许可证 Unlicense（注意：其独立二进制为 GPLv3+ 组合作品） |
| 字幕 / 本地 ASR | 字幕解析；`faster-whisper` | 维持，风险低。**另注**：本项目样本 10/10 视频均有 AI 中文字幕，ASR 实际很少触发（见平台验证记录 3.3.3） |
| ~~本地 OCR~~ | ~~`ffmpeg` 抽帧 + PaddleOCR~~ | ➖ **已取消**（2026-09-23）。组件与依赖链整体移出范围，`REQUIREMENTS.md` / `DEVELOPMENT_PLAN.md` 已同步 |
| 动态图片 | Pillow | 维持（cp314 win wheel 已发布） |
| 总结 | `openai` Python SDK | 维持。**注意会引入 `httpx2`** |
| 导图 | Mermaid CLI 渲染 PNG | 维持。安装指引需显式覆盖 Node.js 与 Puppeteer/Chromium 下载 |
| 本地状态 | `metadata.json` + `index.json`，原子写入 | 无外部依赖，维持 |
| 媒体处理 | `ffmpeg`/`ffprobe` | 维持。建议显式安装而非依赖 `imageio-ffmpeg` 的陈旧内置二进制。**注意：取消 OCR 后 ffmpeg 仅用于 ASR 的音频抽取与媒体校验，不再需要抽帧** |
