"""阶段 2 验证工具：把**已归档的真实动态**渲染成单张长 PNG。

用途（计划第 6.3 节"本机功能测试"）：离线验收动态长图排版 —— 用真实的
``.smoke_sync`` 产物（真中文长文 + 真配图 + 真头像）跑一遍渲染，检查
PNG 非空、中文没被裁掉、图片顺序与正文一致。

它**不联网、不重新归档**：只读条目目录里的 ``content.md`` 与 ``metadata.json``，
把 markdown 还原成渲染项后调用 :func:`subvideo.render.render_long_png`。

```powershell
python -m tools.stage2.render_check                       # 自动挑一条带配图的动态
python -m tools.stage2.render_check --entry "output/1039025435_xxx/2026-09-22_动态_xxx"
python -m tools.stage2.render_check --list                # 只列出候选条目
```

退出码：0 成功；2 找不到条目；1 渲染失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: markdown 图片引用：``![alt](path)``
IMAGE_RE = re.compile(r"^!\[[^\]]*\]\(([^)]+)\)\s*$")
HEADING_RE = re.compile(r"^(#{2,6})\s+(.*)$")


def find_entries(root: Path) -> list[Path]:
    """在输出根目录下找出所有"动态"条目目录（含 content.md 且带 images/）。"""
    out: list[Path] = []
    if not root.exists():
        return out
    for child in sorted(root.rglob("*")):
        if not child.is_dir() or not child.name.startswith("20"):
            continue
        if "_动态_" in child.name and (child / "content.md").is_file():
            out.append(child)
    return out


def pick_entry(entries: list[Path]) -> Path | None:
    """优先挑"图片最多"的那条，长图排版才有代表性。"""
    scored = []
    for entry in entries:
        images = entry / "images"
        count = len([p for p in images.glob("*") if p.is_file()]) if images.is_dir() else 0
        scored.append((count, len((entry / "content.md").read_text(encoding="utf-8")), entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2] if scored else None


def load_header(entry: Path):
    """从 ``metadata.json`` 组装 :class:`RenderHeader`。"""
    from subvideo.render import RenderHeader

    meta_path = entry / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    extra = meta.get("extra") or {}
    flags = meta.get("flags") or {}
    author = (extra.get("author") or {}).get("name") or meta.get("author") or ""
    stat = extra.get("stat") or {}
    stats = ""
    if stat:
        stats = (f"点赞 {stat.get('like')} / 评论 {stat.get('comment')}"
                 f" / 转发 {stat.get('forward')}")
    avatar = ""
    images_dir = entry / "images"
    if images_dir.is_dir():
        faces = sorted(images_dir.glob("avatar.*"))
        if faces:
            avatar = str(faces[0])
    badge = extra.get("badge") or ("置顶" if flags.get("pinned") else "")
    return RenderHeader(
        author=author,
        avatar=avatar,
        published=str(extra.get("pub_time") or ""),
        title=str(meta.get("title") or ""),
        url=str(meta.get("url") or ""),
        badge=badge,
        visible="充电专属" if extra.get("is_only_fans") else "公开",
        stats=stats,
        forward_note="本条为转发动态" if extra.get("forward_images") else "",
    )


def content_to_items(entry: Path) -> list:
    """把 ``content.md`` 的正文部分还原成渲染项（正文与配图保持原顺序）。

    只取第一个 ``## `` 标题之后的内容：前面的 ``# 标题`` 与 ``- 元信息`` 已经
    由 :func:`load_header` 从 metadata 提供了。
    """
    from subvideo.render import RenderItem

    text = (entry / "content.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))

    items: list = []
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        block = "\n".join(buffer).strip()
        if block:
            items.append(RenderItem(kind="text", text=block))
        buffer = []

    for line in lines[start:]:
        stripped = line.strip()
        image = IMAGE_RE.match(stripped)
        if image:
            flush()
            target = image.group(1).strip()
            if target.startswith(("http://", "https://")):
                items.append(RenderItem(kind="image", caption=f"（未下载的配图：{target}）"))
                continue
            path = (entry / target).resolve()
            if path.is_file():
                items.append(RenderItem(kind="image", image=str(path), caption=target))
            else:
                items.append(RenderItem(kind="image", caption=f"（配图缺失：{target}）"))
            continue
        heading = HEADING_RE.match(stripped)
        if heading:
            flush()
            items.append(RenderItem(kind="heading", text=heading.group(2).strip(),
                                    level=len(heading.group(1))))
            continue
        if stripped.startswith("（") and stripped.endswith("）"):
            flush()
            items.append(RenderItem(kind="note", text=stripped))
            continue
        if stripped.startswith(">"):
            flush()
            items.append(RenderItem(kind="note", text=stripped.lstrip("> ").strip()))
            continue
        if not stripped:
            flush()
            continue
        buffer.append(line)
    flush()
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render_check", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(ROOT / ".smoke_sync"),
                        help="输出根目录（默认 .smoke_sync）")
    parser.add_argument("--entry", help="指定条目目录（默认自动挑一条配图最多的动态）")
    parser.add_argument("--out", help="输出 PNG 路径（默认 <entry>/render.png）")
    parser.add_argument("--width", type=int, default=1080, help="图片宽度（默认 1080）")
    parser.add_argument("--max-height", type=int, default=20000, help="高度上限")
    parser.add_argument("--list", action="store_true", help="只列出候选条目")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    entries = find_entries(Path(args.root))
    if args.list or not entries:
        print(f"候选动态条目 {len(entries)} 个（{args.root}）：")
        for entry in entries:
            images = entry / "images"
            count = len([p for p in images.glob("*") if p.is_file()]) if images.is_dir() else 0
            print(f"  [配图 {count:2d}] {entry}")
        return 0 if entries else 2

    entry = Path(args.entry) if args.entry else pick_entry(entries)
    if entry is None or not (entry / "content.md").is_file():
        print(f"找不到可渲染的动态条目：{entry}", file=sys.stderr)
        return 2

    from subvideo.render import render_long_png

    header = load_header(entry)
    items = content_to_items(entry)
    out_path = Path(args.out) if args.out else entry / "render.png"
    result = render_long_png(header, items, out_path, width=args.width,
                             max_height=args.max_height)

    print(f"条目：{entry}")
    print(f"作者：{header.author}    发布：{header.published}")
    print(f"渲染项：{len(items)} 个（图片 {sum(1 for i in items if i.kind == 'image')} 张）")
    print(f"结果：ok={result.ok} {result.width}×{result.height} "
          f"{result.bytes_written} 字节  图片 {result.images_rendered} 张 / "
          f"缺失 {result.images_missing} 张  截断={result.clamped}")
    if result.message:
        print(f"说明：{result.message}")
    if not result.ok:
        print(f"失败：{result.error_kind} {result.message}", file=sys.stderr)
        return 1
    print(f"产物：{out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
