# 阶段 4 稳定与交付：实施与验证记录

状态：已完成（2026-09-23）
依据：[DEVELOPMENT_PLAN.md](../DEVELOPMENT_PLAN.md) 第 5 节（阶段 4 门槛）、第 6 节（测试方案）· [REQUIREMENTS.md](../REQUIREMENTS.md) 第 2、5、6、7 节 · [阶段 3 衔接点](stage3-summary-and-mindmap.md#10-阶段-4-衔接点)
复现：`python -m unittest discover -s tests -t .`（离线 **427** 用例，约 45s）· `python -m tools.stage4.acceptance`（需求第 6 节逐条核对）· `python -m tools.stage4.online_acceptance --uid <UID> --latest 3`（授权在线验收，需自备 Cookie）
产物：[README.md](../README.md)（Windows 安装与使用说明 / 交付说明）· [config.example.toml](../config.example.toml)（配置模板）· `tools/stage4/`（离线 + 在线验收工具）· `tests/test_delivery.py`（交付契约测试）

---

## 1. 交付内容

```text
README.md                                 安装、配置、使用、退出码、已知限制（交付说明）
config.example.toml                       配置模板（只含占位符，覆盖全部配置键）
docs/stage4-stability-and-delivery.md     本文件：门槛对照 + 需求第 6 节验收矩阵 + 限制汇总
tools/stage4/
  acceptance.py                           需求第 6 节逐条**离线**核对（真实样本 + 离线套件）
  online_acceptance.py                    授权**在线**验收：首次采集 + 增量重跑 + 产物核对
tests/test_delivery.py                    交付契约：模板漂移、文档与 CLI 一致、版本、退出码、仓库卫生
```

命令面（本阶段只修正"全局开关的位置"，不新增子命令）：

```powershell
python -m subvideo check  --uid 1039025435                  # 依赖/配置/登录态（阶段 4 文案已更新）
python -m subvideo sync   --uid 1039025435 --latest 20
python -m subvideo sync   --uid 1039025435 --latest 20 --json      # ← 阶段 4 起写在子命令后面也生效
python -m subvideo --json sync --uid 1039025435 --latest 20        # 写在前面同样生效
python -m subvideo retry  --uid 1039025435 --steps summary,mindmap
python -m subvideo sync   --uid 1039025435 --config config.local.toml   # ← --config 同样两处都可写
```

版本：`subvideo 0.3.0 → 0.4.0`；**`FORMAT_VERSION` 保持 3**（阶段 4 不新增功能步骤、
不改产物结构，因此不需要迁移，`models.IMPLEMENTED_STAGE` 也仍是 3 —— 它表示"步骤实现到第几阶段"）。
顺带修正一处历史不一致：`pyproject.toml` 的版本此前停在 `0.1.0`，现已与 `subvideo.__version__` 对齐（有测试锁死）。

---

## 2. 阶段 4 完成门槛对照（计划第 5 节）

| 计划门槛 | 结果 | 证据 |
| --- | --- | --- |
| 单元/集成/授权样本测试 | ✅ 单元 + 集成：427 例离线用例全通过（新增 34 例交付契约）；真实样本：`tools.stage4.acceptance` 用阶段 1/2/3 的**真实归档产物**核对；授权在线：`tools.stage4.online_acceptance` 已就绪（需用户 Cookie，见第 7 节） | 第 4.1、4.2 节 |
| Windows 安装和使用说明 | ✅ [README.md](../README.md)：系统要求表、四步安装（Python/FFmpeg/Node+mermaid-cli/可选 ASR）、配置与环境变量、使用与补做、输出结构、退出码、故障排查、隐私 | 第 5 节 |
| 配置模板 | ✅ `config.example.toml` 覆盖 **57 个配置字段**（映射由 `tests/test_delivery.py::ConfigTemplateTest` 锁死，新增配置项忘写模板即失败） | 第 4.3 节 |
| 需求第 6 节验收项逐条通过 | ✅ 离线可证部分全部通过（7 条验收项的机器可验证面）；**需联网/凭据/缺失组件才能证明的部分单独列出**，不冒充通过 | 第 3 节 |
| 已知平台限制列入交付说明 | ✅ README 第 8 节（平台侧 7 条 + 本机未实测 4 条 + 工程限制 6 条）；来源汇总见第 6 节 | 第 6 节 |

---

## 3. 需求第 6 节验收项逐条对照

### 3.0 方法：三类证据，严格分开

`tools.stage4.acceptance` 对每条验收项给出三类信息，**互不冒充**：

| 类别 | 含义 | 例子 |
| --- | --- | --- |
| 证据 | 机器已验证（真实产物结构 + 离线用例） | "4 张原图全部落盘且被正文引用" |
| 失败 | 断言不成立（真问题） | "index.json 存在重复条目" |
| 待验 | 需要联网 / 凭据 / 本机缺失组件（yt-dlp、FFmpeg、mermaid-cli、云端模型）才能证明 | "真实样本没有跑通总结/导图的视频" |

只有"证据"为空且无失败时该项才是 SKIP。运行结果（本机，2026-09-23）：

```text
结论：PASS 8 / FAIL 0 / SKIP 0（共 8 条验收项）
```

（8 条 = 需求第 6 节的 7 条 + 1 条"交付物齐备"。）

### 3.1 验收 1：三类内容分别归档、目录含日期与类型、重复运行不重复

| 证据 | 内容 |
| --- | --- |
| 真实样本 | `1039025435_战国时代_姜汁汽水`：10 条索引键 `(类型, 平台 ID)` 唯一、目录与 `metadata.json` 齐备（动态 6 / 视频 2 / 专栏 2）、目录名全部形如 `YYYY-MM-DD_<类型>_…`、**没有未被索引登记的孤儿目录**（重复分配会立刻暴露） |
| 真实运行记录 | `_runs/` 11 次运行中有 **2 次**「选中 N 条 → 全部跳过」的增量重跑（`20260923T181308`：selected 6 / skipped 6；`20260923T181505`：selected 4 / skipped 4） |
| 离线用例 | `FullFlowTest::test_full_sync_archives_all_three_kinds`（三类端到端 + 图片不变量 + 产物无凭据）、`test_idempotent_rerun_skips_everything`（第二次 11 条全跳过、目录集合不变）、`test_dry_run_writes_nothing`、`NamingTest`、`IndexTest` —— 13 例通过 |

### 3.2 验收 2：多图动态保存全部原图 + 完整长 PNG

| 证据 | 内容 |
| --- | --- |
| 真实样本 | `2026-09-22_动态_最近这个评论区给我整不会了…`：**4 张原图全部落盘且都被 `content.md` 引用**；归档的 `render.png` 为 **1080×8601**（2160 KiB） |
| 现场重渲染 | 用同一真实样本（真中文正文 + 真配图 + 真头像）现场重渲染：**1080×8601、2160 KiB、配图 4/4 张、未触发高度上限**；头部字段（作者 / 发布时间 / 正文项 / 头像）全部非空 —— 对应需求"至少包含 UP 名称、头像、发布时间、动态正文和动态配图" |
| 离线用例 | `RenderLongPngTest`、`LongImageModuleTest`、`Stage2FlowTest::test_no_render_flag_skips_render_step` —— 15 例通过 |

> 重渲染产物写在 `--work`（默认 `.test_tmp/stage4_acceptance/`），**不改动归档目录**。
> 真实样本里那条 `render.png` 是阶段 2 验证工具补生成的（`metadata.json` 的 `render` 步骤仍是
> `skipped(stage2_not_implemented)`），`retry --steps render` 可让流程正式补做 —— 验收工具如实标注，不掩饰。

### 3.3 验收 3：有字幕的视频产生带时间戳文字稿、总结、导图；失败有明确状态

| 证据 | 内容 |
| --- | --- |
| 真实样本（文字稿） | 两条视频各有 `transcript.txt`（10094 / 11043 字，含 `## P01` 分 P 小节）与 `transcript_timed.srt`（901 / 970 条 cue，全部带 `-->` 时间戳）；来源标注 `subtitle`（平台 AI 中文字幕） |
| 真实样本（总结/导图） | 两条视频的 `summary`/`mindmap` 均为 `skipped(llm_not_configured)` 且带完整原因文案 —— **这正是需求 3.3.6/6.3 要的"失败有明确状态"**；本机未配置云端模型，因此**不宣称已产生真实总结** |
| 离线用例 | `Stage3FlowTest`（13 例：端到端产物、真实长稿尾段进合并、模型失败可重试、缺 mmdc 保留 `.mmd`、指纹复用、**不注入替身走真实 HTTP 客户端**）、`SubtitleParseTest`、`MergeTest`、`MermaidCliTest` —— 31 例通过 |
| 待验 | 真实第三方模型与真实 `mmdc` 渲染：`python -m subvideo retry --uid <UID> --steps summary,mindmap`（配好 `[summary] base_url`+`model` 与 mermaid-cli 之后） |

### 3.4 验收 4：可配置视频下载并发数；部分失败后重跑只补未完成步骤

| 证据 | 内容 |
| --- | --- |
| 配置面 | `video_workers`（默认 3）与 `segment_workers`（默认 4）都是正整数配置项，命令行/TOML 均可覆盖 |
| 真实样本 | 两条视频的 `extra.media` 逐分 P 记录状态，失败分 P 带可定位原因（本机缺 yt-dlp → `dependency_missing`，步骤记 `failed(download_failed)`） |
| 离线用例 | `DownloadMediaTest`（15 例：并发受 `video_workers` 约束、部分失败记录原因、重跑只补失败分 P、断点续传标记、校验拒绝非媒体）、`test_media_partial_failure_then_retry_only_fills_missing`、`test_dependency_missing_then_install_and_retry`、`RetryTest` —— 22 例通过 |
| 待验 | 真实 yt-dlp + FFmpeg 的联网下载（本机未安装，见第 4.4 节） |

### 3.5 验收 5：凭据不泄露、失败原因具体、不覆盖已完成结果

| 证据 | 内容 |
| --- | --- |
| 真实样本扫描 | 29 个产物文本文件（`metadata.json`/`content.md`/`article.md`/`transcript*`/`.mmd` 等）中**没有**任何"敏感键 = 值"形态的凭据；`metadata.json` 不含 `cookie`/`api_key` 等键 |
| 失败可读性 | 真实样本里所有 `failed`/`skipped` 步骤都带 `reason`/`error_kind`/`message`（`retry` 的机读依据） |
| 离线用例 | `test_redact`（脱敏与产物回扫）、`GuardTest`（缺 Cookie / UID 不存在 / 登录态失效 / 并发写锁）、`test_llm_failure_is_retryable`、`test_secrets_never_leak_into_transcript_products` —— 22 例通过 |
| 交付文件自检 | `tests/test_delivery.py::RepoHygieneTest` 断言 README/模板/计划/需求里没有长 Cookie 或 `sk-` 密钥值；`.gitignore` 覆盖 `config.local.toml`、`require.txt`、`output/`、`.test_tmp/`、`.smoke_sync/` |

### 3.6 验收 6：日期范围 / 跨类最新 N；多 P 同目录、一份合并总结与导图

| 证据 | 内容 |
| --- | --- |
| 真实样本 | `index.json` 按发布时间降序（10 条），跨类排序稳定可查 |
| 离线用例 | `test_filters`（日期范围、跨类最新 N、同秒平局、边界、去重）、`FilterTest`（真实流程里的日期筛选与最新 N）、`Stage3FlowTest::test_summary_and_mindmap_end_to_end` —— 18 例通过 |
| 多 P | `FullFlowTest` 的样本视频有 **2 个分 P**：媒体同目录、不拼接、`transcript/P01.srt`+`P02.srt` 与合并稿带 `[P02]` 前缀；总结与导图基于**合并稿**生成（`ChunkTest` 覆盖"`## Pxx` 归属到块"） |
| 待验 | 真实样本（`.smoke_sync`）里两条视频都是单 P，"真实多 P 下载"仍属在线验收项 |

### 3.7 验收 7：本地 ASR 可开关；关闭时不运行识别、不外发媒体

| 证据 | 内容 |
| --- | --- |
| 默认值 | `Config().asr_enabled is False` —— 未显式开启时不下载模型、不跑识别（需求 6.7） |
| 真实样本 | 文字稿来源全部是 `subtitle`；产物里没有 ASR 中间音频残留（默认转写完即删） |
| 离线用例 | `test_asr_flow_uses_transcriber_only_when_no_subtitle`（只在无字幕的分 P 调用转写器）、`test_transcript_asr_disabled_then_enable_asr_retry`（关闭时一次都不调用，开启后可补做）、`TranscriberTest` —— 8 例通过 |
| 待验 | 真实 `faster-whisper` 首次模型下载与 CPU 转写（本机未安装） |

### 3.8 汇总

| 验收项 | 离线结论 | 需在线/凭据的部分 |
| --- | --- | --- |
| 6.1 三类归档 + 判重 | ✅ PASS | —— |
| 6.2 多图动态 + 长 PNG | ✅ PASS（含现场重渲染） | —— |
| 6.3 文字稿 + 总结 + 导图 | ✅ PASS（文字稿与状态机）；总结/导图链路 = 离线替身 + 真实 HTTP 往返 | 真实第三方模型、真实 `mmdc` 渲染 |
| 6.4 并发 + 只补未完成 | ✅ PASS | 真实 yt-dlp/FFmpeg 联网下载 |
| 6.5 凭据与失败原因 | ✅ PASS | —— |
| 6.6 日期/最新 N/多 P | ✅ PASS（多 P 为离线用例） | 真实多 P 下载 |
| 6.7 ASR 开关 | ✅ PASS | 真实 faster-whisper 转写 |
| 交付物齐备 | ✅ PASS | —— |

---

## 4. 本机验证（2026-09-23，Windows / Python 3.14.7）

### 4.1 全量离线测试

```text
python -m unittest discover -s tests -t .
Ran 427 tests in 45.6s
OK
```

阶段 3 为 393 例，本阶段新增 **34** 例（`tests/test_delivery.py`）。测试全程不联网、不调用真实模型、
不运行 mermaid-cli；唯一的 socket 用法是 `Stage3FlowTest::test_real_http_client_against_local_stub`
（本机 loopback 替身服务，阶段 3 引入）。

### 4.2 离线验收工具（真实样本 + 离线套件）

```text
python -m tools.stage4.acceptance --report .test_tmp/stage4_acceptance/report.json
结论：PASS 8 / FAIL 0 / SKIP 0（共 8 条验收项）
```

验收项到测试的映射（`tools/stage4/acceptance.py::SUITE_BY_CHECK`，由 `test_delivery` 锁死名称存在）：

| 验收项 | 映射到的离线用例 | 例数 |
| --- | --- | --- |
| 6.1 | `FullFlowTest` 3 例 + `NamingTest` + `IndexTest` | 13 |
| 6.2 | `RenderLongPngTest` + `LongImageModuleTest` + `test_no_render_flag_skips_render_step` | 15 |
| 6.3 | `Stage3FlowTest` + `SubtitleParseTest` + `MergeTest` + `MermaidCliTest` | 31 |
| 6.4 | `DownloadMediaTest` + 2 个流程用例 + `RetryTest` | 22 |
| 6.5 | `test_redact` + `GuardTest` + 2 个流程用例 | 22 |
| 6.6 | `test_filters` + `FilterTest` + `test_summary_and_mindmap_end_to_end` | 18 |
| 6.7 | 2 个流程用例 + `TranscriberTest` | 8 |
| 交付物 | `test_delivery` | 33 |

（各验收项之间有重叠用例，合计 129 例次；`--quick` 可只跑真实样本检查。）

### 4.3 交付契约测试（新增 34 例）

| 测试类 | 用例 | 锁住的契约 |
| --- | --- | --- |
| `ConfigTemplateTest` | 6 | 模板可被 `tomllib` 解析；**57 个配置字段全部出现在模板里**；模板里没有代码不识别的键；模板无凭据值；模板作为 `config.toml` 能真的加载出文档化默认值；`Config` 新增字段必须登记映射 |
| `ReadmeContractTest` | 6 | README 十个必需章节；覆盖 Python 3.11/yt-dlp/FFmpeg/Node.js/mermaid-cli/faster-whisper/Pillow/openai 与安装命令；退出码 0/1/2/3/4/5/130 全部记录；**README 里每条 `subvideo <子命令>` 命令用到的开关都被该子命令接受**；ASR 默认关闭、`no_transcript`、`llm_not_configured`、不提供 OCR 均已写明 |
| `CliSurfaceTest` | 8 | 子命令固定为 `check/sync/retry`；`--json`/`-v`/`--config` 写在子命令**前后都生效**（本阶段修正）；`retry` 接受配置覆盖开关、但不接受筛选开关；`--version` 退出码 0；未知步骤报 `ConfigError`；退出码常量与错误分类映射稳定 |
| `VersionContractTest` | 3 | `pyproject.toml` 版本 == `subvideo.__version__`；`FORMAT_VERSION` 仍为 3；阶段 4 记录与计划互相链接 |
| `DependencyHintTest` | 4 | 每个被探测的依赖都有可操作的安装提示；只有 `openai` 是可选依赖；可选依赖不进"缺失"告警 |
| `RepoHygieneTest` | 4 | `.gitignore` 覆盖凭据与产物目录；交付文件里没有真实凭据；本地配置被忽略 |
| `AcceptanceToolTest` | 4 | 验收工具点名的测试全部存在；`--quick` 在无样本时**只能是 SKIP 不能假装 PASS**；验收项清单恰为 7+1；在线工具开关面正确 |

### 4.4 本机环境实测

| 检查 | 结果 |
| --- | --- |
| Python | **3.14.7**（`requires-python >= 3.11`） |
| Node.js / npm | **v24.18.0 / 12.0.2**（满足 mermaid-cli 的 engines 要求） |
| Pillow | **已安装 12.3.0** → 动态长图链路真实可用（4.2 节的现场重渲染即用它） |
| 中文字体 | `C:\Windows\Fonts\msyh.ttc` 等 3 个候选可用 |
| yt-dlp / FFmpeg / ffprobe | **未安装** → 真实视频下载未实测（离线替身覆盖） |
| faster-whisper | **未安装** → 真实 ASR 未实测（离线替身覆盖） |
| mermaid-cli（mmdc） | **未安装** → `mindmap.png` 真实渲染未实测（`.mmd` 结构检查 + 替身覆盖） |
| `openai` SDK | 未安装（**可选**：缺失时标准库直连同一端点） |

`subvideo check` 会把这四项缺失连同安装命令一起列出，不需要看文档也能装对。

### 4.5 交付前修正的问题

| # | 问题 | 影响 | 修正 |
| --- | --- | --- | --- |
| 1 | `--json` / `-v` / `--quiet` / `--config` 只挂在主解析器上，写在子命令**后面**会报 `unrecognized arguments` | 文档里的 `subvideo sync … --json` 直接不可用 | `cli.add_global_aliases()`：子命令上也挂同名开关，`default=argparse.SUPPRESS` 保证不覆盖主解析器的值（`CliSurfaceTest` 锁死前后两种写法） |
| 2 | `pyproject.toml` 版本停在 `0.1.0`，与 `subvideo.__version__`（0.3.0）不一致 | 打包/安装元数据与实际版本不符 | 统一为 `0.4.0`，并加测试断言两者一致 |
| 3 | `check` 报告里写着"阶段 3" | 交付版本自述过时 | 改为"阶段 4（稳定与交付；功能面同阶段 3）" |
| 4 | 没有配置模板漂移守卫 | 以后新增配置项可能忘写模板，用户看不到新开关 | `ConfigTemplateTest` 用显式字段↔模板映射锁死（含"模板里没有未知键"的反向检查） |
| 5 | `retry` 只挂了 `--steps`/`--force`，而文档给出的是 `retry --mmdc <路径>` / `retry --asr` | 这些"配置变更后只补做某一步"的命令直接不可用 | 抽出 `cli.add_override_flags()` 供 `sync`/`retry` 共用（筛选类开关仍只属于 `sync`），并加测试锁死 |

---

## 5. Windows 安装与使用说明的验证

README 的安装步骤不是凭印象写的，来源如下：

| README 内容 | 依据 |
| --- | --- |
| 依赖分组安装（`pip install -e ".[media|asr|summary]"`） | `pyproject.toml` 的 `optional-dependencies`；阶段 1 核心为纯标准库 |
| FFmpeg：`winget install Gyan.FFmpeg` | `deps.INSTALL_HINTS["ffmpeg"]`（`check` 报告同一条命令） |
| Node.js + `npm install -g @mermaid-js/mermaid-cli` | `INSTALL_HINTS["mmdc"]`；阶段 0 第 3 节记录的 Puppeteer/Chromium 安装期风险 |
| 可选 ASR：`pip install -e ".[asr]"` | `INSTALL_HINTS["faster_whisper"]`；默认关闭、未启用不下载模型 |
| 环境变量清单 | `credentials.py`（`SUBVIDEO_COOKIE`/`BILI_COOKIE`/`SUBVIDEO_UID`/`SUBVIDEO_LLM_API_KEY`/`OPENAI_API_KEY`）、`config._apply_env`（`SUBVIDEO_OUTPUT_DIR`/`SUBVIDEO_LLM_BASE_URL`/`SUBVIDEO_LLM_MODEL`/`SUBVIDEO_MMDC`/`SUBVIDEO_REQUEST_INTERVAL`） |
| 退出码表 | `errors.EXIT_*` 与 `cli` 的返回路径（含 130 = Ctrl+C） |
| 故障排查表 | 各阶段记录里的 `reason` 取值（`dependency_missing`/`llm_not_configured`/`no_transcript`/`permission_preview_only` 等） |
| 已知限制 | 第 6 节汇总 |

`tests/test_delivery.py` 会持续校验"README 提到的开关存在、必需组件都覆盖、退出码都记录"，
避免文档与代码各自演化。

---

## 6. 已知平台限制清单（README 第 8 节的来源）

| # | 限制 | 来源 |
| --- | --- | --- |
| 1 | 已删除与无权限同为 `62002`，接口层不可区分 | 阶段 0 第 3.6 节、阶段 1 第 8 节 |
| 2 | 充电视频无权限时接口不报错，只有试看片段 | 阶段 0 第 3.3.4 节、阶段 1 第 8 节 |
| 3 | 充电条目仅部分可从列表发现（公开列表与充电子集是不同翻页窗口） | 阶段 1 第 7 节第 2 条 |
| 4 | 未登录时字幕接口静默返回空数组 | 阶段 0 第 8 节第 4 条 |
| 5 | 本样本 UP 的 44 篇专栏全是新格式 opus | 阶段 1 第 8 节第 2 条 |
| 6 | `max_pages` 默认 40，极端账号可能翻不完 | 阶段 1 第 8 节第 7 条 |
| 7 | 发现阶段取不到发布时间的条目不计入 N 候选 | 阶段 1 第 8 节第 8 条 |
| 8 | 真实第三方模型未实测 | 阶段 3 第 9 节第 1 条 |
| 9 | `mindmap.png` 真实渲染未实测 | 阶段 3 第 9 节第 2 条 |
| 10 | 联网视频下载与本地 ASR 端到端未实测 | 阶段 2 第 9 节第 1 条 |
| 11 | `openai` SDK 路径只被替身模块覆盖 | 阶段 3 第 9 节第 4 条 |
| 12 | 分块按字数而非 token | 阶段 3 第 9 节第 6 条 |
| 13 | `insufficient_text` 是终态 | 阶段 3 第 9 节第 8 条 |
| 14 | 无 ffmpeg 时只能下渐进式单流 | 阶段 2 第 9 节第 2 条 |
| 15 | `ffprobe` 不可用时降级为魔数校验 | 阶段 2 第 9 节第 3 条 |
| 16 | 依赖缺失时 `media`/`render` 每次 `sync` 重试一次 | 阶段 2 第 9 节第 9 条 |
| 17 | 动态长图超过 `max_height` 会截断（明确告警） | 阶段 2 配置与 `longimage.py` |

这 17 条都写进了 README 第 8 节，并区分"平台侧""本机未实测""工程限制"三类 —— 交付说明不承诺未验证的能力。

---

## 7. 授权在线验收清单（用户手动执行）

计划第 6.4 节：只使用**用户提供的有权账号与样本**，手动执行一次首次采集与一次增量重跑，
核对三类内容与本地目录、状态摘要，确认未访问无权内容。默认不在 CI 运行、不保存凭据。

```powershell
$env:SUBVIDEO_COOKIE = "SESSDATA=...; bili_jct=..."     # 只放环境变量，不入库
python -m tools.stage4.online_acceptance --uid <UID> --latest 3 --preflight   # ① 预检：确认会选中什么
python -m tools.stage4.online_acceptance --uid <UID> --latest 3              # ② 首次采集 + 增量重跑
python -m tools.stage4.online_acceptance --uid <UID> --latest 3 --report .test_tmp/stage4_online/report.json
```

工具会打印并落盘以下核对结果（凭据全程只经内存，报告文本统一过 `Redactor`）：

| 检查 | 通过条件 |
| --- | --- |
| 登录态与作者 | 能读到作者名与 mid |
| 首次采集产物 | 每个条目有 `metadata.json`；动态有 `content.md`、专栏有 `article.md`、视频有文字稿或**明确原因** |
| 增量重跑 | 首次 `done` 的条目在第二次**全部 skipped**；条目目录集合不变；`index.json` 键唯一 |
| 凭据 | 产物扫描 0 命中 |
| 退出码 | 0（全成功）或 4（有条目未完成，原因逐条记录） |

**本机未执行该在线验收**（没有可用账号的 Cookie，也不应在自动化流程里使用他人凭据）。
执行后把结论补进本节与 README 第 8 节即可。

---

## 8. 测试

| 文件 | 用例 | 本阶段变化 |
| --- | --- | --- |
| `tests/test_delivery.py` | 34 | **新增**（交付契约，见 4.3） |
| 其余 11 个测试文件 | 393 | 不变（阶段 3 基线） |
| 合计 | **427** | +34 |

离线测试全部使用替身（`FakeChatClient` / `fake_mmdc_runner` / `FakeDownloader` / 注入 `transport` /
假 `openai` 模块），**不联网、不调用真实模型、不运行 mermaid-cli**；真实样本检查只读 `.smoke_sync`。

---

## 9. 实现中新增的设计决策

1. **全局开关用 `SUPPRESS` 挂两份**：argparse 的子解析器会把自己的默认值整体复制回主命名空间，
   因此子命令上的同名开关必须用 `default=argparse.SUPPRESS`，否则会把主解析器解析到的值覆盖成 `False`。
2. **`FORMAT_VERSION` 不随阶段 4 升版本**：交付阶段不改产物结构；`IMPLEMENTED_STAGE` 也保持 3
   （它是"步骤实现到第几阶段"，不是项目阶段）。
3. **验收工具把"通过"和"待在线验收"分开**：`evidence` / `problems` / `pending` 三类，
   只有 `evidence` 非空才算 PASS —— 用替身通过不会写成真实链路通过。
4. **验收项到测试的映射写进代码并由测试锁死**：`SUITE_BY_CHECK` 里的名称若被重命名，
   `AcceptanceToolTest` 立刻失败，不会出现"验收工具悄悄少跑几条"。
5. **模板漂移守卫用显式映射**：`FIELD_TO_TEMPLATE` 把 57 个配置字段钉到 `[段] 键`，
   并反向检查模板里没有未知键（拼错的键在实现里是静默失效的）。
6. **离线验收不改归档目录**：需要新产物的检查（长图重渲染）一律写到 `--work`，
   归档目录保持"阶段 2/3 真实运行时的样子"，包括历史 `skipped` 状态。
7. **在线验收默认 `--latest 3` 且提供 `--preflight`/`--no-media`**：先看选中范围再决定是否采集，
   避免一次手滑把整站拉下来。
8. **README 的开关一致性由测试保证**：从 README 里抽出所有 `subvideo <子命令>` 命令行的 `--xxx`，
   与该子命令真实的开关集合比对 —— 文档写了不存在的开关就会失败。
9. **配置覆盖开关由 `sync` / `retry` 共用，筛选开关只属于 `sync`**：`retry` 的语义是
   "补做被配置变更影响的步骤"，所以 `--mmdc`/`--asr`/`--summary-*` 必须在它上面可用；
   而 `--from`/`--to`/`--latest`/`--types` 刻意不挂过去，避免"补做时又改选条目范围"的歧义
   （`CliSurfaceTest` 同时锁住"接受"和"不接受"两侧）。

---

## 10. 已知限制与偏差

| # | 限制 | 影响 | 处理 |
| --- | --- | --- | --- |
| 1 | 本机未配置云端模型、未装 mermaid-cli、未装 yt-dlp/FFmpeg/faster-whisper | 真实模型、真实 PNG 渲染、联网下载与 ASR **未实测** | README 第 8.2 节明示；第 7 节给出在线验收清单 |
| 2 | `.smoke_sync` 两条视频都是单 P | "真实多 P 下载"只有离线用例证据 | README 第 8.2 节第 3 条；在线验收时选一条多 P 视频即可覆盖 |
| 3 | `.smoke_sync` 的动态 `render.png` 由阶段 2 验证工具补生成，`metadata.json` 里 `render` 仍是 `skipped(stage2_not_implemented)` | 真实流程的 `render` 步骤在本机没有正式 `done` 记录 | 验收工具用"现场重渲染"补上真实链路证据；`retry --steps render` 可让流程正式补做 |
| 4 | 旧格式专栏（`read/cv`）分支只有离线样本 | 真实旧格式专栏未在线验证 | 阶段 1 第 8 节第 2 条已列；在线验收时换一位有旧格式专栏的作者 |
| 5 | `require.txt` 是明文 Cookie 的遗留路径 | 用户可能继续用它 | README 建议改放环境变量/`config.local.toml`；`.gitignore` 已覆盖，且它是最后兜底来源 |
| 6 | 未在 CI 上跑过（本仓库无 CI 配置） | 回归依赖手动执行 | README 第 11 节给出两条命令（全量测试 + 验收工具） |
| 7 | Python 版本只在本机 3.14.7 实测 | 3.11/3.12 未实测 | `requires-python >= 3.11`；阶段 0 的组件验证按 wheel tag 覆盖了 3.11+ |
| 8 | 验收工具的"待验"项不会自动重试 | 在线验收需人工执行 | 这是设计选择（计划第 6.4 节：在线测试默认不在 CI 运行、不保存凭据） |

---

## 11. 交付清单

```text
代码        subvideo/（阶段 1-3 功能，本阶段只改 cli 全局开关/覆盖开关与版本文案）
测试        tests/（427 例离线用例，其中 tests/test_delivery.py 34 例为交付契约）
工具        tools/stage4/acceptance.py（离线逐条验收）
            tools/stage4/online_acceptance.py（授权在线验收）
文档        README.md（安装/使用/限制，交付说明）
            docs/stage4-stability-and-delivery.md（本文件）
            docs/stage0-*.md、docs/stage1-*.md、docs/stage2-*.md、docs/stage3-*.md
配置        config.example.toml（只含占位符，覆盖 57 个配置字段）
需求/方案   REQUIREMENTS.md、DEVELOPMENT_PLAN.md（进度已更新为阶段 4 完成）

复现交付验证：
  python -m unittest discover -s tests -t .        # 427 例，OK
  python -m tools.stage4.acceptance                # 需求第 6 节：PASS 8 / FAIL 0 / SKIP 0
  python -m tools.stage4.online_acceptance --uid <UID> --latest 3   # 需用户 Cookie（第 7 节）
```
