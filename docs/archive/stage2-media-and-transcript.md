# 阶段 2 媒体与本地文字：实施与验证记录

状态：已完成（2026-09-23）
依据：[DEVELOPMENT_PLAN.md](../DEVELOPMENT_PLAN.md) 第 3.3、4、5、6 节 · [REQUIREMENTS.md](../REQUIREMENTS.md) 第 3.2.2、3.3 节 · [阶段 0 平台验证](stage0-platform-verification.md) 第 3.3.3（字幕）、3.3.4（playurl 与充电权限）、3.7（图片）节 · [阶段 1 衔接点](stage1-basic-archive.md#9-阶段-2-衔接点)
复现：`python -m unittest discover -s tests -t .`（离线 **291** 用例，约 35s）· `python -m subvideo check --uid <UID>` · `python -m tools.stage2.render_check`
产物：`subvideo/{deps,media}.py`、`subvideo/transcript/`、`subvideo/render/`（实现）· `tests/test_{media,transcript,render,render_wiring}.py`（测试）· `tools/stage2/render_check.py`（验证工具）· `.smoke_sync/`（联网冒烟产物，`.gitignore` 已排除）

---

## 1. 交付内容

```text
subvideo/
  deps.py                        依赖探测（yt-dlp / ffmpeg / ffprobe / Pillow / faster-whisper）
  media.py                       视频分 P 下载：Downloader 协议 + yt-dlp 适配 + 并发/续传/校验
  transcript/
    __init__.py                  编排：字幕优先 → ASR 兜底 → 跨 P 合并落盘
    srt.py                       片段模型、时间戳、SRT/纯文本序列化（纯函数）
    subtitle.py                  字幕列表 → 选轨 → 下载 → 解析 → 单 P 落盘
    asr.py                       ffmpeg 提音频 + faster-whisper 惰性适配（默认关闭）
  render/
    __init__.py                  对外导出（模块顶层不 import PIL）
    fonts.py                     中文字体探测（msyh.ttc → Deng.ttf → simhei.ttf → …）
    longimage.py                 单张长图排版与渲染（中文逐字换行、高度随内容增长）
tools/stage2/render_check.py     离线验证：真实归档产物 → 长 PNG
```

命令面（计划第 4 节，新增项加粗）：

```powershell
python -m subvideo check  [--scan-output]                 # 依赖/配置/登录态/产物凭据自检
python -m subvideo sync   --uid 123456 --from 2026-01-01 --to 2026-09-23
python -m subvideo sync   --uid 123456 --latest 20 --asr --video-workers 3
python -m subvideo sync   --uid 123456 --types video --quality 720p --segment-workers 8
python -m subvideo sync   --uid 123456 --no-media --no-render          # 只要元数据与文字
python -m subvideo retry  --uid 123456 --steps transcript,render
```

新增配置段：`[transcript] prefer_subtitle`、`[download] media/video_workers/segment_workers/quality/timeout_seconds/ffmpeg_path/ffprobe_path`、`[asr] language/beam_size/keep_audio`、`[render] dynamic_png/width/max_height/font`。模板 `config.example.toml` 已同步，只含占位符。

---

## 2. 阶段 2 完成门槛对照（计划第 5 节）

| 计划门槛 | 结果 | 证据 |
| --- | --- | --- |
| 多 P 与多图样本产物完整 | ✅ 离线：2 P 视频两个 `videos/P0N.mp4` 落盘且逐个校验；4 图动态长图 1080×8601 / 2.2 MB 含 4 张真配图 | 第 5.2、6 节 |
| 关闭识别不运行模型 | ✅ `--asr` 关闭时 `transcript` 记 `skipped(asr_disabled)`，`transcriber.calls == []`；`check` 显示"本地 ASR 关闭" | `test_offline_flow.py::test_no_media_flag_skips_media_step`、`test_asr_flow_*` |
| 失败后补做 | ✅ 部分 P 失败 → `media=failed(partial_download)`，重跑**只补失败的分 P**；缺依赖装好后 `retry` 成功 | `test_media.py::test_rerun_only_retries_failed_pages`、`test_offline_flow.py::test_media_partial_failure_then_retry_only_fills_missing`、`test_dependency_missing_then_install_and_retry` |
| 字幕（主路径） | ✅ 真实联网：900 段 / 9815 字、969 段 / 10771 字（与阶段 0 的 900 段样本吻合） | 第 4.2 节 |
| 本地 ASR（兜底） | ✅ 字幕缺失才触发；`test_subtitle_wins_over_asr` 证明有字幕的分 P **不会**跑 ASR | 第 6 节 |
| 动态单张长 PNG | ✅ 真实归档产物渲染 1080×8601；中文无方块、配图按内容宽等比缩放 | 第 5 节 |
| 阶段 1 遗留步骤被接管 | ✅ 阶段 1 的 `skipped: stage2_not_implemented` 被自动识别为"可重做"，`retry`/`sync` 直接补做 | 第 3 节、`test_offline_flow.py::test_stage1_stage2_not_implemented_is_taken_over` |

---

## 3. 阶段推进的接管机制（本阶段新增的关键设计）

阶段 1 把 `media` / `transcript` / `render` 记成 `skipped: stage2_not_implemented` 并**已结算**。
而 `Store.pending_steps` 原本把 `skipped` 一律视为终态 —— 若不处理，阶段 2 落地后
`retry` **永远不会**碰这些条目，能力升级对既有产物无效。

解决方式（`models.stale_skip_map`）：按**步骤**维护"哪些 `skipped` 原因现在应该重做"。

| 来源 | 例子 | 何时生效 |
| --- | --- | --- |
| 阶段推进 | `render/media/transcript` 的 `stage2_not_implemented` | `STEP_IMPLEMENTED_AT[step] <= IMPLEMENTED_STAGE` |
| 配置变化 | `media_disabled`、`render_disabled`、`download_disabled`、`subtitle_disabled`、`asr_disabled` | 对应开关本次为开启 |

按**步骤**而不是按原因匹配，是因为同一个原因串在不同步骤下语义不同：`no_transcript`
对 `transcript` 是终态（ASR 跑过了确实没文字），对 `summary`/`mindmap` 却是"等阶段 3
接管"，只有阶段 3 落地后才应重做。

**明确排除**终态跳过，避免 `retry` 反复打接口：`permission_preview_only`（权限不会因为
重跑而变好）、`content_blocked`、`no_media`、`no_speech`、`no_images`。

阶段 3 落地时只需把 `IMPLEMENTED_STAGE` 改成 3（并在需要时补一条 `no_transcript` 规则）。

---

## 4. 文件与状态契约

### 4.1 步骤状态

| 类型 | 步骤 | 阶段 2 状态 |
| --- | --- | --- |
| 动态 | `fetch` → `content` → `images` → `render` | **全部实现**（`render` 产出 `render.png`） |
| 专栏 | `fetch` → `content` → `images` | 全部实现（无变化） |
| 视频 | `fetch` → `media` → `transcript` → `summary` → `mindmap` | 前三步实现；后两步记 `skipped(stage3_not_implemented)` |

视频两步的跳过原因（都会写进 `metadata.json`，可被 `retry` 区分处理）：

| 步骤 | 原因 | 含义 | 可重做 |
| --- | --- | --- | --- |
| `media` | `permission_preview_only` | 充电视频无完整权限（只有试看） | ❌ 终态 |
| `media` | `media_disabled` | 用户关闭了媒体下载 | ✅ 开启后 |
| `media` | `no_pages` | 分 P 列表为空 | ❌ |
| `media` | `partial_download` / `download_failed` | 部分 / 全部下载失败（`failed`） | ✅ 天然 |
| `transcript` | `asr_disabled` | 无平台字幕且 ASR 关闭 | ✅ 开启 `--asr` 后 |
| `transcript` | `subtitle_disabled` | 用户关闭了字幕提取 | ✅ 开启后 |
| `transcript` | `no_media` | 上游没有媒体文件（权限门控等） | ❌ 终态 |
| `transcript` | `no_transcript` | ASR 跑完确实没有文字 | ❌ 终态 |
| `transcript` | `dependency_missing` / `transcript_failed` | 缺 faster-whisper / ffmpeg，或转写失败（`failed`） | ✅ 装好后 |
| `render` | `render_disabled` | 用户关闭了长图 | ✅ 开启后 |
| `render` | `upstream_blocked` | 正文被门控，无内容可渲染 | ❌ 终态 |
| `summary`/`mindmap` | `stage3_not_implemented` | 阶段 3 未实现 | ❌（阶段 3 接管） |
| `summary`/`mindmap` | `no_transcript` | 缺少文字来源（需求 3.3.6 要求的显式标记） | ❌（阶段 3 接管） |

### 4.2 真实产物结构（联网冒烟）

```text
.smoke_sync/1039025435_战国时代_姜汁汽水/
  2026-09-16_视频_预防式加息 vs 新加息周期；…_BV1zveP6eEZ1/
    metadata.json                 8125 B（含 extra.media / extra.transcript / extra.access）
    transcript_timed.srt         66693 B  合并带时间戳（每条前缀 [P01]，P 边界有标记 cue）
    transcript.txt               29170 B  合并纯文本（按 P 分节，标注来源与字数）
    transcript/
      P01.subtitle.json         150314 B  平台字幕原文（CDN 原始 JSON，供溯源与重做）
      P01.srt                    60996 B  单 P 带时间戳（可直接配合 videos/P01.* 使用）
      P01.txt                    28802 B  单 P 纯文本
```

动态条目新增 `render.png`（单张长图）；视频条目新增 `videos/P01.mp4`（媒体）。

### 4.3 时间基准的取舍

需求明确"各 P 媒体文件保存在同一文件夹内，**不强制拼接**为单个视频文件"，因此合并文件
**保留各 P 内部的原始时间戳**（可直接对 `videos/P01.mp4` 定位），而不是累加成一条虚构的
连续时间轴。P 号与时间基准同时写在：

1. 每条 cue 的 `[P01] ` 前缀；
2. 每个 P 边界的一条标记 cue：`【P01】<分P标题> · 来源 平台字幕（中文） · 时间基准：本 P 起点 00:00:00,000（时间戳相对 videos/P01.*，各 P 未拼接）`；
3. `metadata.extra.transcript.time_base = "per_page"`。

---

## 5. 真机验证：动态单张长图（需求 3.2.2）

用**真实归档产物**（真中文长文 + 5 张真配图 + 真头像）离线渲染，命令与结果：

```powershell
python -m tools.stage2.render_check
```

| 指标 | 值 |
| --- | --- |
| 条目 | `2026-09-22_动态_最近这个评论区给我整不会了。…_1250848386750349380` |
| 作者 / 发布 | 战国时代_姜汁汽水 / 2026-09-22 18:50:35 |
| 渲染项 | 22 个（含 4 张真配图） |
| 产物 | `render.png` **1080 × 8601**，2,212,283 字节（≈2.2 MB），PNG / RGB |
| 配图 | 渲染 4 张 / 缺失 0 张；按内容宽度等比缩放，顺序与正文一致 |
| 截断 | `clamped=False`（未触及 `max_height=20000`） |

目视检查（裁切上下两段逐字核对）：UP 名称、头像（圆形）、发布时间、`充电专属` 胶囊、
点赞/评论/转发、加粗标题、`## 正文` 小标题、中文正文**逐字换行且无方块**、标点正确、
配图完整未裁切。

渲染器自身的 28 个用例另含：中文非方块（非白像素计数 + 不同文本像素不同）、
高度随内容增长、图片缺失/损坏走占位框、`max_height` 触顶时 `clamped=True` 且 message 明说截断、
Pillow 缺失时返回 `dependency_missing` 而**不抛异常**（有单测扫描源码守住"模块顶层不 import PIL"）。

---

## 6. 联网冒烟验证（2026-09-23，目标 UID 1039025435）

环境：Python 3.14.7 / Windows；凭据来自本地 `require.txt`（登录 mid 914754）；请求间隔 1.2s。
本机**未安装** `yt-dlp` / `ffmpeg` / `faster-whisper`（Pillow 12.3.0 已装）——这恰好验证了
"缺依赖如实报错、可补做"的路径。

| 运行 | 命令要点 | 结果 |
| --- | --- | --- |
| A 环境体检 | `check --uid 1039025435` | 登录态有效；依赖表逐项列出缺失项与**安装命令**；中文字体 6 个候选 |
| B 接管 + 字幕 | `sync --types video --latest 2` | **接管阶段 1 遗留的 2 条视频**；发现 35 条；15 次请求 / 0 风控；2 条各产出 900 段/9815 字与 969 段/10771 字平台字幕 |
| C 重跑 | 同 B | `media` 仍 `failed(dependency_missing)`（依赖没装，如实重试）；字幕未重复下载 |
| D 长图 | `render_check` | 见第 5 节 |

运行 B/C 的关键输出（节选）：

```text
~ [video] 预防式加息 vs 新加息周期；… → partial（1 P；媒体 成功 0 / 失败 1；文字 1/1 P 有文字（平台字幕），900 段 / 9815 字）
· 缺失依赖（对应步骤会记 failed(dependency_missing)，装好后 retry 可补做）：
  yt-dlp（pip install "subvideo[media]"）、ffmpeg（winget install Gyan.FFmpeg）、…
· 本地 ASR 关闭：无平台字幕的分 P 不产生文字稿（开启 --asr 后可 retry 补做）
```

退出码 4（运行完成但有条目未完成），与"媒体缺依赖"一致。

> **诚实说明**：本机没有 `yt-dlp` / `ffmpeg`，因此**视频媒体下载与本地 ASR 的联网路径未在本机
> 端到端跑通**。这两条路径的覆盖来自：① 离线替身测试（29 + 52 用例，含并发上限、续传标记、
> 产物校验失败、只补失败分 P）；② 阶段 0 已实测的 `playurl` 形态与权限判定；③ yt-dlp / ffmpeg
> 的命令行参数按官方文档构造。**交付前需在一台装好 FFmpeg 的机器上做一次授权在线验收。**

---

## 7. 测试

`python -m unittest discover -s tests -t .` → **291 用例，全部通过**（约 33s）。阶段 1 为 155 例，本阶段新增 136 例。

| 文件 | 用例 | 覆盖点 |
| --- | --- | --- |
| `test_media.py` | 29 | 清晰度别名与 `format` 选择（无 ffmpeg 时禁选 DASH 分离流）、错误文本分类、容器魔数、`probe_media` 六条分支（缺失/空/过小/魔数通过/非媒体/ffprobe 成功·无流·失败降级·抛异常降级）、并发上限、续传标记、幂等跳过、只补失败分 P、依赖缺失、下载器抛异常不中断整批 |
| `test_transcript.py` | 53 | 时间戳往返、`body[]` 解析、SRT 序列化/前缀/标记 cue/连续序号/往返解析、段落切分、字幕选轨（优先级/锁定/回退）、两种包裹形状、字幕获取五条失败分支、ffmpeg 提音频（缺依赖/缺源/成功/失败/抛异常）、转写器缺依赖、`build_transcript` 九种组合、合并产物、签名地址不落盘 |
| `test_render.py` | 28 | 渲染器自身（换行算法、字体回退、端到端 PNG、高度增长、占位框、`max_height` 截断、Pillow 缺失、中文确实被绘制） |
| `test_render_wiring.py` | 10 | **真实阶段 0 响应**驱动 `_render_items`：正文/图片顺序、缺失图占位、转发章节、端到端 PNG |
| `test_offline_flow.py` | 33（阶段 1 为 17，本阶段 +16） | 新增 `Stage2FlowTest`（13 例）：字幕流、ASR 流、`--no-media`/`--no-render`、开关打开后重做、阶段接管、权限终态不重试、部分失败补做、缺依赖补做、ASR 失败可重试、`media_bytes` 计入摘要、文字稿产物与 metadata 无凭据/无签名地址；`StaleSkipMapTest`（3 例） |

离线测试全部使用替身（`FakeApi` / `FakeDownloader` / `FakeTranscriber` / 假 ffmpeg 执行器），
**不联网、不跑模型、不调用真实 ffmpeg**。

---

## 8. 实现中新增的平台认知与设计决策

1. **阶段 0 证据文件是"摘要化"的**：`stage0/evidence/raw/*.json` 里长数组被替换成
   `"."` / `"...(共 N 项)"` 占位（例：`dynamic_detail__auth.json` 的 `opus.pics` 是
   `[".", ".", "."]`）。它们适合做**字段路径**参照，不能直接当完整 fixture 用；
   涉及数组内容的测试需自行补数据。
2. **字幕是绝对主路径**：真实联网两条视频都有 `ai-zh` 轨（900 / 969 段），与阶段 0 的
   "前 10 条全部带 AI 中文字幕"一致。因此 ASR 默认关闭是正确的默认值。
3. **"没有字幕"和"取字幕失败"必须分开**：前者是良性跳过（`no_subtitle` → 可开启 ASR
   补做），后者是硬失败（`failed`，可 `retry`）。早期实现把两者都吞成 `skipped`，
   已修正 —— 否则字幕接口临时失败会被静默当成"这视频没字幕"。
4. **ASR 失败的三分类**：`no_media`（上游没给媒体，终态）、`no_speech`（识别跑完确实没人声，
   终态）、`transcript_failed`（转写报错，可重试）。混在一起会让 `retry` 无限重跑无声视频。
5. **产物校验不能只看"下载成功"**：yt-dlp 报告成功也可能落下一个 HTML 错误页。因此
   `media` 步骤在下载后统一过 `probe_media`：有 `ffprobe` 读真实流信息；没有则降级为
   "大小阈值 + 容器魔数"，并把 `verify_note` 写成"未使用 ffprobe 校验（不可用）；…可播放性
   未经解码验证" —— **不把魔数校验说成"已验证可播放"**。
6. **没有 ffmpeg 时不能选 DASH 分离流**：选了也合流不了。`build_format_selector` 在无
   ffmpeg 时退化为渐进式单流，并把"清晰度可能更低"写进运行摘要，不做静默降级。
7. **权限判定优先于下载开关**：`--no-media` 时若该视频本就无权限，仍记
   `denied`（内容不可访问这个事实与开关无关），否则会把无权限的充电视频报成 `done`。
8. **`resumed` 必须在下载前判断**：yt-dlp 完成后会删掉 `.part` 残片，下载后检查永远是 `False`。
9. **媒体字节数单独统计**：yt-dlp 不走 `HttpClient`，因此 `ArchiveResult.media_bytes`
   单独累计进 `RunSummary.media_bytes`，摘要里显示"另媒体 X MiB"。
10. **依赖全部惰性导入**：`import subvideo` 不因缺 yt-dlp / Pillow / faster-whisper 而失败；
    `check` 统一从 `deps.probe_dependencies()` 出报告，附可照做的安装命令。
11. **长图不做静默截断**：超 `max_height` 时 `clamped=True` 且 message 明说"已截断；正文可能
    不完整"，同时写进运行摘要。
12. **字幕的 `subtitle_url` 是临时签名地址，绝不落盘**：它是
    `https://aisubtitle.hdslb.com/…?auth_key=<签名>-…` 形式的限时凭据（阶段 0 第 3.3.3 节
    记录的形态）。计划 3.2 明确"metadata 不含临时下载 URL"，因此 `PageTranscript.subtitle_url`
    只保留在内存里供日志使用，`to_json()` 不输出；原始字幕内容已存进
    `transcript/P0N.subtitle.json`，重做时重新调接口取即可。有测试锁死这条
    （`test_signed_subtitle_url_is_never_persisted` + 离线流程里对 metadata 的字符串断言）。
13. **`format_version` 升到 2**：阶段 2 在 `extra` 里新增 `media` / `transcript` / `render`
    三组结构。新增字段都是**附加**的，阶段 1 的产物无需迁移（读取侧按缺省值兜底，
    `Store.rebuild` 也不校验版本）。

---

## 9. 已知限制与偏差

| # | 限制 | 影响 | 处理 |
| --- | --- | --- | --- |
| 1 | 本机无 `yt-dlp`/`ffmpeg`，视频媒体下载与本地 ASR 的**联网端到端未跑通** | 这两条路径只有离线替身证据 | 第 6 节已明示；交付前需在装好 FFmpeg 的机器上做一次授权在线验收 |
| 2 | 无 ffmpeg 时只能下渐进式单流 | 清晰度低于 DASH 分离流 | 运行摘要明确说明"安装 FFmpeg 后可 retry 以更高清晰度补做" |
| 3 | `ffprobe` 校验不可用时降级为魔数校验 | 只能证明"结构上像媒体" | `verify_note` 如实写明"可播放性未经解码验证"，不冒充已验证 |
| 4 | 合并 SRT 的每条 cue 带 `[P01]` 前缀 | 文本比原文多一个前缀 | 这是需求"标记 P 号"的直接实现；单 P 的 `transcript/P01.srt` 无前缀，可直接配播放器 |
| 5 | 长图渲染器 `scale` 参数仅支持 1 | 预留参数，传其他值按 1 处理 | 契约里本就是预留；当前按 `width` 直接渲染已满足需求 |
| 6 | 长图配图"只缩不放" | 比内容宽窄的小图保持原始像素居中 | 避免小图被放大糊掉；如需严格铺满内容宽，改 `longimage.py` 一处即可 |
| 7 | ASR 中间音频默认转写完即删 | 重跑 ASR 需重新提音频 | `[asr] keep_audio=true` 可保留 |
| 8 | `62002` 仍不可区分"已删除 / 无权限" | 沿用阶段 0 结论 | 状态里原样记录错误码，不写成"权限不足" |
| 9 | 依赖缺失时 `media` 记 `failed` | 每次 `sync` 都会重试一次该步骤 | 这是"可补做"的正确语义；`check` 会提前给出安装命令 |
| 10 | 中文长文合并后段落较长（字幕本身无句读边界） | 可读性一般 | 按 220 字 / 2s 停顿 / 60s 跨度切段；字幕片段本就无标点，不做臆测补标点 |

---

## 10. 阶段 3 衔接点

阶段 2 已把阶段 3 需要的输入全部留在 `metadata.json` 里：

| 阶段 3 工作 | 阶段 2 已备好的数据 |
| --- | --- |
| 分段总结（长稿不丢尾段） | `transcript.txt`（按 P 分节）+ `transcript_timed.srt`（带时间戳）+ `extra.transcript.chars/segments` |
| 自定义 prompt / 模型指纹 | `config.summary_*` 已就绪；`extra.transcript` 记录来源，便于只重做总结而不动文字稿 |
| `.mmd` 与 PNG 导图 | `extra.transcript.sources` 标注字幕/ASR 来源；有文字才有导图，缺文字时 `summary`/`mindmap` 已显式记 `no_transcript` |
| 独立重试 | `retry --steps summary,mindmap`；阶段 3 落地时把 `IMPLEMENTED_STAGE` 改为 3 即可让历史 `stage3_not_implemented` 自动变为可重做 |

→ 阶段 3 已落地（`IMPLEMENTED_STAGE = 3`），实施与验证记录见
[阶段 3 总结与思维导图](stage3-summary-and-mindmap.md)；本节列出的输入数据在实际实现中全部用上了。

---

## 11. 运行与退出码

```powershell
# 1) 准备配置：把 Cookie 放到环境变量或 config.local.toml（不入库）
$env:SUBVIDEO_COOKIE = "SESSDATA=...; bili_jct=..."
Copy-Item config.example.toml config.toml

# 2) 先体检（会报告 yt-dlp / ffmpeg / faster-whisper / Pillow 的可用性与安装命令）
python -m subvideo check  --uid 1039025435

# 3) 采集：媒体 + 字幕（ASR 默认关闭）
python -m subvideo sync   --uid 1039025435 --latest 20
python -m subvideo sync   --uid 1039025435 --types video --quality 720p
python -m subvideo sync   --uid 1039025435 --types dynamic --no-images   # 只要长图不下载配图

# 4) 开启本地 ASR（仅对无平台字幕的分 P 执行，不调用云端识别）
python -m subvideo sync   --uid 1039025435 --asr --asr-model medium

# 5) 补做（只做未结算的步骤；阶段 1 遗留的步骤也会被接管）
python -m subvideo retry  --uid 1039025435
python -m subvideo retry  --uid 1039025435 --steps transcript,render

# 6) 离线验证动态长图排版
python -m tools.stage2.render_check --list
python -m tools.stage2.render_check
```

| 退出码 | 含义 |
| --- | --- |
| 0 | 全部成功（含幂等跳过） |
| 1 | 未预期错误（带 `-v` 打印堆栈） |
| 2 | 配置/参数/输出目录错误 |
| 3 | 缺少凭据或登录态失效 |
| 4 | 运行完成但有条目失败/权限不足/部分完成（摘要给出定位信息） |
| 5 | 遇到风控且退避无效，已停止（不绕过，提示人工处理） |
| 130 | 用户中断（已完成产物保留，可 `retry` 补做） |


