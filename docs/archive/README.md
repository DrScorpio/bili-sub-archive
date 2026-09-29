# 开发过程归档（v0.1.0）

本目录保存 **subvideo v0.1.0 的开发过程记录**：需求、开发方案、逐阶段实施与验证记录，
以及当时使用的验证工具源码。它们随 v0.1.0 发布从仓库根目录移到这里归档。

> **冻结声明**：本目录内容**不再维护**，只作为"当初为什么这么做"的追溯依据。
> 其中的结论、数字、版本号与文件路径都停留在写下时的状态，
> **不代表当前代码的行为**——当前行为以 [README.md](../../README.md)、
> `config.example.toml` 和 `subvideo/` 代码为准。

## 内容

| 文件 | 内容 |
| --- | --- |
| [REQUIREMENTS.md](REQUIREMENTS.md) | 需求条目与第 6 节验收标准（开发时的验收口径） |
| [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) | 开发方案、阶段划分与各阶段门槛 |
| [stage0-platform-verification.md](stage0-platform-verification.md) | 接口白名单、11 条契约、充电内容可见性实测 |
| [stage0-component-verification.md](stage0-component-verification.md) | 依赖许可证、维护状态、Python 3.14 兼容性；**不提供本地视频画面 OCR** 的决策依据（第 2.1 节） |
| [stage1-basic-archive.md](stage1-basic-archive.md) | 基础归档（发现、筛选、索引/状态、动态与专栏）实施与验证 |
| [stage2-media-and-transcript.md](stage2-media-and-transcript.md) | 媒体下载、字幕/本地 ASR、动态长图实施与验证 |
| [stage3-summary-and-mindmap.md](stage3-summary-and-mindmap.md) | 总结、自定义 prompt、Mermaid 导图实施与验证 |
| [stage4-stability-and-delivery.md](stage4-stability-and-delivery.md) | 测试矩阵、安装说明、验收项逐条对照、限制汇总 |
| `tools/` | 当时使用的验证工具源码（见下） |
| `stage0-evidence/` | 阶段 0 探针保存的脱敏响应样本（`raw/*.json`、`RESULTS.md`、`summary.json`） |

### `tools/` 里的工具

| 路径 | 当时的用途 |
| --- | --- |
| `tools/stage0/probe.py` | 接口探测探针，生成 `stage0-evidence/` 与平台验证记录 |
| `tools/stage0/check_evidence.py` | 校验平台验证记录里的数字与证据文件一致 |
| `tools/stage0/{client,wbi,credstore}.py` | 探针用的限速 HTTP 客户端、WBI 签名、凭据存储 |
| `tools/stage2/render_check.py` | 真实归档产物 → 动态长 PNG |
| `tools/stage3/mindmap_check.py` | 真实文字稿 → `.mmd`（有 `mmdc` 时渲染 PNG） |
| `tools/stage3/local_llm_stub.py` | 本机替身模型服务，验证总结/导图的真实 HTTP 链路 |
| `tools/stage4/acceptance.py` | 需求第 6 节验收项逐条离线核对 |
| `tools/stage4/online_acceptance.py` | 授权在线验收（需要自备 Cookie，真实联网） |

## 使用归档工具的注意事项

这些工具**不再随产品维护**，直接运行大概率需要先修路径：

- 它们以 `tools.` 为顶层包名（如 `python -m tools.stage4.acceptance`），
  归档后包名已变为 `docs.archive.tools.`；仓库根目录下不再有 `tools/`。
- 部分工具用 `Path(__file__).resolve().parents[N]` 定位仓库根目录，
  目录深度变化后需要同步调整。
- `tools/stage4/acceptance.py` 会读取当时的 `README.md` / `DEVELOPMENT_PLAN.md` /
  `REQUIREMENTS.md` 并断言其中的章节与措辞；这些文件已重写或归档，
  该检查项在归档代码里必然失效。

需要复现某次验证时，建议直接从本目录读取当时的**结论与命令**，
再按当前代码重跑等价步骤，而不是直接执行归档脚本。

## 与当前仓库的关系

- `stage0-evidence/` 含脱敏响应样本，按 `.gitignore` 的既有决定**不入库**；
  它只在本机存在，`tests/test_render_wiring.py` 会在缺失时自动跳过相关用例。
- 归档不影响产品包 `subvideo/` 的运行：产品代码只依赖标准库与可选第三方包，
  从不导入 `tools/`。
