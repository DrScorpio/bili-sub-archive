# 阶段 1 基础归档：实施与验证记录

状态：已完成（2026-09-23）
依据：[DEVELOPMENT_PLAN.md](../DEVELOPMENT_PLAN.md) 第 3、4、5 节 · [REQUIREMENTS.md](../REQUIREMENTS.md) 第 3.1/3.2/3.4 节 · [阶段 0 平台验证](stage0-platform-verification.md) 第 5 节（接口白名单 + 11 条契约）
复现：`python -m unittest discover -s tests -t .`（离线 155 用例，约 15s）· `python -m subvideo sync --uid <UID> --latest 6`
产物：`subvideo/`（实现）· `tests/`（测试）· `config.example.toml`（配置模板）· `.smoke_sync/`（联网冒烟产物，`.gitignore` 已排除）

---

## 1. 交付内容

```text
subvideo/
  __main__.py  cli.py             CLI：check / sync / retry、参数面与运行摘要打印
  config.py    credentials.py     四层配置（默认<config.toml<config.local.toml<环境/CLI）+ 凭据装配
  redact.py                       脱敏：日志、异常、产物统一出口 + 产物回扫自检
  errors.py                       错误分类、退出码、平台错误码语义
  timeutil.py  models.py          北京时间工具；条目/步骤/统计模型
  paths.py                        目录命名清理、作者目录定位、原子写入、单写者锁
  store.py                        index.json + metadata.json、步骤状态机
  filters.py                      日期范围 + 跨类最新 N + 稳定排序
  discover.py                     三类列表分页发现、置顶识别、翻页边界、扫描统计
  runner.py                       sync / retry 编排、风控停止、运行摘要落盘
  bili/  wbi.py client.py parse.py api.py
                                  限速/退避/WBI/错误分类；双形状解析；接口白名单
  archive/ dynamic.py article.py video.py images.py htmlmd.py
                                  动态/专栏/视频归档；配图下载；旧格式 HTML→Markdown
```

命令面（计划第 4 节）：

```powershell
python -m subvideo check  [--scan-output]                 # 依赖/配置/登录态/产物凭据自检
python -m subvideo sync   --uid 123456 --from 2026-01-01 --to 2026-09-23
python -m subvideo sync   --uid 123456 --latest 20 --types dynamic,article --no-images
python -m subvideo retry  --uid 123456 --steps content,images
```

`config.example.toml` 只含占位符；实际使用复制为 `config.toml`（可入库）与 `config.local.toml`（不入库，放 Cookie/密钥）。

---

## 2. 阶段 1 完成门槛对照（计划第 5 节）

| 计划门槛 | 结果 | 证据 |
| --- | --- | --- |
| 三类公开样本可归档 | ✅ 动态 6 篇、专栏 2 篇、视频 2 条（阶段 1 视频到元数据/分 P）、共 10 个条目 / 6.27 MiB | 第 5.1 节运行 A、C、G |
| 重跑不重复 | ✅ 第二次同参数运行：`跳过 6`、下载 `0 KiB`、目录集合与条目数不变 | 第 5.2 节运行 B |
| 日期和合计 N 筛选正确 | ✅ `--latest 6` 跨三类取全局最新 6 条（全为动态，符合真实时间序）；`--from/--to` 命中 4 / 候选 50 | 运行 A、D |
| 索引/状态可恢复 | ✅ `index.json` + 逐条 `metadata.json`；索引损坏/缺失时按 metadata 重建；单写者锁 | 第 4.3、6 节 |
| 动态归档（正文/原图/顺序） | ✅ `content.md` + `images/`，图片不变量 11 张下载 / 11 处引用 / 0 违例 | 运行 A、第 5.1 节 |
| 专栏归档（两种格式） | ✅ 新格式 opus 联网验证（120 段落块 / 17 图 / 6,239 字）；旧格式 read/cv 由离线合成样本覆盖（见第 8 节限制 2） | 运行 C、`tests/test_offline_flow.py` |

---

## 3. 阶段 0 的 11 条契约落地位置

| # | 契约 | 实现 | 验证 |
| --- | --- | --- | --- |
| 1 | 统一输出 `kind/id/published_at/url/title/author/raw_ref` | `bili/api.py` 的 `video_item` / `dynamic_item` / `opus_item` / `legacy_article_item` | `tests/test_parse.py::ItemAssemblyTest` |
| 2 | 所有列表与详情默认带登录 Cookie | `HttpClient` 统一注入；`sync` 无 Cookie 直接 `CredentialError` | `test_offline_flow.py::GuardTest` |
| 3 | 不把 `code=0` 当"内容可读" | 视频取 `playurl` 做访问探针，按 `dash.video` / `durl` 形态判定 `permission=full/preview_only/unknown` | `test_parse.py`、冒烟运行 G（真实充电视频 `dash 12 路 → full`） |
| 4 | 状态三分类如实 | `未发现`（列表里没有）/ `invisible`（62002 原样记录）/ `failed`（网络/风控/HTTP） | `errors.classify` + `store` 错误记录；`test_client.py` |
| 5 | 风控退避、不绕过 | `-352`/HTTP 412 清空 wbi 密钥 + 指数退避；连续 3 次即停止本次运行并提示人工处理 | `test_client.py::RetryTest`、`runner.RISK_STOP_THRESHOLD` |
| 6 | 限速（间隔 + 抖动） | `HttpClient._throttle`，默认 1.2s；`opus/detail` 串行 | `test_client.py::ThrottleTest`；冒烟 40 次请求 0 次风控 |
| 7 | 两种 `modules` 形状都要解析 | `parse_dynamic_item` 同时处理 dict（`/detail`）与 list（`opus/detail`） | `test_parse.py::DynamicParseTest` |
| 8 | 发布时间来源优先级 | 详情 `module_author.pub_ts` > 列表；`opus/feed` 无时间时逐条取详情 | `Discoverer._articles`、`test_offline_flow.py::FilterTest` |
| 9 | 专栏两个来源、去重 | `opus/feed?type=article`（新）+ `/x/space/wbi/article`（旧）；`(kind, platform_id)` 去重；旧格式详情里回填 `opus_id`/`dynamic_id` 便于人工核对 | `Discoverer._articles`、`article.py` |
| 10 | `-352` 不得写成终态失败 | 失败步骤留 `failed` + 错误码，条目保持 `pending`，`retry` 可补做 | `test_offline_flow.py::RetryTest` |
| 11 | 不以 `count` 做分页终止 | 一律以 `has_more` / 实际条目数为准，`count` 只作交叉校验并写入扫描统计 | `models.ScanStats.count_reported`、运行 A 摘要 |

补充实现（计划第 3.2 节）：目录命名与 Windows 保留字清理、作者目录以 UID 定位（改名不新建目录）、临时文件 + `os.replace` 原子写入（可选 `SUBVIDEO_FSYNC=0` 关闭刷盘）、`.lock` 单写者锁（陈旧锁按 6 小时判据抢占并告警）。

---

## 4. 文件与状态契约

### 4.1 真实产物结构（冒烟运行）

```text
.smoke_sync/1039025435_战国时代_姜汁汽水/
  index.json                       条目索引（唯一写者：本次运行）
  _runs/20260923T180708_0800.json  每次运行的完整摘要
  2026-09-22_动态_最近这个评论区给我整不会了。…_1250848386750349380/
    metadata.json
    content.md                     正文（含内联图片、转发原文、卡片信息、未获取内容）
    images/01.jpg 02.jpg 03.jpg avatar.jpg
  2026-06-17_专栏_地缘 + 金银 + 石油 + A股_12147419925782/
    metadata.json
    article.md
    images/01.png … 17.png
  2026-09-16_视频_预防式加息 vs 新加息周期；…_BV1zveP6eEZ1/
    metadata.json                  标题/简介/发布时间/BV/分 P/权限探针（阶段 1 到此）
```

### 4.2 步骤状态

| 类型 | 步骤 | 阶段 1 状态 |
| --- | --- | --- |
| 动态 | `fetch` → `content` → `images` → `render` | 前三步实现；`render` 记 `skipped: stage2_not_implemented` |
| 专栏 | `fetch` → `content` → `images` | 全部实现 |
| 视频 | `fetch` → `media` → `transcript` → `summary` → `mindmap` | 仅 `fetch`（元数据 + 分 P + 权限探针）；其余 `skipped` 并注明阶段 |

每条状态取 `pending/running/done/skipped/failed`；`skipped` 必带 `reason`（如 `no_images`、`permission_preview_only`、`content_blocked`、`stage2_not_implemented`）。门控类跳过会同时写入 `errors[]`，因此"权限不足"在索引里可见、但不会被自动 `retry` 反复打接口。

`metadata.json` 不含 Cookie / API 密钥 / 临时下载 URL（图片按 `images/NN.ext` 相对路径记录，失败时保留原始 CDN 链接）。写出路径全部经过 `Redactor`，`check --scan-output` 可回扫验证。

---

## 5. 联网冒烟验证（2026-09-23，目标 UID 1039025435）

环境：Python 3.14.7 / Windows；凭据来自本地 `require.txt`（登录 mid 914754）；请求间隔 1.2s。全部 7 次运行 **0 次风控、0 次重试**。

| 运行 | 命令要点 | 结果 |
| --- | --- | --- |
| A 首次采集 | `sync --latest 6` | 选中 6 条动态；40 次请求；下载 3,046 KiB；6/6 `done` |
| B 幂等重跑 | 同 A | 6/6 `skipped`；17 次请求（仅发现阶段）；下载 0 KiB；目录与条目数不变 |
| C 专栏 | `sync --types article --latest 2` | 2/2 `done`；32 次请求；3,245 KiB；正文 120 块/17 图/6,239 字 与 69 块/7 图 |
| D 日期筛选 | `sync --from 2026-09-18 --to 2026-09-22` | 候选 50 → 范围外 46 → 选中 4（9/18、9/21、9/22×2），全部命中既有条目 |
| E 补做 | `retry` | `没有需要补做的条目`；2 次请求 |
| F 凭据自检 | `check --scan-output` | 扫描 `.smoke_sync` 全部文本产物：`未发现凭据值 ✔` |
| G 视频 | `sync --types video --latest 2` | 2/2 `done`（`fetch` only）；11 次请求；两条均为充电专属且 `permission=full`（`dash` 12 路 / `durl` 0 段） |

### 5.1 图片不变量（运行 A 后逐条目校验）

| 指标 | 值 |
| --- | --- |
| 已下载配图（`status=done`） | 11 |
| `content.md` 中的图片引用 | 11 |
| 已下载但未被引用 / 引用了不存在的文件 | 0 / 0 |

同一条不变量已固化为测试 `test_offline_flow.py::assert_image_invariants`。

### 5.2 多 P 结构（只读校验）

用阶段 0 的 4 P 样本 `BV1BqhB6nEdN` 直接校验解析层：

| P | cid | part | duration | 尺寸 |
| --- | --- | --- | --- | --- |
| 1 | 42082042159 | 《原神》角色预告-「沃雅妮莎：此夜共沦」 | 175s | 2560×1440 |
| 2 | 42082043958 | 日-… | 179s | 2560×1440 |
| 3 | 42081978254 | 英-… | 179s | 2560×1440 |
| 4 | 42082110816 | 韩-… | 179s | 2560×1440 |

与阶段 0 第 3.3.2 节记录的 `cid`/`part`/`duration` **逐字段一致**；`view.pages` 与 `pagelist` 同时返回 4 P。

---

## 6. 测试

`python -m unittest discover -s tests -t .` → **155 用例，全部通过**（约 15s；`tests/` 会自动设置 `SUBVIDEO_FSYNC=0` 以避免 Windows 上每次 fsync ≈ 50ms 的开销）。

| 文件 | 覆盖点 |
| --- | --- |
| `test_filters.py` | 北京时间闭区间边界、UTC 归一时间、跨类最新 N、先日期后 N、同秒平局、置顶按真实时间、未知时间如实回报 |
| `test_paths.py` | Windows 非法字符/保留名/截断、作者目录 UID 定位复用、同名不覆盖、原子写入无残留、锁互斥/陈旧锁抢占/异常释放 |
| `test_store.py` | 条目分配与幂等、步骤状态转移与汇总、索引往返、索引缺失/损坏时按 metadata 重建、运行摘要落盘、`published_at` ISO 往返 |
| `test_parse.py` | 富文本对象、表情替代文本、段落类型 1/2/9、未知段落保留说明、`modules` 双形状、卡片/转发解析、`playurl` 形态判定、统一 Item 字段 |
| `test_client.py` | WBI 签名与密钥缓存、限速、`-352`/412 退避重试、`-101`/62002/非 JSON/网络错误分类、图片下载与 `http→https` |
| `test_config.py` | TOML 分层与 local 覆盖、环境变量、CLI 覆盖、各类非法配置报错、`require.txt` 回退 |
| `test_redact.py` | Cookie 整串/分字段/LLM 密钥脱敏、字段名保留、递归脱敏、产物回扫 |
| `test_offline_flow.py` | **离线集成**：合成响应驱动完整 sync（三类归档、门控 denied、部分图片失败 partial）、幂等重跑、dry-run、日期/N/单类筛选、`-352` 后 retry 补做、图片不变量 |

离线集成使用 `tests/fixtures.py` 的 `FakeApi`，字段路径照抄阶段 0 证据（`stage0/evidence/raw/*.json`），不联网。

---

## 7. 实现中新增的平台认知

1. **`opus/feed/space` 无发布时间**（阶段 0 已知），落地后量化出成本：每选 1 条专栏候选 ≈ 1 次 `opus/detail`，`--latest N` 实际约 N+1 次详情请求；该成本写入 `ScanStats.notes` 与运行摘要。
2. **充电专属子集列表会给出公开列表当页未出现的条目**：`--latest 6` 时子集额外贡献 5 条。原因是两个列表是不同的翻页窗口，而非公开列表漏抓；这些条目都早于翻页边界，不会进入"最新 N"。运行摘要如实标注。
3. **图文动态的配图可能只在 `major.opus.pics` 里**（`opus/detail` 的 `paragraphs` 只有文字）。首轮冒烟因此出现"图下载了但正文没引用"——已修复为：图片序号按【主动态段落 → 主动态卡片 → 转发原文段落 → 转发原文卡片】布局，未被正文消费的图片单独在 `## 配图` 节引用，并加测试锁死该不变量。
4. **门控内容的正确表达**是 `content=skipped(content_blocked)` + `errors[]` 记录，而不是把 `fetch` 记为失败：详情接口本身是成功的，失败的是"正文不可读"；这样 `retry` 不会对权限不足的条目反复打接口。
5. **风控计数不能与内部 `nav` 请求耦合**：清空 wbi 密钥后重取 `nav` 必定成功，若把它的成功当作"风控解除"，连续风控计数会被错误清零。已加 `track_risk=False` 区分内部辅助请求。
6. 本机沙箱环境对形如 `tmp<8位随机>` 的目录拒绝访问，测试工作目录改用 `.test_tmp/case_*`（`tests/__init__.py` 有说明）。

---

## 8. 已知限制与偏差

| # | 限制 | 影响 | 处理 |
| --- | --- | --- | --- |
| 1 | 阶段 1 不实现视频下载、字幕/ASR、总结、导图、动态长图 | 视频目录当前只有 `metadata.json`；动态无 `render.png` | 步骤显式记 `skipped` + 原因，阶段 2/3 直接接管（第 9 节） |
| 2 | 本样本 UP 的 44 篇专栏**全部是新格式 opus**，联网跑不到旧格式 read/cv 分支 | 旧格式 `article.md` 只有离线合成样本 + 阶段 0 对照作者证据 | 上线前用任一有旧格式专栏的作者做一次授权在线验收 |
| 3 | `62002` 仍不可区分"已删除 / 无权限"（阶段 0 第 3.6 节） | 状态里原样记录错误码，不写成"权限不足" | 交付说明列明；沿用阶段 0 结论 |
| 4 | 充电视频无权限时接口不报错 | 只有试看片段 | `permission=preview_only`，`media` 记 `skipped: permission_preview_only`，绝不下载试看当正片 |
| 5 | 单条失败不中断整批；部分配图失败 | 条目记 `partial`，`content.md` 保留原链接 | `retry` 只补失败项（已测） |
| 6 | 目录名由标题摘要（60 字符）+ 平台 ID 组成 | 长标题看不清 | 完整标题在 `metadata.json` 与正文里；`index.json` 按 `(kind, platform_id)` 定位 |
| 7 | `max_pages` 默认 40；达到上限时结果可能不完整 | 极端账号可能漏条目 | 扫描统计记 `stopped_by=max_pages` 并在摘要告警 |
| 8 | 发现阶段取不到发布时间的条目不计入 N 候选 | 可能少选 | 运行摘要给出条数与"重跑可补做"提示（不猜时间） |

---

## 9. 阶段 2 衔接点

阶段 1 已把阶段 2 需要的输入全部留在 `metadata.json` 里，阶段 2 只需实现被 `skipped` 的步骤：

| 阶段 2 工作 | 阶段 1 已备好的数据 |
| --- | --- |
| 多 P 视频下载/续传 | `extra.pages[]`（cid/page/part/duration/尺寸）、`extra.access`（`dash` 路数、`accept_quality/description`）、`permission` |
| 字幕提取（主路径） | `bili/api.py::subtitle_list`（白名单已登记、默认登录态）、`extra.pages[].cid` |
| 无字幕时本地 ASR | `config.asr_*` 开关与模型名；`skipped: stage2_not_implemented` 的 `transcript` 步骤位 |
| 动态单张长图 | `extra.images`（本地相对路径 + 原始尺寸 `width/height`）、`extra.author.face`（头像）、`content.md` 结构 |
| 失败可补做 | 步骤状态机 + `retry`（含 `--steps` 过滤）+ 单写者锁 |

需要新增的运行时依赖（阶段 1 为纯标准库）：`yt-dlp`、`ffmpeg`、`faster-whisper`（可选）、`Pillow`。`check` 已能报告这些依赖的可用性。

---

## 10. 运行与退出码

```powershell
# 1) 准备配置：把 Cookie 放到环境变量或 config.local.toml（不入库）
$env:SUBVIDEO_COOKIE = "SESSDATA=...; bili_jct=..."
Copy-Item config.example.toml config.toml

# 2) 先体检，再采集
python -m subvideo check  --uid 1039025435
python -m subvideo sync   --uid 1039025435 --latest 20
python -m subvideo sync   --uid 1039025435 --from 2026-01-01 --to 2026-09-23
python -m subvideo sync   --uid 1039025435 --types dynamic,article --no-images
python -m subvideo retry  --uid 1039025435 --steps content,images
```

| 退出码 | 含义 |
| --- | --- |
| 0 | 全部成功（含幂等跳过） |
| 1 | 未预期错误（带 `-v` 打印堆栈） |
| 2 | 配置/参数/输出目录错误 |
| 3 | 缺少凭据或登录态失效 |
| 4 | 运行完成但有条目失败/权限不足（摘要给出定位信息） |
| 5 | 遇到风控且退避无效，已停止（不绕过，提示人工处理） |
| 130 | 用户中断（已完成产物保留，可 `retry` 补做） |
