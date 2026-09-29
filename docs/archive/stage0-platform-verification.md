# 阶段 0 平台可行性验证记录

状态：已完成（待与需求就"专栏"偏差对齐）
日期：2026-09-23
依据：[DEVELOPMENT_PLAN.md](../DEVELOPMENT_PLAN.md) 第 2.1、5 节 · [REQUIREMENTS.md](../REQUIREMENTS.md) 第 3、5 节
证据：[`stage0/evidence/RESULTS.md`](../stage0/evidence/RESULTS.md)（自动生成）· [`stage0/evidence/raw/`](../stage0/evidence/raw/)（逐探针脱敏原始响应）
复现：`python tools/stage0/probe.py`

---

## 1. 结论摘要

| 内容类型 | 列表可发现 | 详情可读取 | 判定 |
| --- | --- | --- | --- |
| **视频（公开）** | ✅ `arc/search` 授权下稳定，按发布时间严格降序，`page.count` 准确 | ✅ `view` + `pagelist` 取分 P | **通过** |
| **视频（充电专属）** | ✅ 公开列表里就带 `is_charging_arc` 标记；`special_type=charging` 另有子集列表 | ✅ 授权账号可拿到 12 路 dash 流（**确有充电权限**） | **通过** |
| **动态（图文 / 转发 / 视频卡 / 问答卡）** | ✅ `feed/space` 游标分页；`opus/feed/space?type=all` 覆盖图文 | ✅ `detail` / `opus/detail` 可取正文、图片、发布时间 | **通过** |
| **动态（充电专属）** | ⚠️ 条目与 ID 可见，但 badge 标记与列表正文对未授权被剥离 | ⚠️ 未授权时正文被门控（`MODULE_TYPE_BLOCKED`）；授权后完整可读 | **通过（须授权）** |
| **专栏（新格式 opus 专栏，本项目样本全部 44 篇）** | ✅ `opus/feed/space?type=article` 翻页得 44 条，与计数接口 `count=44` 完全一致 | ✅ `opus/detail?id=` 取 `paragraphs[]` 正文与图片 | **通过** |
| **专栏（旧格式 read/cv 专栏）** | ✅ `/x/space/wbi/article` 的 `articles[]` | ✅ `/x/article/view?id=` 返回 HTML 正文 + `image_urls` | **通过** |

**阶段 0 判定：三类内容全部通过，可以进入阶段 1。**

> 专栏一节在首轮验证中被误判为"不可得"（原因见 3.5.2：比对 `type` 参数时只看条目数、没看首条 ID）。用户提供的 `/opus/` 样例触发了复核，纠正后 44 篇专栏全部可发现、可读取。**首轮结论已被推翻，本记录保留纠错过程。**

---

## 2. 验证环境与方法

### 2.1 环境

| 项 | 值 |
| --- | --- |
| 目标 UP 主 | UID `1039025435`，名称「战国时代_姜汁汽水」，粉丝 819,256 |
| 归档账号 | 登录 UID `914754`（`vipStatus=0`，非大会员） |
| Cookie 来源 | 本地 `require.txt`（22 个字段，含 `SESSDATA`/`bili_jct`/`buvid3`/`bili_ticket`） |
| Python | 3.14.7（Windows） |
| 网络 | 经本地 HTTP 代理；`curl.exe` 的 schannel 栈不可用，Python 侧 TLS 正常 |
| 探针数 | 65 个（含 18 组三身份矩阵） |

> **凭据卫生**：`require.txt` 含真实 Cookie 且位于项目根目录。它**未**被写入任何产物（探针末尾有一道"产物中出现凭据值即报错退出"的校验）。建议尽快改名/移动到 `config.local.toml` 并加入 `.gitignore`，见第 7.3 节。

### 2.2 身份矩阵方法

同一接口分别用三种身份请求，把"Cookie 到底扩大了多少可发现/可读取范围"量化出来，而不是只测一次成功：

| 身份 | Cookie | 用途 |
| --- | --- | --- |
| `auth` | 完整登录 Cookie | 产品实际运行态 |
| `anon_buvid` | 仅 `buvid3`/`buvid4`（由 `/x/frontend/finger/spi` 获取） | 真实匿名 Web 会话的最小指纹 |
| `anon` | 无 | 纯匿名对照 |

完整矩阵见 [`RESULTS.md`](../stage0/evidence/RESULTS.md) 首表。

---

## 3. 逐项验证结果

### 3.1 登录态（计划 2.1-1、2.1-4）

| 探针 | 结果 |
| --- | --- |
| `/x/web-interface/nav`（授权） | `code=0`，`isLogin=true`，`mid=914754` |
| `/x/web-interface/nav`（匿名） | `code=-101`，`isLogin=false`，**但仍下发 `wbi_img` 密钥** |
| 伪造 `SESSDATA=deadbeef` | `code=-101`，与匿名完全一致 |

**结论**：登录态检查用 `/x/web-interface/nav`，判据是 `code==0 && data.isLogin`。未登录时该接口仍返回 wbi 密钥，因此"取密钥"与"校验登录"必须是两件事——密钥可以在未登录状态下拿到，不能把"拿到密钥"当成"已登录"。Cookie 失效与未登录在接口层**不可区分**（同为 `-101`），产品提示语应合并表述为"登录态无效或已过期"。

### 3.2 作者信息（计划 2.1-1）

`/x/web-interface/card?mid=` 匿名即可读，返回名称、头像 `face`、`fans`、`sign`。

**结论**：输出目录命名 `<UID>_<清理后的UP名称>` 用该接口取名称即可，不需要登录，也不需要 wbi。`/x/space/wbi/acc/info` 匿名+buvid 会 `-352` 风控，**不采用**。

### 3.3 视频（计划 2.1-1、2.1-2、2.1-3）

#### 3.3.1 列表与分页

`/x/space/wbi/arc/search`（WBI 必需），`pn`/`ps` 分页 + `page.count` 给总数：

| 列表 | `page.count` | 第 1 页条目 | 排序 |
| --- | --- | --- | --- |
| 公开投稿（`order=pubdate`） | 211 | 30（其中 `is_charging_arc=true` **24 条**） | `created` 严格降序，含首条（本样本无置顶） |
| 充电专属（`special_type=charging`） | 88 | 30 | `created` 严格降序 |

第 1、2 页无重叠、时间区间单调衔接：第 1 页覆盖 `2026-09-16 20:31` → `2026-04-21 16:43`，第 2 页覆盖 `2026-04-20 10:27` → `2026-01-04 15:33`（两页之间 1.26 天无投稿，属正常间隔而非漏页）。分页行为正常，`order=pubdate` 可靠。两列表第 1 页交集 25 条、各 5 条互不包含——因为两者是"全部投稿"与"充电子集"两个不同的窗口，不是包含关系。

**关键结论：充电专属视频可以从公开投稿列表直接发现**，条目自带 `is_charging_arc`（布尔）与 `elec_arc_badge`（"充电专属"）字段。不需要"已知链接"才能访问，也不需要猜。`special_type=charging` 只是额外的子集视图。

> ⚠️ 匿名访问该接口**不稳定**：同一份代码在两次运行中分别得到 `code=-352`、`HTTP 412`、以及偶发的 `code=0` 正常结果（30 条）。**不能依赖匿名访问做发现**，必须使用登录 Cookie。

#### 3.3.2 详情与分 P

`/x/web-interface/view?bvid=` 免登录可用，`pages[]` 给出各 P 的 `cid`、`page`、`part`、`duration`；`/x/player/pagelist?bvid=` 返回一致。

本项目样本的视频均为单 P（`pages` 长度 1）。为覆盖需求 3.3.1 的"多 P 一个条目一个文件夹"，另外扫描了 22 个候选 BV 号找到多 P 样本 `BV1BqhB6nEdN`（`videos=4`）：

| P | cid | part | duration |
| --- | --- | --- | --- |
| 1 | 42082042159 | 《原神》角色预告-「沃雅妮莎：此夜共沦」 | 175s |
| 2 | 42082043958 | 日-《原神》角色预告… | 179s |
| 3 | 42081978254 | 英-《原神》角色预告… | 179s |
| 4 | 42082110816 | 韩-《原神》角色预告… | 179s |

各 P 有独立 `cid`、`part`、`duration`、`dimension`、`first_frame`。**结论：分 P 结构清晰，`cid` 是各 P 的唯一定位键，多 P 归档可按 `pages[]` 逐 P 处理。**

#### 3.3.3 字幕（计划 2.1-3）

`/x/player/wbi/v2?bvid=&cid=`（WBI）返回 `data.subtitle.subtitles[]`，每项含 `lan`、`lan_doc`、`subtitle_url`。

| 样本 | 授权 | 匿名+buvid |
| --- | --- | --- |
| 非充电公开视频 `BV1zqMC6LEmp` | 6 条（`ai-zh`/`ai-en`/`ai-ja`/`ai-es`/`ai-ar`/`ai-pt`） | **0 条** |
| 充电专属视频 `BV1zveP6eEZ1` | 2 条（`ai-zh`/`ai-en`） | **0 条** |

未登录时该接口返回**空数组而非错误**——这是一个静默失败点，适配层不能把"空字幕"直接当成"该视频没有字幕"，必须结合登录态判断。

拿到 `subtitle_url` 后，**字幕文件本体在 CDN 上匿名可下载**：实测 900 段 JSON，结构 `{from, to, sid, content, location, music}`，`from`/`to` 为秒（浮点）。这正是需求 3.3.2 要的"带时间戳的文字稿"。

**结论**：字幕是首选文字来源，成本远低于本地 ASR。本项目样本的视频（含充电专属）普遍带 AI 中文字幕，**多数视频不需要跑 ASR**。

#### 3.3.4 播放地址与充电权限（计划 2.1-3、2.1-4；风险表"视频格式与字幕获取差异"）

**必须先记录一条接口陷阱**：

| 变体 | 结果 |
| --- | --- |
| `/x/player/wbi/playurl`（WBI） | **恒为 `HTTP 412`**，授权与匿名皆然 |
| `/x/player/playurl`（非 WBI）+ `platform=pc` | ✅ `code=0`，`accept_quality=[80,64,32,16]`，dash 12 路视频 + 3 路音频 |

**适配层必须使用非 WBI 的 `/x/player/playurl`。** 这一条与旧工程 `E:\bilivideo\bilisum\download.py` 的做法一致，本次独立复现确认。

**这是判定"账号是否真有充电权限"的决定性证据**：

| 身份 | 充电专属视频 `BV1zveP6eEZ1` 的 playurl |
| --- | --- |
| 授权 | `code=0`，**dash 视频流 12 路**（正常正片） |
| 匿名+buvid | `code=0`，dash **0 路**，`durl` **1 段**（试看片段） |

两者 `code` 都是 0，**权限差异只体现在媒体流的数量与形态上，不体现在错误码上**。该账号确实拥有充电权限，充电视频可正常下载。

> **对适配层的要求**：判定"无权限"不能只看 `code`。应比对 `dash.video` 是否为空、`durl` 是否为单段，或直接看 `/x/web-interface/view` 的 `is_upower_exclusive` 与 `rights`。否则会把"试看片段"当成完整正片下载，产生静默的错误产物。

### 3.4 动态（计划 2.1-1、2.1-2、2.1-3）

#### 3.4.1 两个列表接口

| 接口 | 分页 | 每页 | 说明 |
| --- | --- | --- | --- |
| `/x/polymer/web-dynamic/v1/feed/space` | `offset` 游标 + `has_more` | 11–13 | 主动态流，含图文、视频卡、转发、问答卡 |
| `/x/polymer/web-dynamic/v1/opus/feed/space` | `page` + `offset` + `has_more` | 20 | 图文（opus）视图，覆盖充电专属图文 |

**`opus/feed/space` 的列表项不提供发布时间**（`pub_time` 为空字符串）。它只有 `opus_id`、`content`、`badge`、`cover`、`jump_url`、`stat`。而需求 3.1.1 的日期筛选与"合计最新 N 条"**必须有发布时间**，因此该列表不能单独用于筛选。

可行的发布时间来源（两条都已验证）：

1. `/x/polymer/web-dynamic/v1/feed/space` 的 `items[].modules.module_author.pub_ts`（Unix 秒）+ `pub_time`（"2026年08月10日 20:01"）；
2. `/x/polymer/web-dynamic/v1/opus/detail` 的 `data.item.modules[].module_author.pub_ts`。

旧工程另外使用 `www.bilibili.com/opus/{id}` 的 SSR `__INITIAL_STATE__` 取 `pub_ts`（抗风控但慢）。本次验证确认详情接口本身可用，**优先用接口，SSR 仅作为降级手段**。

#### 3.4.2 动态类型与字段路径（阶段 1 解析依据）

逐类型取样结果（`stage0/evidence/raw/dynamic_shapes.json`）：

| 动态类型 | `major.type` | 文本位置 | 图片位置 | 实测样本 |
| --- | --- | --- | --- | --- |
| `DYNAMIC_TYPE_DRAW` | `MAJOR_TYPE_OPUS` | `major.opus.summary.rich_text_nodes[].orig_text` | `major.opus.pics[]` | 144 字，`title="谨防诈骗"` |
| `DYNAMIC_TYPE_AV` | `MAJOR_TYPE_ARCHIVE` | `major.archive.title` + `desc` | `major.archive.cover` | `bvid`/`duration_text="33:00"` |
| `DYNAMIC_TYPE_FORWARD` | （空） | `module_dynamic.desc.text` | 转发的原文在 `orig` 字段 | 194 字 |
| `DYNAMIC_TYPE_COMMON_SQUARE` | `MAJOR_TYPE_COMMON` | `major.common.title` + `desc` | 无 | **充电专属问答卡**，`jump_url` 指向 `member.bilibili.com/mall/upower-manage/qa/detail` |

**两个必须记住的坑**：

1. `major.opus.summary` **不是字符串**，是富文本对象 `{rich_text_nodes:[{orig_text, text, ...}]}`。按字符串取长度会得到字典键数，是错的。
2. `/x/polymer/web-dynamic/v1/detail` 与 `/x/polymer/web-dynamic/v1/opus/detail` 的 **`modules` 形状不同**：
   - `detail` → `modules` 是 **dict**（键 `module_author` / `module_dynamic` / `module_more` / `module_stat`）；
   - `opus/detail` → `modules` 是 **list**（元素 `{module_type, module_content}`，正文在 `MODULE_TYPE_CONTENT.module_content.paragraphs[]`，`para_type=1` 为文字、`=2` 为图片）。

   同一个解析器必须同时兼容两种形状。

#### 3.4.3 充电专属动态的门控

`opus/feed/space` 在授权与匿名+buvid 下返回**完全相同的 20 个 `opus_id`**（交集 20，差集 0），但：

| 指标 | 授权 | 匿名+buvid |
| --- | --- | --- |
| 带 `badge`（"充电专属"）的条目 | 19 / 20 | **0 / 20** |
| 列表项正文长度 | 725 字 | **15 字** |
| `opus/detail` 的 modules | `MODULE_TYPE_CONTENT`（正文 725 字） | **`MODULE_TYPE_BLOCKED`**（正文 0 字） |
| `basic.is_only_fans` | `true` | `true` |

**结论**：充电专属动态的**条目 ID 对匿名可见，正文与标记被门控**。授权账号可完整读取。产品必须在"未授权"时明确标注为权限不足，而不是把 15 字的截断正文当成完整内容归档——这正好对应需求 3.1.4 与 3.1.5 的"区分未发现 / 无权 / 失败"。

`/x/polymer/web-dynamic/v1/opus/detail` 的参数名是 **`id`**，不是 `opus_id`（用 `opus_id` 返回 `code=-400`）。旧工程文档中记的 `opus_id=` 已失效，本次纠正。

### 3.5 专栏（计划 2.1-1、2.1-3）—— 通过（含一次结论修正）

> **本节记录了一次错误的发现与纠正，保留过程以便后续不再踩同一个坑。**

#### 3.5.1 纠错起点：用户提供的样例是 `/opus/` 而非 `/read/cv`

用户给出的专栏样例是 `https://www.bilibili.com/opus/1214741992578220039`。它的详情返回：

| 字段 | 值 |
| --- | --- |
| `item.type` | **1** |
| `basic.article_type` | **4** |
| `basic.is_only_fans` | `true` |
| 标题 | 地缘 + 金银 + 石油 + A股 |
| `paragraphs` | **181** 段（164 段文字 + 17 段图片） |
| 正文长度 | 5,698 字 |
| `pub_ts` | 1781667560（2026-06-17 11:39） |

正文开头自述"后面的内容是地缘部分，**先把前面的内容发专栏**……**文章发布在专栏**，有的用户可能看不到专栏"——**它确实是专栏，只是以 opus 格式发布**。

#### 3.5.2 纠错：`type=article` 参数是生效的（此前的判断是错的）

先前一轮探查把 `opus/feed/space` 的 `type` 参数判为"被忽略"，依据是四种取值"返回同一批条目"。**该判断错误**：当时只比对了条目数量与 `jump_url` 类型，没有比对首条 ID。逐项核对后：

| `type` 取值 | 首条 `opus_id` |
| --- | --- |
| `all` | 1250858063341027334 |
| **`article`** | **1214741992578220039**（用户样例） |
| `dynamic` | 1250858063341027334 |
| `video` | 1250858063341027334 |

`type=article` 的首条与其余取值**不同**。全量翻页核实：

| 检查 | 结果 |
| --- | --- |
| `type=article` 翻页 | 20 + 20 + 4 = **44 条**，去重 44，`has_more=false` |
| `/x/space/wbi/article` 报告的 `count` | **44** |
| 两者是否一致 | ✅ **完全一致** |
| 用户样例在列表中的位置 | **第 0 位（最新）** |
| `type=all` 前 60 条与专栏列表的交集 | **0** |

**结论：`/x/polymer/web-dynamic/v1/opus/feed/space?type=article` 就是该 UP 的专栏条目列表**，与 `type=all`（动态图文流）**互不重叠**，条目总数与计数接口完全吻合。

#### 3.5.3 两种专栏格式并存

B 站目前同时存在两种专栏承载格式，**必须两个来源都查、按 ID 去重**：

| 格式 | 列表来源 | 详情来源 | 条目形态 | 计数接口是否返回条目 |
| --- | --- | --- | --- | --- |
| **新格式（opus 专栏）** | `opus/feed/space?type=article` | `opus/detail?id=` | `item.type=1`，`basic.article_type ∈ {3,4}`，正文在 `paragraphs[]` | ❌ 只给 `count` |
| **旧格式（read/cv 专栏）** | `/x/space/wbi/article` 的 `articles[]` | `/x/article/view?id=<cv>` | `id` 是 cv 号，正文是 HTML | ✅ 返回 `articles[]` |

对照实验（同一个计数接口，不同作者；对照作者由搜索接口现场取样，因此每次运行可能不同）：

| 作者 | `count` | `articles` | `opus type=article` |
| --- | --- | --- | --- |
| **1039025435（本项目样本）** | 44 | **字段缺失** | ✅ 44 条 |
| 3706984593361427（某次取样） | 82 | 30 条（旧格式） | ✅ 20 条（新格式） |
| 121821849 | 3 | 3 条 | — |
| 34492979 / 494577671 | 1 / 21 | 1 / 21 条 | — |

**本项目样本的 44 篇专栏全部是新格式**（`item.type=1`，`article_type ∈ {3,4}`），所以计数接口只给 `count` 而不给 `articles[]`。这解释了此前的困惑：**不是"列表不可得"，而是"列表不在那个接口里"。**

注意最后一行：对照作者**同时**有旧格式 `articles[]` 和新格式 opus 专栏，说明两种格式在平台上并存，**不是新格式取代旧格式**。因此适配层必须两个来源都查。

旧格式详情接口仍然正常：`cv2504094` 返回 HTML 正文 6,500 字符、4 个 `<img>`、27 个 `<p>`，另有 `image_urls[]` 原图。

#### 3.5.4 专栏条目的门控与内容规模

| 指标 | 授权 | 匿名+buvid |
| --- | --- | --- |
| 用户样例的标题 / `type` / `article_type` | 均可见 | 均可见 |
| 用户样例的 `paragraphs` / 正文长度 | **181 段 / 5,698 字** | **0 段 / 0 字** |
| `is_only_fans` | `true` | `true` |
| 列表页 `badge`（"充电专属"）条目数 | 20 / 20 | **0 / 20** |

**充电专属专栏的条目元数据对匿名可见，正文被门控**（与 3.4.3 的动态行为一致）。

全量清单（44 篇逐条取详情，已落盘 `stage0/evidence/raw/article_opus_inventory.json`）：

| 指标 | 值 |
| --- | --- |
| 成功取到详情 | **44 / 44** |
| 充电专属（`is_only_fans=true`） | **43 / 44**（1 篇公开） |
| 段落数 | 中位 **51**，最大 **913** |
| 正文字数 | 中位 **3,164**，最大 **12,476** |
| 含图片段落的篇数 | 11 / 44 |
| 最新一篇 | `1214741992578220039`（用户样例，2026-06-17） |

**对阶段 3 的直接影响**：单篇专栏正文最长约 12,476 字，中位 3,164 字。**一篇长专栏就足以超出一般模型的舒适上下文**，计划 3.3 节"超出模型上下文时分块总结再汇总"不是可选项而是必需项。

#### 3.5.5 可靠性：`-352` 风控是间歇性的

同一批条目在不同轮次结果差异很大：

| 轮次 | 请求数 | 成功 | 失败 |
| --- | --- | --- | --- |
| 全量取详情（间隔 1.2s，重试 3 次） | 44 | 35 | **9 条 `-352`**（含用户样例） |
| 抽样取详情（间隔 0.9s） | 12 | **12** | 0 |
| 全量清单（间隔 0.9s，失败后隔 2s 重试一次） | 44 | **44** | 0 |

**用户样例在第一次请求时成功（181 段），第二次却返回 `-352`；而第三次又成功。** 这证明 `-352` 是**间歇性限流，不是永久失败**，且"失败后稍等再试一次"就能救回。

**对适配层的硬性要求**：专栏详情必须逐条记录状态、支持重跑补做，且**不能把 `-352` 写成终态失败**。这也印证了计划 3.2 节"失败步骤及其下游依赖可重试"的设计。

#### 3.5.6 仍未解决的一项（不阻塞阶段 1）

`/x/space/wbi/article` 的 `count` 字段语义**尚未完全确定**：对本项目样本 `count=44` 等于新格式专栏数，但对 mid=3706984593361427，`count=82`（旧格式 82 条）与其新格式专栏（≥20 条）之和不等于 82。

**处理方式**：适配层**不依赖 `count` 做分页终止判断**，而是以列表接口的 `has_more` 为准；`count` 仅作为交叉校验与告警（与实抓条目数不符时记入运行摘要）。这样即使 `count` 语义有变，也不会漏条目。

### 3.6 错误码可区分性（计划 2.1-4）

| 场景 | `code` | 含义 | 可否区分 |
| --- | --- | --- | --- |
| 未登录 / Cookie 失效 | `-101` | 账号未登录 | ✅ |
| 参数错误（BV 号格式非法、专栏 id 非数字） | `-400` | 请求参数错误 | ✅ |
| 资源不存在（`aid=99999999999`、专栏 id 不存在） | `-404` | 啥都木有 | ✅ |
| **稿件已删除 / 无权限** | **`62002`** | 稿件不可见 | ❌ **二者不可区分** |
| 风控（WBI 校验失败） | `-352` | 风控校验失败 | ✅ |
| 风控（HTTP 层） | `HTTP 412` | 无 JSON body | ✅ |
| **充电视频无权限** | **`code=0`** | 成功，但只有 1 段试看 `durl` | ❌ **错误码层面不可区分** |

两条**必须写进适配层契约**的结论：

1. `62002` 同时覆盖"已删除"和"无权访问"，**不能**据此断言是删除还是权限不足。需求 3.1.5 要求区分"无权查看 / 未发现 / 抓取失败"，在视频详情这一层做不到三分——只能在状态里如实记为"稿件不可见（62002）"，并保留条目与错误码。
2. 充电视频的权限不足**不报错**。必须按 3.3.4 的媒体流形态判定，否则会把试看片段当正片。

### 3.7 媒体可达性（计划 2.1-3："动态原图、专栏图片"）

| 项 | 字段路径 | 实测 |
| --- | --- | --- |
| 动态原图 | `opus/detail` → `MODULE_TYPE_CONTENT.module_content.paragraphs[].pic.pics[]`（`para_type=2`） | URL `http://i0.hdslb.com/bfs/new_dyn/…png`，**1388×1267**，字段 `{url, width, height, size, type}`；**匿名下载成功，437,124 字节，`image/png`** |
| 专栏图片 | `/x/article/view` → `data.image_urls[]`（原图）与正文 HTML 内联 `<img>` | URL `https://i0.hdslb.com/bfs/new_dyn/banner/…png`；**匿名下载成功，238,736 字节，`image/png`** |

**两个实现注意点**：

1. 动态原图 URL 返回的是 **`http://`**（非 https），专栏图片是 `https://`。适配层应统一升级为 `https://` 再下载，避免明文请求。
2. 图片 CDN **不校验 Cookie**，可以直接匿名下载；因此下载失败只可能是网络/风控/URL 过期，与登录态无关，错误分类时不要归因到权限。

---

## 4. 每类的可发现 / 可读取范围（计划第 5 节完成门槛）

### 4.1 明确可覆盖

- **视频**：列表（公开全集 + 充电子集）、分页、总数、发布时间、标题、封面、时长、分 P（已用 4 P 样本验证）、`is_charging_arc` 标记；详情元数据；播放地址（dash 多路流）；字幕列表与字幕文件本体。
- **动态**：图文 / 视频卡 / 转发 / 问答卡 / 充电专属图文的列表与详情；正文、原图（匿名可下载）、发布时间、转发原文。
- **专栏**：**新格式（opus）** 44 篇全部可发现（`type=article` 翻页，与 `count` 一致），详情含 `paragraphs[]` 正文与图片段落；**旧格式（read/cv）** 列表 `articles[]` 与 HTML 正文 + `image_urls` 原图（匿名可下载）。
- **作者**：名称、头像、粉丝数。
- **字幕覆盖率**：本项目样本公开列表前 10 条**全部**带 AI 中文字幕（`ai-zh`），**本地 ASR 在本样本上几乎不会被触发**。这直接影响阶段 2 的取舍：字幕解析是主路径，ASR 是兜底。

### 4.2 明确不可覆盖 / 受限

| 项 | 限制 | 证据 |
| --- | --- | --- |
| **已删除 vs 无权限** | 同为 `62002`，不可区分 | 3.6 |
| **充电视频无权限** | 不报错，需按媒体流形态判定 | 3.3.4 |
| **匿名访问 space 类接口** | `-352` / `HTTP 412` / 空列表 / 偶发成功，**结果不稳定** | 3.3.1、3.4.1 |
| **未登录取字幕** | 返回空数组而非错误，静默失败 | 3.3.3 |
| **`-352` 风控** | 即使授权身份、1.2s 间隔，44 次专栏详情请求仍有 9 次 `-352`；但同一条目换一次请求可成功 → **间歇性，非永久失败** | 3.5.5 |
| **空间页 SSR** | 无内嵌数据，SSR 路线不可行 | 3.5.1（旧记录） |
| **`opus/feed/space` 列表** | 不提供发布时间（`pub_time` 为空串），不能单独用于时间筛选 | 3.4.1、3.5 |
| **`/x/space/wbi/article` 的 `count`** | 语义未完全确定，不能用于分页终止判断 | 3.5.6 |
| **互动视频、直播回放** | 按需求第 7 节不纳入，本次未验证 | 需求 §7 |

---

## 5. 适配策略确认（计划 2.1-5）

### 5.1 接口白名单

适配层只允许调用下表中"已验证"的接口；业务代码不得直接拼 URL。

| 用途 | 接口 | WBI | 最低身份 |
| --- | --- | --- | --- |
| 登录态 / wbi 密钥 | `/x/web-interface/nav` | 否 | 匿名可读密钥 |
| 匿名指纹 | `/x/frontend/finger/spi` | 否 | 匿名 |
| 作者名片 | `/x/web-interface/card?mid=` | 否 | 匿名 |
| 视频列表 | `/x/space/wbi/arc/search` | **是** | **登录**（匿名不稳定） |
| 视频详情 / 分 P | `/x/web-interface/view?bvid=`、`/x/player/pagelist?bvid=` | 否 | 匿名 |
| 播放地址 | `/x/player/playurl`（**非 WBI**，`platform=pc`） | **否** | 登录（充电内容） |
| 字幕列表 | `/x/player/wbi/v2?bvid=&cid=` | **是** | **登录** |
| 字幕文件 | `subtitle_url`（`aisubtitle.hdslb.com`） | 否 | 匿名可下载 |
| 动态列表 | `/x/polymer/web-dynamic/v1/feed/space` | **是** | 登录 |
| 动态详情 | `/x/polymer/web-dynamic/v1/detail?id=` | **是** | 登录 |
| 图文列表 | `/x/polymer/web-dynamic/v1/opus/feed/space?type=all` | **是** | 登录 |
| 图文详情 | `/x/polymer/web-dynamic/v1/opus/detail?id=` | **是** | 登录 |
| **专栏列表（新格式）** | `/x/polymer/web-dynamic/v1/opus/feed/space?type=article` | **是** | 登录 |
| **专栏详情（新格式）** | `/x/polymer/web-dynamic/v1/opus/detail?id=` | **是** | 登录 |
| **专栏列表（旧格式）** | `/x/space/wbi/article?mid=` 的 `articles[]` | **是** | 登录 |
| **专栏详情（旧格式）** | `/x/article/view?id=` | 否 | 匿名 |
| 专栏计数（交叉校验） | `/x/space/wbi/article?mid=` 的 `count` | **是** | 登录 |

**明确不使用**：`/x/space/wbi/acc/info`（匿名 `-352`，改用 `card`）、`/x/player/wbi/playurl`（恒 412）、`/x/space/article`（旧计数接口，同样无条目）、空间页 SSR。

### 5.2 适配层必须实现的契约

1. **统一输出**：`kind` / `id` / `published_at` / `url` / `title` / `author` / `raw_ref`。`published_at` 内部用带时区的值；`created` / `pub_ts` 是 Unix 秒，目录日期按北京时间换算。
2. **身份**：所有列表与详情请求默认带登录 Cookie。适配层不提供"匿名模式"作为产品路径，因为匿名结果不稳定（3.3.1）。
3. **不把 `code=0` 当成"内容可读"**：充电视频必须检查媒体流形态；字幕必须结合登录态判断"空"的含义。
4. **状态三分类要如实**：`未发现`（列表里就没有）、`稿件不可见（62002）`（无法进一步区分删除/权限）、`抓取失败`（网络/风控/`-352`/`412`）。不得把 `62002` 写成"权限不足"。
5. **风控退避**：遇到 `-352` 或 `HTTP 412`，清空 wbi 密钥缓存、指数退避重试；重试仍失败则停止并提示人工处理，**不做验证码绕过或账号轮换**（需求 §5）。
6. **限速**：请求间隔 + 随机抖动；`opus/detail` 这类易风控接口默认串行。
7. **两种 `modules` 形状都要解析**（3.4.2）；`major.opus.summary` 按富文本解析（3.4.2）。
8. **发布时间来源优先级**：动态详情 `module_author.pub_ts` > 动态页 SSR（降级）。
9. **专栏必须查两个来源并去重**：`opus/feed/space?type=article`（新格式）与 `/x/space/wbi/article` 的 `articles[]`（旧格式）。判别位是 `item.type==1` / `basic.article_type != 0`（3.5.3）。
10. **`-352` 不得写成终态失败**：它是间歇性限流，同一条目换一次请求可成功。逐条记录状态，重跑补做（3.5.5）。
11. **不以 `count` 做分页终止**：一律以列表接口的 `has_more` 为准；`count` 只作交叉校验，不符时记入运行摘要（3.5.6）。

### 5.3 封装库选型结论

**采用自研薄适配层**，不引入 `bilibili-api-python`。理由：

- 本次验证所需的 14 个接口全部可用标准库 `urllib` + WBI 签名打通（`tools/stage0/client.py` 已实现限速、WBI、风控退避、错误分类），代码量约 250 行；
- 阶段 0 暴露的关键点（`playurl` 必须用非 WBI 变体、`opus/detail` 参数名是 `id`、`62002` 不可区分、充电权限靠媒体流形态判定）**都是封装库通常不承诺、也无法替我们兜住的语义**；
- 计划 2.1-5 要求"不把未验证接口散布到业务代码中"——白名单集中在适配层内部即可满足。

组件许可证与维护状态的核实结果见 [第 6 节](#6-组件选型验证)。

---

## 6. 组件选型验证

完整的许可证 / 维护状态 / Python 3.14 兼容性核查见 **[`docs/stage0-component-verification.md`](stage0-component-verification.md)**。要点：

| 组件 | 结论 |
| --- | --- |
| `yt-dlp` 2026.8.19 | ✅ Unlicense，纯 Python wheel，classifier 明确含 3.14 |
| `Pillow` 12.3.0 | ✅ MIT-CMU，已发布 cp314 win_amd64 wheel |
| `openai` 3.19.0 | ✅ Apache-2.0，3.14 无风险。**注意其依赖是 `httpx2` 而非 `httpx`** |
| `faster-whisper` 1.2.1 | ✅ MIT，风险低（ctranslate2 / av / onnxruntime 均有 cp314 win wheel） |
| `@mermaid-js/mermaid-cli` 11.17.0 | ✅ MIT，与 Python 无关；Windows 风险在 Puppeteer 的 Chromium 下载 |
| `imageio-ffmpeg` 0.6.0 | ⚠️ 自 2025-01-16 停滞；本机 ffmpeg 目前**只**由它间接提供 |
| `bilibili-api-python` 17.4.2 | ❌ **仓库已归档（2026-07-06）、LICENSE 被删除、收到 B 站侵权告知函** → 不采用 |
| ~~`paddleocr` / `paddlepaddle`~~ | ➖ **已取消该组件**（2026-09-23）：本地 OCR 移出范围，依赖链不再引入。原本的 Python 3.14 硬阻塞因此消失 |

**一项影响后续阶段决策的结论**：

**封装库选型确定**：`bilibili-api-python` 因归档 + 法律状态不明而排除，**维持自研薄适配层**（阶段 0 已用约 250 行打通全部 16 个接口）。

---

## 7. 交付与操作

### 7.1 复现方式

```powershell
# 全量（授权 + 匿名矩阵）
python tools/stage0/probe.py

# 只做匿名/公开范围
python tools/stage0/probe.py --mode public

# 只跑某一组（前缀匹配）
python tools/stage0/probe.py --only video_list_public,playurl

# 换目标 UID
python tools/stage0/probe.py --uid 123456

# 核对本文档中的数字断言是否与证据一致（28 项，全通过才算一致）
python tools/stage0/check_evidence.py
```

产物：

| 路径 | 内容 |
| --- | --- |
| `stage0/evidence/RESULTS.md` | 自动生成的验证结果表（身份矩阵 + 逐探针观察） |
| `stage0/evidence/summary.json` | 结论化记录（含对照数据与身份矩阵） |
| `stage0/evidence/raw/*.json` | 逐探针**脱敏 + 裁剪**的原始响应（57 个） |
| `stage0/evidence/component-verification-raw.md` | 组件许可证/维护状态核查的完整原始报告 |

> `stage0/evidence/` 已在 `.gitignore` 中排除（可由 `probe.py` 重新生成）。

### 7.2 凭据处理

- 凭据只从环境变量 `SUBVIDEO_COOKIE` / `SUBVIDEO_UID`、`config.local.toml`、`require.txt` 三者之一读取，优先级同上。
- 所有落盘与打印路径强制经过 `Redactor`；探针结束前会扫描全部产物，**若发现任何 Cookie 字段值即报错退出（退出码 3）**。
- 产物中只保留 Cookie 的**字段名**（如 `SESSDATA`、`bili_jct`），不含值。

### 7.3 待办（凭据卫生）

`require.txt` 目前是明文 Cookie 且位于项目根目录。**建议尽快**：

1. 把内容迁到 `config.local.toml`（`[bilibili]` 段，`uid` + `cookie`）；
2. 在 `.gitignore` 中加入 `config.local.toml` 与 `require.txt`；
3. 删除 `require.txt`。

本次已添加 `.gitignore` 覆盖这两项。**该 Cookie 已在对话中出现过，建议验证结束后在 B 站重新登录以使 `SESSDATA` 失效。**

---

## 8. 与需求的偏差 / 待对齐项

| # | 偏差 | 需求依据 | 状态 / 需决定的选项 |
| --- | --- | --- | --- |
| 1 | ~~本项目样本的专栏列表不可得~~ | 需求 3.4、验收 6 | ✅ **已解决**：专栏走 `opus/feed/space?type=article`（新格式），44 篇全部可发现。适配层须同时查新/旧两个来源并去重（3.5.3） |
| 2 | 已删除与无权限同为 `62002`，无法三分 | 需求 3.1.5 | 状态里合并为"稿件不可见（62002）"，在交付说明中列明该平台限制 |
| 3 | 充电视频无权限不报错，只有试看片段 | 需求 3.1.5、§5 | 适配层按媒体流形态判定并在 `metadata.json` 记录 `permission: preview_only` |
| 4 | 未登录时字幕接口静默返回空数组 | 需求 3.3.2、3.3.6 | 适配层在未登录时不把空字幕判为"无字幕"，直接以登录态错误终止 |
| 5 | ~~本地 OCR 方案在 Python 3.14 上不可安装~~ | 需求 2、3.3.2、验收 3 | ✅ **已决策：取消本地 OCR**（2026-09-23）。`REQUIREMENTS.md` 与 `DEVELOPMENT_PLAN.md` 已同步更新：不产生 `ocr.txt`、不引入 OCR 依赖链、移除 `--ocr` 参数与 PaddleOCR 组件。**Python 3.14 阻塞随之消失**，无需换引擎或降版本 |
| 6 | `/x/space/wbi/article` 的 `count` 语义未确定 | 需求 3.1.1 | **无需决策**：适配层不以 `count` 判断终止，改用 `has_more`（3.5.6） |

**六项全部有结论，没有遗留待决策项。** 第 1、5 项已解决，第 2、3、4、6 项按上表直接落到实现里。

---

## 9. 阶段 0 完成门槛对照

计划第 5 节要求："明确每类可发现/可读取范围及无法覆盖的情况"。

| 要求 | 状态 |
| --- | --- |
| 登录态验证 | ✅ 第 3.1 节 |
| 三类列表 / 详情验证 | ✅ 视频（3.3）、动态（3.4）、专栏（3.5，含一次结论纠错） |
| 公开与授权充电样本验证 | ✅ 第 3.3.4 节（充电权限已用媒体流形态证实） |
| 动态原图 / 专栏图片 / 视频分 P / 播放地址 / 字幕 可读性 | ✅ 第 3.3.2、3.3.3、3.3.4、3.7 节 |
| 候选封装库维护状态与许可证 | ✅ [组件验证记录](stage0-component-verification.md) |
| 明确可发现范围 | ✅ 第 4.1 节 |
| 明确无法覆盖的情况 | ✅ 第 4.2 节 |
| 确认适配策略 | ✅ 第 5 节（接口白名单 + 11 条契约） |
| 不靠模拟成功掩盖充电限制 | ✅ 充电限制以原始响应记录在 `stage0/evidence/raw/`，第 3.3.4、3.4.3、3.5.4 节如实标注"错误码层面不可区分" |

**结论：阶段 0 的验证工作已完成，三类内容全部通过，且无遗留待决策项。** 阶段 1 的全部工作（项目结构、CLI/TOML、采集适配层、时间筛选、跨类最新 N、索引/状态、动态与专栏归档）**均无阻塞项**。

原第 5 项待决策（本地 OCR 方案）已于 2026-09-23 决定为**取消本地 OCR**，`REQUIREMENTS.md` 与 `DEVELOPMENT_PLAN.md` 已同步。视频文字来源只有两种：平台现成字幕、本地 ASR。**Python 3.14 上不再有已知的依赖阻塞。**

> 记录一次纠错：首轮验证把专栏判为"本样本不可得"，并据此提出"阶段 1 暂不做专栏"的选项。用户提供 `/opus/` 样例后复核发现 `type=article` 参数生效，44 篇专栏全部可发现。**该错误选项已撤回**，专栏与其他两类同等可做。

### 9.1 阶段 1 可直接落地的依据

| 阶段 1 工作 | 阶段 0 已提供的依据 |
| --- | --- |
| 项目结构 / CLI / TOML | 三个子命令的参数面已在计划第 4 节确定；凭据优先级已由 `tools/stage0/credstore.py` 实现并验证 |
| 采集适配层 | 第 5.1 节接口白名单（16 个接口，含 WBI 与否、最低身份） |
| 时间筛选 | `created` / `pub_ts` 为 Unix 秒；`arc/search` 与动态/专栏详情均可拿到发布时间；两个 opus 列表都不提供时间，需走详情（3.4.1、3.5） |
| 跨类最新 N | 三类列表的排序语义已确认：视频 `order=pubdate` 严格降序（3.3.1）；动态 `offset` 游标倒序（3.4.1）；专栏 `type=article` 按时间倒序且首条最新（3.5.2） |
| 索引 / 状态 | 错误码语义表（3.6）与"三分类要如实"的契约（5.2）；`-352` 必须可重跑补做（3.5.5） |
| 动态归档 | 逐类型字段路径（3.4.2）、`modules` 双形状、富文本解析、原图路径（3.7） |
| 专栏归档 | 两个来源与判别位（3.5.3）、`paragraphs[]` 正文与 `para_type=2` 图片段落（3.5.4）、旧格式 HTML 解析（3.5.3） |
