# 阶段 3 总结与思维导图：实施与验证记录

状态：已完成（2026-09-23）
依据：[DEVELOPMENT_PLAN.md](../DEVELOPMENT_PLAN.md) 第 2.2、3.3、4、5、6 节 · [REQUIREMENTS.md](../REQUIREMENTS.md) 第 2（总结配置）、3.3.3、3.3.4、3.3.6、5、6 节 · [阶段 2 衔接点](stage2-media-and-transcript.md#10-阶段-3-衔接点)
复现：`python -m unittest discover -s tests -t .`（离线 **393** 用例，约 44s）· `python -m subvideo check --uid <UID>` · `python -m tools.stage3.local_llm_stub --check-entry <条目>` · `python -m tools.stage3.mindmap_check`
产物：`subvideo/summarize/`（实现）· `tests/test_summarize.py` + `tests/test_offline_flow.py::Stage3FlowTest`（测试）· `tools/stage3/`（验证工具）· `.test_tmp/stage3_selfcheck/`（替身模型端到端产物，`.gitignore` 已排除）

---

## 1. 交付内容

```text
subvideo/
  summarize/
    __init__.py        编排：transcript.txt → 分块 → 要点 → 合并总结 → summary.md → 大纲 → .mmd → PNG
    llm.py             OpenAI 兼容 Chat Completions 客户端（openai SDK 或标准库直连，二选一）
    prompt.py          内置 prompt（system/map/reduce/full）+ 自定义 prompt 文件 + 指纹
    chunk.py           长稿分块（不丢尾段；块数超上限就放大每块）
    outline.py         受控层级大纲（深度/节点数/标签长度上限 + 派生兜底）
    mermaid.py         大纲 → mindmap.mmd（确定性序列化 + Mermaid 特殊字符全角转义）
    mermaid_cli.py     mmdc 渲染 PNG（原子替换 + PNG 头校验，失败保留 .mmd）
tools/stage3/
  local_llm_stub.py    本地 OpenAI 兼容替身服务（验证真实 HTTP 链路 + 尾段不丢 + 不泄凭据）
  mindmap_check.py     真实归档文字稿 → .mmd（+ 有 mmdc 时渲染 PNG）离线检查
```

命令面（计划第 4 节，新增项加粗）：

```powershell
python -m subvideo check  --uid 1039025435                       # 依赖/配置/登录态（新增总结与导图两行）
python -m subvideo sync   --uid 123456 --latest 20
python -m subvideo sync   --uid 123456 --summary-model gpt-4o-mini `
                          --summary-base-url https://api.example.com/v1 --summary-prompt prompts/me.txt
python -m subvideo sync   --uid 123456 --no-summary --no-mindmap # 只要归档与文字稿
python -m subvideo retry  --uid 123456 --steps summary,mindmap   # 只补做总结/导图
python -m subvideo retry  --uid 123456 --mmdc "C:\...\mmdc.cmd"  # 装好 mermaid-cli 后只补渲染
```

新增配置段（`config.example.toml` 已同步，只含占位符）：`[summary] enabled/base_url/model/api_key/client/prompt_file/chunk_chars/max_chunks/overlap_chars/temperature/max_tokens/timeout_seconds/retries/min_chars`、`[mindmap] enabled/mmdc_path/puppeteer_config/max_nodes/max_depth/label_chars/width/background/timeout_seconds`。
环境变量新增 `SUBVIDEO_MMDC`；密钥沿用 `SUBVIDEO_LLM_API_KEY`（或 `[summary] api_key`，两个配置文件都有该配置项，建议只填在 `config.local.toml`）。

版本：`subvideo 0.2.0 → 0.3.0`，`FORMAT_VERSION 2 → 3`（`extra.summary` 是**新增**字段，旧产物无需迁移）。

---

## 2. 阶段 3 完成门槛对照（计划第 5 节）

| 计划门槛 | 结果 | 证据 |
| --- | --- | --- |
| 每个有文字稿的视频一份总结和导图 | ✅ 离线：有文字稿 → `summary.md` + `mindmap.mmd` + `mindmap.png` 三步齐备；无文字 → 显式 `skipped(no_transcript)` 且**一次模型都不调用** | 第 5、7 节；`Stage3FlowTest::test_summary_and_mindmap_end_to_end`、`test_no_transcript_never_calls_model` |
| OpenAI 兼容接口 | ✅ 真实 HTTP 往返（本机替身服务）：4 块 → 5 次调用 → 解析 token 用量 → 落盘 | 第 5.1 节 |
| 自定义 prompt | ✅ 真 TOML（`system/map/reduce/full` 四个顶层键）或 INI 分节 `[system]/[map]/[reduce]/[full]`；占位符校验；prompt 指纹写进产物 | `test_summarize.py::PromptTest` |
| 长稿分块、不丢尾段 | ✅ 单元：分块完整覆盖正文（`uncovered_ranges == []`）；端到端：真实 10013 字文字稿的最后 60 字出现在最终总结里 | 第 5.2 节；`ChunkTest`、`Stage3FlowTest::test_long_transcript_tail_reaches_final_reduce` |
| `.mmd` 与 PNG | ✅ `.mmd` 确定性序列化 + 全角转义（真实中文产物通过结构检查）；PNG 走 mmdc，失败保留 `.mmd` | 第 5.2、6 节 |
| 模型失败可独立重试 | ✅ `summary` 失败 → `mindmap` 记 `failed(summary_failed)`；`retry` 只补这两步；渲染失败不影响 `summary` | `Stage3FlowTest::test_llm_failure_is_retryable`、`test_missing_mmdc_keeps_mmd_and_can_be_retried` |
| 总结失败不影响原视频和文字稿保存 | ✅ 总结失败只把条目记 `partial`，`transcript.txt`/`videos/` 原样保留 | 同上 |
| 阶段 2 遗留步骤被接管 | ✅ 历史 `skipped: stage3_not_implemented` 被自动识别为可重做 | 第 3.3 节；真实 retry 见第 5.3 节 |

---

## 3. 关键设计

### 3.1 分块：宁可每块更长，也不丢尾段

`[summary] max_chunks` 是"最多调用多少次模型"的**成本上限**，不是内容上限。正文超过
`chunk_chars × max_chunks` 时，实现把每块**放大**到 `ceil(正文长度 / max_chunks)`（`ChunkPlan.scaled`
+ 运行说明），而不是截断后面的分 P。三条保障：

1. 分块按空行/行边界切，`## Pxx` 标题决定每个块覆盖哪些分 P（prompt 里会写明"涉及分 P"）；
2. 块之间保留 `overlap_chars` 尾部重叠，避免恰好切在结论中间；
3. `chunk.uncovered_ranges()` 给出"没有被任何块覆盖的区间"，测试断言它恒为空 —— 这是
   "不丢尾段"的机器可验证形式（真实 10013 字文字稿切成 4 块，尾段在最终总结里可查，见 5.2）。

合并阶段若"分段要点"本身超预算，会先做一轮**要点再压缩**（最多 3 轮，`_group_points` 按
`### ` 小节保序分组），仍然超预算就按当前长度直接合并并在说明里写明"可能触及模型上下文上限"。

### 3.2 受控大纲 + 确定性 Mermaid

计划 3.3 与风险表都点名"LLM 直接写 Mermaid 容易语法出错"，因此**模型只产出缩进大纲**：

- `outline.extract_outline()` 解析 `## 大纲` 小节，按缩进栈建树；`max_depth` 把过深的层**压平**
  （不丢节点）、`max_nodes` 截断、`label_chars` 截断、同层重复标签去重；
- 模型没给可用大纲时，`outline.derive_outline()` 用**确定性规则**从摘要段落/列表派生，
  并在产物里标 `outline_source = derived`（不假装是模型给的）；
- `mermaid.to_mermaid()` 生成 `mindmap` 语法：根 `root((标题))`、每层 2 空格缩进，标签里的
  `( ) [ ] { } " ' : ; # % | < > \` *` 等 Mermaid 语法字符统一转成**全角**（中文标签里这些
  本来就是半角混写，转全角既保可读性又彻底避开歧义），换行压成空格；
- 同一棵树永远得到同一个 `.mmd`（`test_to_mermaid_shape_and_determinism`）。

### 3.3 输入指纹：不重复花钱，也能只重做该重做的

步骤状态看不出"换了模型或 prompt"，因此 `summary`/`mindmap` 各自记录一个**输入指纹**：

| 指纹 | 组成 |
| --- | --- |
| `summary.signature` | prompt 指纹 + 模型 + 端点 + 客户端 + 分块参数（块字数/块数上限/重叠/温度/min_chars）+ 文字稿（字数 + sha1 前 12 位） |
| `mindmap.signature` | 总结指纹 + 节点上限/深度/标签长度/PNG 宽度/背景色 |

两条路径都会用到它：

1. **归档流程内**：`_archive_stage3` 发现指纹一致且产物齐全 → 直接沿用（`reused=True`），
   不调用模型、不重复渲染；`--force` 强制重做；
2. **选中阶段**：`Runner._signature_steps()` 在 `sync`/`retry` 选条目时比对指纹，把"已完成
   但已过时"的步骤重新纳入（`Store.pending_steps(..., extra=...)`）—— 否则换了 prompt 之后
   所有步骤都是 `done`，条目根本不会被选中，计划 3.2 的"变更 prompt 或模型只重做总结/导图"
   就落不了地（`test_prompt_change_triggers_redo`）。

### 3.4 接管与"不反复打接口"的边界

`models.stale_skip_map` 在阶段 3 新增两条规则：`summary_disabled`/`mindmap_disabled`（开关重新打开）、
`llm_not_configured`（配好 base_url + model 之后）。同时**刻意不把 `no_transcript` 纳入**：

- `no_transcript` 对 `transcript` 是终态（ASR 跑过确实没文字）；
- 对 `summary`/`mindmap` 若也算"可重做"，那么每条无字幕视频每次 `retry` 都会被选中并**重打详情接口**，
  与"避免反复打接口"（阶段 2 第 3 节）冲突；
- 文字稿后来补上了（例如开启 `--asr` 后 `transcript` 被重做），同一次 `retry` 会重跑整条视频流程，
  总结与导图**自然跟着做**（`Stage3FlowTest::test_transcript_asr_disabled_then_enable_asr_retry` 仍为 2 条选中）。

### 3.5 只发送文字稿

`prompt.py` 的模板占位符只有 `{transcript}` `{points}` `{chars}` `{chunks}` `{pages}` `{title}` `{author}` `{index}`；
`metadata` 只记**端点主机名**（`endpoint_host`，不记完整地址 —— 有的代理把令牌放在 query 里）；
密钥只进请求头，且落盘/日志统一过 `Redactor`。本机替身服务把每次请求记进 JSONL，
自检脚本断言"请求里出现 `SESSDATA`/`bili_jct`/`Cookie` 的次数为 0"（第 5.1 节）。

---

## 4. 文件与状态契约

### 4.1 步骤状态

| 类型 | 步骤 | 阶段 3 状态 |
| --- | --- | --- |
| 视频 | `fetch` → `media` → `transcript` → `summary` → `mindmap` | **全部实现** |

`summary`/`mindmap` 的 `reason`（都写进 `metadata.json`，`retry` 可区分处理）：

| 步骤 | 原因 | 含义 | 可重做 |
| --- | --- | --- | --- |
| `summary`/`mindmap` | `no_transcript` | 没有文字来源（需求 3.3.6 要求的显式标记） | ❌ 终态（补上文字稿后会随流程一起做） |
| `summary`/`mindmap` | `insufficient_text` | 正文少于 `[summary] min_chars` | ❌ 终态（可 `--force --steps transcript,summary` 重做） |
| `summary`/`mindmap` | `llm_not_configured` | 没配 base_url/model | ✅ 配置好后 |
| `summary`/`mindmap` | `summary_disabled` / `mindmap_disabled` | 开关关闭 | ✅ 开关打开后 |
| `mindmap` | `summary_failed` / `llm_failed` | 上游总结失败 | ✅ 天然（`failed`） |
| `summary`/`mindmap` | `llm_failed` / `llm_http_error` / `llm_auth_error` / `llm_timeout` / `llm_response_invalid` | 模型调用失败（`failed`） | ✅ 天然 |
| `mindmap` | `dependency_missing`（`mmdc` 缺失）、`render_failed`、`render_invalid`、`timeout` | 渲染失败（`failed`），**`.mmd` 保留** | ✅ 装好 mmdc 后 |
| `summary`/`mindmap` | `transcript_missing` / `prompt_invalid` | 产物或 prompt 文件有问题（`failed`） | ✅ 修好后 |

### 4.2 产物结构（真实条目）

```text
<条目目录>/
  transcript.txt / transcript_timed.srt     阶段 2 产物（总结的输入）
  summary.md                                总结正文 + 元信息头（模型 / prompt 指纹 / 生成时间 / 分块与 token 统计）
  mindmap.mmd                               Mermaid mindmap 源文件（确定性序列化，可手工编辑）
  mindmap.png                               mmdc 渲染结果（缺 mmdc 时不存在，步骤记 failed 并保留 .mmd）
  metadata.json                             extra.summary = {status, model, client, endpoint_host, prompt{source,fingerprint},
                                            signature, transcript{chars,sha1}, chunk_plan, calls, tokens, outline{...},
                                            mindmap{status,mmd,png,nodes,width,height,bytes,signature}}
```

`summary.md` 的正文夹在 `<!-- subvideo:summary:begin -->` / `<!-- subvideo:summary:end -->` 之间：
元信息头可以随时补写，复用产物时按标记切正文（人工编辑过、没有标记的文件也能读，会退化为
"剥掉开头的标题与元信息列表"）。

`summary.md` 里的指纹字段（真实产物，替身模型）：

```text
- 模型：stub-1（客户端 urllib，端点 127.0.0.1:56011）
- prompt：builtin，指纹 e74ca5e23355
- 生成时间：2026-09-23T22:32:46+08:00
- 文字稿：10013 字（sha1 c62e42387eba），分 P：P01，切成 4 块
- 模型调用：5 次；tokens prompt 4040 / completion 155
- 输入指纹：c281a2c9fc3f（prompt/模型/分块参数变化时重做）
```

---

## 5. 本机验证（2026-09-23，Windows / Python 3.14.7）

### 5.1 真实 HTTP 链路 + 不丢尾段 + 不泄凭据（替身模型）

本机**没有**可用的云端模型，也不想为验证花钱，因此用 `tools/stage3/local_llm_stub.py` 起一个
本地 OpenAI 兼容服务（`127.0.0.1`，实现真实 HTTP + JSON，内容确定性），再用**真实归档产物**跑
完整链路：

```powershell
python -m tools.stage3.local_llm_stub --check-entry ".smoke_sync/1039025435_战国时代_姜汁汽水/2026-09-16_视频_…_BV1zveP6eEZ1"
```

| 指标 | 值 |
| --- | --- |
| 输入文字稿 | 真实平台字幕合并稿 **10013 字**（1 个分 P 小节，阶段 2 的 900 段产物） |
| 分块 | 4 块（3000 字/块 + 200 字重叠） |
| 模型调用 | **5 次**（4 次要点 + 1 次合并），tokens prompt 4040 / completion 155 |
| 尾段 | 文字稿最后 60 字出现在最终 `summary.md` 里 → **不丢尾段**（端到端） |
| `mindmap.mmd` | 结构检查通过（`mindmap` 首行、`root((...))`、缩进正确、标签无残留语法字符） |
| 凭据 | 5 次请求的 JSONL 记录里 `SESSDATA`/`bili_jct`/`Cookie` 出现 **0** 次；`Authorization` 为空（未配密钥） |
| 请求体 | 只有 system（87 字）+ user（文字稿与要点），无媒体地址、无 Cookie |

真实产物 `mindmap.mmd`（根节点标签带的是**真实文字稿内容**，因为替身把收到的尾段原样带回）：

```text
%% SubVideo 思维导图（确定性序列化，非模型直出 Mermaid）
%% 视频：2026-09-16视频预防式加息 vs 新加息周期；沃什与期限溢价的相关性；…
%% 大纲来源：llm（llm = 模型输出，derived = 由摘要段落派生）
mindmap
  root((替身主题：最近中东的事情尤其是胡塞武装的事))
    第一部分要点
      细节
    第二部分要点
```

### 5.2 真实中文文字稿 → `.mmd` 序列化检查

```powershell
python -m tools.stage3.mindmap_check
```

自动挑 `.smoke_sync` 里文字稿最长的一条（32107 B 的真实中文文字稿），派生大纲 → 生成
`.mmd`（9 个节点 / 2 层）→ 逐行检查结构与转义：**通过**；同时如实报告 `mmdc` 缺失、
PNG 未生成（退出码 1，含义是"渲染未完成"，不是"链路失败"）。

### 5.3 阶段 2 遗留步骤的真实接管

```powershell
python -m subvideo retry --uid 1039025435 --out .smoke_sync --steps summary,mindmap
```

```text
[1/2] 归档 video BV1zveP6eEZ1：预防式加息 vs 新加息周期；…
    → partial：… 文字 1/1 P 有文字（平台字幕），900 段 / 9815 字；总结未完成（llm_not_configured）；
               导图未完成（llm_not_configured）
[2/2] 归档 video BV11zY76LErQ：828 房地产新政；…
    → partial：… 文字 1/1 P 有文字（平台字幕），969 段 / 10771 字；总结未完成（llm_not_configured）；
               导图未完成（llm_not_configured）
说明：
  · 未配置 LLM（[summary] base_url 与 model…）：有文字稿的视频其 summary/mindmap 记
    skipped(llm_not_configured)，配置好后 retry 可补做
  · 未找到 mermaid-cli（mmdc）：mindmap.mmd 照常生成，mindmap.png 记 failed(dependency_missing)
```

阶段 2 写下的 `skipped: stage3_not_implemented` 被自动识别为可重做（`选中 2 条`），
13 次请求 / 0 风控；退出码 4（有条目未完成，原因在说明里逐条列出）。

`python -m subvideo check --uid 1039025435` 真实输出（节选）：登录态有效、作者可读，
依赖表新增 `mermaid-cli`（缺失，附 npm 安装命令）与 `openai SDK`（未安装，标"可选"），
配置区新增"总结（LLM）：未配置…"与"思维导图：开启（节点上限 60 / 深度 3 / 宽 1600）；未找到 mmdc…"。

> **诚实说明**：本机**未配置云端模型、也未安装 mermaid-cli**，因此
> ① 真实第三方模型（上下文长度、限流、鉴权、截断行为）与 ② `mindmap.png` 的真实渲染
> **未在本机跑通**。这两条路径的覆盖来自：离线替身测试（Chat 替身 + mmdc 替身，含 401/429/5xx/
> 超时/空响应/非法 JSON/截断、mmdc 退出码非零/无产物/体积异常/超时/可执行文件缺失）、
> 5.1 节的真实 HTTP 往返（协议层已实测）、以及 Mermaid mindmap 官方语法。**交付前需在一台
> 配好模型与 mermaid-cli 的机器上做一次授权在线验收**（命令见第 9 节）。

---

## 6. Mermaid 渲染的工程细节（`mermaid_cli.py`）

| 处理 | 原因 |
| --- | --- |
| 只调用外部 `mmdc`（`--mmdc` 或 PATH），不自己实现渲染器 | 需求指定 Mermaid CLI；重复造轮子没有收益 |
| 渲染到 `mindmap.tmp.png` 再 `os.replace` | 半张图/错误页不会被当成成功产物 |
| 校验 PNG 签名 + IHDR 宽高（纯标准库读头，不用 Pillow） | 空文件、HTML 错误页、截断文件都会被判 `render_invalid` |
| 失败保留 `mindmap.mmd`，`mindmap` 记 `failed` | 计划 3.3："PNG 失败保留 `.mmd` 供重试"；`retry` 只补渲染 |
| 超时/退出码/异常都转成结果对象，不抛异常 | 单条失败不中断整批（需求 5） |
| `-w` 宽度、`-b` 背景、`-p` puppeteer 配置可配 | 容器环境常需 `--no-sandbox` 之类的 puppeteer 参数 |

---

## 7. 测试

`python -m unittest discover -s tests -t .` → **393 用例，全部通过**（约 44s）。阶段 2 为 291 例，本阶段新增 **102** 例。

| 文件 | 用例 | 覆盖点 |
| --- | --- | --- |
| `test_summarize.py` | 87 | 分块（头部/正文切分、覆盖无缝隙、分 P 归属、重叠上限、超块数放大、巨块硬切、空正文）· 大纲（主题作根、深度压平、节点/标签上限、无小节回落、去重、派生兜底、缩进栈）· Mermaid（特殊字符全角化、换行/截断、确定性与缩进、上限与省略提示）· LLM 客户端（端点/主机名、请求载荷与请求头、无密钥、多段 content、截断、401 不重试、429 重试、5xx 耗尽、网络/超时分类、非法 JSON/空响应、缺配置、SDK 模式与替身模块、异常映射）· mmdc（缺可执行/缺源文件/成功与原子性/退出码/无产物/体积异常/超时/崩溃）· prompt（分节解析、未知分节、占位符校验、覆盖合并、渲染替换、花括号存活、指纹变化）· 编排（成功产物与元信息、JSON 往返、未配置/关闭/无文字/文字不足、模型失败、截断提示、单块 vs 多块调用、放大分块、指纹变化、派生大纲、上游跳过/失败传播、渲染失败保留 .mmd） |
| `test_offline_flow.py` | 48（阶段 2 为 33，本阶段 +15） | 新增 `Stage3FlowTest`（13 例）：端到端产物与 metadata、真实长稿尾段进合并、未配置→配置后 retry、无文字不调用模型、文字不足、模型失败可重试、某块失败不生成残缺总结、缺 mmdc 保留 .mmd 且只补渲染、指纹一致不重复调用模型、换 prompt 触发重做、开关关闭→开启、产物不含密钥、**不注入 chat_client 时走真实 HTTP 客户端（本地替身服务，仅 loopback）**；`StaleSkipMapTest` 新增 2 例（`llm_not_configured` 可重做、`no_transcript` 对总结是终态） |

离线测试全部使用替身（`FakeChatClient` / `fake_mmdc_runner` / 注入 `transport` / 假 `openai` 模块），
**不联网、不调用真实模型、不运行 mermaid-cli**。

---

## 8. 实现中新增的设计决策

1. **两种等价传输**：计划 2.2 拟用 `openai` SDK；本机没装，于是实现里同时保留
   `urllib` 标准库直连（`[summary] client = auto|sdk|http`）。SDK 是**可选**依赖
   （`deps.probe_openai` 标 `optional`），缺失时 `check` 只提示"未安装（可选）"，不进"缺失依赖"告警。
2. **密钥可选**：本地 OpenAI 兼容端点（Ollama / LM Studio 等）通常不需要鉴权，因此不强制
   密钥；没配密钥时按匿名请求发送并在运行说明里写明"若服务端要求鉴权会返回 401"。
3. **`min_chars` 只看正文**：`transcript.txt` 的头（标题、时间基准、分 P 统计）不是内容。
   早期实现按整文件字数判断，导致"只有一行字幕"的短稿靠头部凑够 200 字而被误判可总结，
   已修正为只算第一个 `## Pxx` 之后的正文。
4. **`endpoint_host` 而不是完整 `base_url`**：有的代理把访问令牌放在 URL query 里，
   metadata 只记主机名（计划 3.2：metadata 不含临时下载 URL/凭据）。
5. **`llm_*` 错误分类与平台错误分开**：`llm_auth_error` 不会被误当成"B 站 Cookie 失效"，
   退出码/摘要文案也因此能说清是哪一边的问题。
6. **指纹要包含文字稿摘要**：只比 prompt/模型是不够的 —— 开了 ASR 重新转写后文字稿变了，
   总结必须重做（`transcript{chars,sha1}` 进指纹）。
7. **"已完成但过时"必须能在选中阶段被发现**：只做流程内指纹复用不够，`retry` 需要
   `Store.pending_steps(extra=...)` 这个钩子（第 3.3 节）。
8. **`no_transcript` 刻意不进 stale 表**（第 3.4 节），否则每条无字幕视频每次 `retry`
   都会被选中重打详情接口。
9. **过深的大纲节点压平而不是丢弃**：模型多给一层比丢内容好；`max_nodes` 才是硬截断，
   且会在 `.mmd` 注释与运行说明里写明"省略 N 个节点"。
10. **`summary.md` 用标记包裹正文**：元信息头（模型/prompt 指纹/时间）与正文分离，
    复用产物时不必靠"猜哪几行是头"。
11. **`FORMAT_VERSION = 3`、`subvideo 0.3.0`**：`extra.summary` 是附加字段，
    阶段 1/2 的产物读取侧按缺省值兜底，`Store.rebuild` 不校验版本，无需迁移。
12. **`tools/stage3/local_llm_stub.py` 是验证工具而不是测试替身**：它存在的意义是
    在**没有云端模型**的机器上验证真实 HTTP 链路（传输、载荷、解析、尾段传播、不泄凭据），
    离线单元测试里的 `FakeChatClient` 则完全不碰 socket。

---

## 9. 已知限制与偏差

| # | 限制 | 影响 | 处理 |
| --- | --- | --- | --- |
| 1 | 本机未配置云端模型，真实第三方模型的兼容性/限流/长上下文**未实测** | 只有协议层（真实 HTTP 往返）与替身证据 | 第 5.1 节已明示；交付前需做一次授权在线验收：`subvideo sync --uid <UID> --latest 1 --summary-base-url <真实端点> --summary-model <模型>` |
| 2 | 本机未安装 `mermaid-cli`，`mindmap.png` **未真实渲染** | PNG 只有替身 + PNG 头校验证据 | 第 5.2、6 节已明示；`npm install -g @mermaid-js/mermaid-cli` 后 `subvideo retry --steps mindmap` 即可补渲染（`.mmd` 已在手） |
| 3 | `mmdc` 首次渲染会下载 Chromium | 首次运行慢、需要网络 | `check` 报告 mmdc 状态与安装命令；失败只影响 `mindmap` 步骤 |
| 4 | `openai` SDK 路径在本机只被"替身模块"覆盖 | SDK 的版本差异（如 `max_tokens` 更名）未实测 | `client = http` 可随时绕过 SDK；SDK 初始化失败会自动降级为标准库直连并写日志 |
| 5 | `max_tokens` 用旧字段名 `max_tokens` | 个别新模型只认 `max_completion_tokens` | 兼容实现通常两者都收；被拒时服务端会返回 400，`summary` 记 `failed(llm_http_error)` 并保留文字稿，可改 prompt/模型后重试 |
| 6 | 分块按字数而非 token | 与模型真实上下文有偏差 | 块字数可配（`chunk_chars`）；超出时先"要点再压缩"再合并，并如实写说明 |
| 7 | 大纲由模型给出，质量取决于模型 | 可能给出笼统节点 | 深度/节点/标签三重上限 + 派生兜底；`.mmd` 可手工编辑后再渲染 |
| 8 | `insufficient_text` 是终态 | 短稿不会自动重试 | 说明里给出"补齐文字后可重跑"；需要时用 `--force --steps transcript,summary` |
| 9 | 复用判定依赖 `metadata.extra.summary.signature` | 手工删掉 `summary.md` 后会重做（符合预期） | 产物缺失即视为需要重做，不会静默沿用 |
| 10 | 阶段 1/2 的历史产物没有 `extra.summary` | 首次 `retry` 会正常计算（指纹为空 → 视为需重做） | 这是期望行为（阶段接管） |
| 11 | 推理模型的思考 token 也计入 `max_tokens` | `max_tokens=2048` 时正文可能为空（`finish_reason=length`）或摘要写到一半断掉、大纲缺失（导图退化为派生大纲） | 实测 `deepseek-v4-flash` + 10k 字文字稿：提到 8192 即可稳定产出「摘要 + 大纲」；空正文的报错已带"提高 max_tokens / 换非推理模型"的指引（`llm.py::_empty_content_hint`） |

---

## 10. 阶段 4 衔接点

| 阶段 4 工作 | 阶段 3 已备好的数据 |
| --- | --- |
| 安装与使用说明（Windows：Python / FFmpeg / Node.js+mermaid-cli / 可选 ASR 模型） | `check` 的依赖表已把四个外部组件 + 两个可选库连安装命令一起列出 |
| 配置模板与凭据约定 | `config.example.toml` 已含 `[summary]`/`[mindmap]` 全部键（只含占位符）；密钥只走环境变量或 `config.local.toml` |
| 授权样本验收（需求第 6 节逐条） | 阶段 3 的门槛已用离线 + 真实产物覆盖；在线验收脚本可复用 `tools/stage3/local_llm_stub.py --check-entry`（换真实端点即可） |
| 已知平台限制清单 | 阶段 0/1/2 的限制 + 本文件第 9 节（模型与 mermaid-cli 未实测） |
| 交付说明中的"失败可重试"清单 | `metadata.json` 的步骤状态与 `reason` 已是可机读的重试依据（第 4.1 节） |

---

## 11. 运行与退出码

```powershell
# 1) 配置 LLM（密钥只放环境变量或 config.local.toml，不入库）
$env:SUBVIDEO_LLM_API_KEY = "sk-..."
python -m subvideo sync --uid 1039025435 --latest 20 `
  --summary-base-url https://api.example.com/v1 --summary-model gpt-4o-mini

# 2) 自定义 prompt（可选）：真 TOML（system/map/reduce/full 四个顶层键）或 INI 分节，
#    占位符 {transcript} {points} {chars} {chunks} {pages} {title} {author} {index}
python -m subvideo sync --uid 1039025435 --summary-prompt prompts/summary.toml

# 3) 只要归档与文字稿
python -m subvideo sync --uid 1039025435 --no-summary --no-mindmap

# 4) 装好 mermaid-cli 后只补渲染（总结不会重做：指纹未变）
python -m subvideo retry --uid 1039025435 --steps mindmap

# 5) 本机验证
python -m tools.stage3.local_llm_stub --check-entry "<条目目录>"   # 真实 HTTP 链路（替身模型）
python -m tools.stage3.mindmap_check                              # 真实文字稿 → .mmd（+PNG）
python -m tools.stage3.local_llm_stub --port 8799                 # 只起替身服务，自己指过来
```

退出码沿用阶段 1/2：0 全部成功；1 未预期错误；2 配置/参数/输出目录错误；3 缺少凭据或登录态失效；
4 运行完成但有条目失败/权限不足/部分完成（总结或导图失败即计入）；5 风控停止；130 用户中断。
