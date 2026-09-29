"""bili-sub-archive —— B 站 UP 主内容归档工具。

能力范围：

- **发现与归档**：CLI/TOML、采集适配层、时间筛选、跨类最新 N、索引/状态，
  动态与专栏归档（含配图原图）；
- **媒体与本地文字**：视频分 P 并发下载与续传、可播放校验、平台字幕提取、
  本地 ASR 兜底（默认关闭）、跨 P 文字稿合并、动态单张长 PNG；
- **总结与导图**：OpenAI 兼容接口的分块总结（长稿不丢尾段）、自定义 prompt、
  受控大纲、Mermaid ``mindmap.mmd`` 与 ``mindmap.png``（PNG 失败保留源文件可重试）。

设计与契约依据（原始过程记录已从仓库移除，结论收拢在开发文档里）：

- ``docs/DEVELOPMENT.md`` 第 3 节：技术方案（模块数据流、接口白名单、适配层契约、关键决策）
- ``docs/DEVELOPMENT.md`` 第 4 节：阶段 0~4 的实施与验证结论、已知限制
"""

from __future__ import annotations

__version__ = "0.1.1"

#: ``metadata.json`` / ``index.json`` 的格式版本。
#: 2 = 阶段 2：步骤表新增 ``media``/``transcript``/``render`` 的实际实现，
#: 并在 ``extra`` 里新增 ``media`` / ``transcript`` / ``render`` 三组结构；
#: 3 = 阶段 3：``summary``/``mindmap`` 落地，``extra`` 新增 ``summary``
#: （含模型、prompt 指纹、分块与导图统计）。
#: 后续新增字段一律是附加的，因此**不升版本**；
#: 旧产物无需迁移（读取侧按缺省值兜底）。
FORMAT_VERSION = 3

__all__ = ["__version__", "FORMAT_VERSION"]
