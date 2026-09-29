"""阶段 3 验证工具：用**真实归档产物**验证 ``mindmap.mmd`` 与 PNG 渲染链路。

用途（计划第 6.3 节"本机功能测试"）：离线验收思维导图 —— 拿 ``.smoke_sync`` 里
真实的视频条目（真中文文字稿）跑一遍"大纲 → ``.mmd`` → ``mindmap.png``"，
检查中文标签没被转义搞坏、文件语法行结构正确、PNG 非空且有合理尺寸。

它**不联网、不调用 LLM**：

- 条目已有 ``summary.md``（真的跑过总结）→ 用它的正文；
- 只有 ``transcript.txt`` → 用文字稿段落首句派生一份确定性大纲，
  这样没有 LLM 也能验证"序列化 + 渲染"这条链路（内容质量不是本工具的目标）。

产物默认写到 ``--out-dir``（默认 ``.test_tmp/stage3_mindmap``），**不动归档目录**。

```powershell
python -m tools.stage3.mindmap_check                        # 自动挑文字稿最长的一条
python -m tools.stage3.mindmap_check --list                 # 只列候选条目
python -m tools.stage3.mindmap_check --entry ".smoke_sync/1039025435_xxx/2026-09-16_视频_xxx"
python -m tools.stage3.mindmap_check --mmdc "C:\\Users\\me\\AppData\\Roaming\\npm\\mmdc.cmd"
```

退出码：0 成功；2 找不到条目；1 ``.mmd`` 生成失败或渲染失败。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 导图正文里不该出现的 Mermaid 语法字符（标签已被全角化）
FORBIDDEN = set('()[]{}"#%|<>\\`;:')

PAGE_HEADING_RE = re.compile(r"^##\s*P\d+", re.MULTILINE)
META_LINE_RE = re.compile(r"^[>-]\s")


def find_entries(root: Path) -> list[Path]:
    """输出根目录下所有"视频"条目目录（必须有 transcript.txt 或 summary.md）。"""
    out: list[Path] = []
    if not root.exists():
        return out
    for child in sorted(root.rglob("*")):
        if not child.is_dir() or not child.name.startswith("20") or "_视频_" not in child.name:
            continue
        if (child / "transcript.txt").is_file() or (child / "summary.md").is_file():
            out.append(child)
    return out


def pick_entry(entries: list[Path]) -> Path | None:
    """挑文字稿最长的一条：中文标签与节点数量才有代表性。"""
    def size(entry: Path) -> int:
        path = entry / "transcript.txt"
        return path.stat().st_size if path.is_file() else 0

    return max(entries, key=size) if entries else None


def transcript_to_markdown(entry: Path, *, paragraphs: int = 8,
                           paragraph_chars: int = 150) -> str:
    """把 ``transcript.txt`` 的正文按 ~150 字切段，作为派生大纲的"摘要"。

    字幕本身没有标点边界，所以这里不按句号切，而是按长度切段 —— 目的是让
    "序列化 + 渲染"这条链路拿到多个真实中文节点，而不是一个巨长节点。
    """
    text = (entry / "transcript.txt").read_text(encoding="utf-8", errors="replace")
    body = text
    match = PAGE_HEADING_RE.search(text)
    if match:
        body = text[match.start():]
    chunks: list[str] = []
    buffer = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or META_LINE_RE.match(line):
            continue
        buffer += line
        if len(buffer) >= paragraph_chars:
            chunks.append(buffer)
            buffer = ""
    if buffer:
        chunks.append(buffer)
    picked = chunks[:paragraphs] or ["文字稿过短，无法派生大纲"]
    return "## 摘要\n\n" + "\n\n".join(f"{s}。" for s in picked) + "\n"


def load_markdown(entry: Path) -> tuple[str, str]:
    """优先用真实总结；否则用文字稿派生（返回 ``(markdown, 来源说明)``）。"""
    from subvideo.summarize import read_summary_body

    summary = entry / "summary.md"
    if summary.is_file():
        body = read_summary_body(summary)
        if body:
            return body, "summary.md（真实总结）"
    return transcript_to_markdown(entry), "transcript.txt（派生大纲，未调用 LLM）"


def entry_title(entry: Path) -> str:
    """条目标题：优先 metadata.json 的 title，回落目录名。"""
    import json

    meta_path = entry / "metadata.json"
    if meta_path.is_file():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if data.get("title"):
                return str(data["title"])
        except (OSError, ValueError):
            pass
    return entry.name


def check_mmd(text: str) -> list[str]:
    """检查 ``.mmd`` 的结构与转义（返回问题列表，空 = 通过）。"""
    problems: list[str] = []
    lines = [line for line in text.splitlines() if not line.startswith("%%")]
    if not lines or lines[0].strip() != "mindmap":
        problems.append("首行不是 mindmap")
    if len(lines) < 2 or "root((" not in lines[1]:
        problems.append("缺少 root((...)) 根节点")
    else:
        label = lines[1].split("root((", 1)[1].rsplit("))", 1)[0]
        bad = FORBIDDEN & set(label)
        if bad:
            problems.append(f"根节点标签残留 Mermaid 语法字符 {sorted(bad)}：{label!r}")
    for line in lines[2:]:
        if not line.startswith("  "):
            problems.append(f"节点缩进异常：{line!r}")
            break
        bad = FORBIDDEN & set(line)
        if bad:
            problems.append(f"标签里残留 Mermaid 语法字符 {sorted(bad)}：{line!r}")
            break
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mindmap_check", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(ROOT / ".smoke_sync"),
                        help="输出根目录（默认 .smoke_sync）")
    parser.add_argument("--entry", help="指定条目目录（默认自动挑文字稿最长的一条）")
    parser.add_argument("--out-dir", default=str(ROOT / ".test_tmp" / "stage3_mindmap"),
                        help="预览产物目录（默认 .test_tmp/stage3_mindmap）")
    parser.add_argument("--mmdc", default="", help="mermaid-cli 路径（默认从 PATH 查找）")
    parser.add_argument("--width", type=int, default=1600, help="PNG 宽度（默认 1600）")
    parser.add_argument("--max-nodes", type=int, default=60, help="节点上限（默认 60）")
    parser.add_argument("--max-depth", type=int, default=3, help="深度上限（默认 3）")
    parser.add_argument("--list", action="store_true", help="只列出候选条目")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    entries = find_entries(Path(args.root))
    if args.list or not entries:
        print(f"候选视频条目 {len(entries)} 个（{args.root}）：")
        for entry in entries:
            text = entry / "transcript.txt"
            print(f"  [文字稿 {text.stat().st_size if text.is_file() else 0:>7d} B] {entry}")
        return 0 if entries else 2

    entry = Path(args.entry) if args.entry else pick_entry(entries)
    if entry is None or not entry.is_dir():
        print(f"找不到可验证的视频条目：{entry}", file=sys.stderr)
        return 2

    from subvideo.paths import atomic_write_text, ensure_dir
    from subvideo.summarize import MINDMAP_PNG, SummaryPlan, build_mindmap
    from subvideo.summarize.mermaid import MMD_NAME
    from subvideo.summarize.mermaid_cli import probe_mmdc

    markdown, source = load_markdown(entry)
    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)

    class _Config:
        """只提供 build_mindmap 需要的字段（本工具不碰归档配置）。"""

        mindmap_enabled = True
        mindmap_mmdc_path = args.mmdc
        mindmap_max_nodes = args.max_nodes
        mindmap_max_depth = args.max_depth
        mindmap_label_chars = 24
        mindmap_width = args.width
        mindmap_background = "white"
        mindmap_timeout_seconds = 120.0
        mindmap_puppeteer_config = ""

    plan = SummaryPlan(status="done", text=markdown, signature="check", model="(未调用)")
    plan = build_mindmap(plan=plan, config=_Config(), entry_dir=out_dir,
                         title=entry_title(entry))

    mmd_path = out_dir / MMD_NAME
    text = mmd_path.read_text(encoding="utf-8") if mmd_path.is_file() else ""
    problems = check_mmd(text) if text else ["没有生成 .mmd"]

    print(f"条目：{entry}")
    print(f"大纲来源：{source}；outline_source={plan.outline_source}")
    print(f"节点：{plan.mindmap_nodes} 个 / {plan.outline_depth} 层"
          f"（省略 {plan.outline_dropped} 个）")
    print(f"mmdc：{probe_mmdc(_Config()) or '未找到'}")
    print(f"结构检查：{'通过 ✔' if not problems else '不通过 ✘ ' + '；'.join(problems)}")
    print(f"产物目录：{out_dir}")
    print("--- .mmd 前 12 行 ---")
    for line in text.splitlines()[:12]:
        print(line)

    if not text or problems:
        atomic_write_text(out_dir / "mindmap_check_error.txt", "\n".join(problems))
        print("失败：.mmd 结构检查不通过", file=sys.stderr)
        return 1

    if plan.mindmap_status != "done":
        print(f"渲染：未完成（{plan.mindmap_reason}）{plan.mindmap_message}", file=sys.stderr)
        print("说明：mindmap.mmd 已生成并通过结构检查；装上 mermaid-cli 后可重跑本工具验证 PNG。")
        return 1
    print(f"PNG：{out_dir / MINDMAP_PNG}（{plan.mindmap_width}×{plan.mindmap_height}，"
          f"{plan.mindmap_bytes / 1024:.1f} KiB）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
