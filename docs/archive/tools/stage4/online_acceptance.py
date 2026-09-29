"""阶段 4 **授权在线验收**：用你自己的 Cookie 真实联网跑一次「首次采集 + 增量重跑」。

计划第 6.4 节把在线验收定义为**手动**动作（默认不进 CI、不保存凭据），本工具把那两条命令
与随后的核对固定下来，让验收结果可复现、可存档：

```text
① 首次采集：sync --latest N   → 条目目录 / metadata.json / 各类产物落盘
② 增量重跑：同一条命令再来一次 → ①里已完成的条目必须全部 skipped，目录集合不变
③ 核对：三类产物齐备、失败步骤带原因、产物里没有任何凭据值、index.json 键唯一
```

它**只使用你提供的 Cookie 与有权访问的账号**，不绕过任何权限控制，也不下载无权内容；
凭据只在内存里使用，报告里出现的所有文本都过 :class:`subvideo.redact.Redactor`。

```powershell
$env:SUBVIDEO_COOKIE = "SESSDATA=...; bili_jct=..."
python -m tools.stage4.online_acceptance --uid 1039025435 --latest 3 --preflight   # 只发现与筛选
python -m tools.stage4.online_acceptance --uid 1039025435 --latest 3               # 首次 + 增量
python -m tools.stage4.online_acceptance --uid 1039025435 --latest 3 --no-media    # 不下视频媒体
python -m tools.stage4.online_acceptance --uid 1039025435 --latest 3 --report .test_tmp/stage4_online/report.json
```

退出码：0 全部通过；1 有失败项；2 参数/配置错误；3 缺少 Cookie 或登录态失效；5 风控停止。
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
SECRET_RE = re.compile(
    r"(?i)\b(sessdata|bili_jct|dedeuserid|buvid3|b_lsid|bili_ticket|api[_-]?key"
    r"|authorization|password|secret|llm_api_key)\b\s*[=:]\s*([^\s,;\"'}]{6,})"
)


@dataclass
class Check:
    rid: str
    title: str
    evidence: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.problems:
            return FAIL
        return PASS if self.evidence else SKIP

    def ok(self, text: str) -> None:
        self.evidence.append(text)

    def fail(self, text: str) -> None:
        self.problems.append(text)

    def todo(self, text: str) -> None:
        self.pending.append(text)

    def to_json(self) -> dict:
        return {"id": self.rid, "title": self.title, "status": self.status,
                "evidence": self.evidence, "problems": self.problems, "pending": self.pending}


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_artifacts(author_dir: Path, redactor) -> Check:
    c = Check("online-1", "首次采集：三类产物按条目落盘，失败步骤带原因")
    index = _read_json(author_dir / "index.json") or {}
    entries = list(index.get("entries") or [])
    if not entries:
        c.fail(f"{author_dir.name}：index.json 没有条目")
        return c
    keys = [(e.get("kind"), e.get("platform_id")) for e in entries]
    dup = sorted(k for k, n in collections.Counter(keys).items() if n > 1)
    if dup:
        c.fail(f"index.json 存在重复条目：{dup}")
    else:
        c.ok(f"index.json 记录 {len(entries)} 条，键（类型, 平台 ID）唯一")
    kinds = collections.Counter(k for k, _ in keys)
    c.ok("三类分布：" + "、".join(f"{k}={kinds.get(k, 0)}"
                                 for k in ("dynamic", "video", "article")))
    for entry in entries:
        entry_dir = author_dir / str(entry.get("dir") or "")
        meta = _read_json(entry_dir / "metadata.json")
        if not isinstance(meta, dict):
            c.fail(f"{entry.get('dir')}：metadata.json 缺失或不可解析")
            continue
        kind = meta.get("kind")
        want = {"dynamic": "content.md", "article": "article.md"}.get(kind)
        if want and not (entry_dir / want).is_file():
            c.fail(f"{entry.get('dir')}：缺少 {want}")
        if kind == "video":
            has_text = (entry_dir / "transcript.txt").is_file()
            tstate = ((meta.get("steps") or {}).get("transcript") or {})
            if not has_text and not (tstate.get("reason") or tstate.get("error_kind")):
                c.fail(f"{entry.get('dir')}：既没有文字稿，也没记明原因")
            elif not has_text:
                c.todo(f"{entry.get('dir')}：无文字稿（{tstate.get('reason')}）——"
                       "需要时用 sync --asr 或 retry --steps transcript --asr 补做")
        for name, state in (meta.get("steps") or {}).items():
            if state.get("status") in ("failed", "skipped") and not (
                    state.get("reason") or state.get("error_kind") or state.get("message")):
                c.fail(f"{entry.get('dir')}:{name} 记 {state.get('status')} 但无原因")
    c.ok("每个条目的 failed/skipped 步骤都带 reason/error_kind/message（可机读重试依据）")
    hits = _scan_secrets(author_dir, redactor)
    if hits:
        c.fail(f"产物里发现凭据值 {len(hits)} 处：{hits[:3]}")
    else:
        c.ok("产物凭据自检：未发现任何 Cookie/密钥值")
    return c


def _scan_secrets(root: Path, redactor) -> list[str]:
    hits: list[str] = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".json", ".md", ".txt", ".mmd",
                                                            ".srt", ".toml"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if redactor is not None and redactor.contains_secret(text):
            hits.append(str(path.relative_to(root)))
            continue
        for match in SECRET_RE.finditer(text):
            value = match.group(2)
            if "REDACT" in value.upper() or value.startswith("["):
                continue
            hits.append(f"{path.relative_to(root)}：{match.group(1)}=…")
    return hits


def check_incremental(first: dict, second: dict, dirs_first: set[str], dirs_second: set[str]) -> Check:
    c = Check("online-2", "增量重跑：已完成的条目全部跳过，不产生重复条目/重复下载")
    done_keys = {(r.get("kind"), r.get("platform_id")) for r in first.get("results") or []
                 if r.get("outcome") == "done"}
    second_outcome = {(r.get("kind"), r.get("platform_id")): r.get("outcome")
                      for r in second.get("results") or []}
    redone = sorted(k for k in done_keys if second_outcome.get(k) != "skipped")
    if not done_keys:
        c.todo("首次采集没有 done 的条目（全部失败/跳过），无法验证增量重跑")
    elif redone:
        c.fail(f"首次已完成但第二次没有跳过（会重复下载/重复调用模型）：{redone[:3]}")
    else:
        c.ok(f"首次 done 的 {len(done_keys)} 条在第二次运行中全部 skipped")
    if dirs_first == dirs_second:
        c.ok(f"条目目录集合未变化（{len(dirs_first)} 个目录，未重复分配）")
    else:
        c.fail(f"第二次运行改变了条目目录集合：新增 {sorted(dirs_second - dirs_first)[:3]}，"
               f"消失 {sorted(dirs_first - dirs_second)[:3]}")
    if second.get("counts", {}).get("done"):
        c.todo(f"第二次运行仍有 {second['counts']['done']} 条被处理（首次未完成，属正常补做）")
    return c


def entry_dirs(author_dir: Path) -> set[str]:
    if not author_dir.exists():
        return set()
    return {p.name for p in author_dir.iterdir()
            if p.is_dir() and not p.name.startswith("_")}


def print_report(checks: list[Check], runs: list[dict], stream=sys.stdout) -> None:
    mark = {PASS: "✔ PASS", FAIL: "✘ FAIL", SKIP: "… SKIP"}
    print("", file=stream)
    print("=" * 78, file=stream)
    print("阶段 4 授权在线验收（真实联网，仅使用你提供的 Cookie）", file=stream)
    print("=" * 78, file=stream)
    for idx, run in enumerate(runs, 1):
        counts = run.get("counts") or {}
        print(f"第 {idx} 次运行 {run.get('mode')}：发现 {run.get('discovered')} 条 → "
              f"选中 {run.get('selected')} 条；结果 "
              + "、".join(f"{k}={v}" for k, v in counts.items())
              + f"；请求 {run.get('http_requests')} 次（风控 {run.get('risk_events')}）；"
              f"退出码 {run.get('exit_code')}", file=stream)
    for c in checks:
        print("", file=stream)
        print(f"[{mark[c.status]}] {c.rid}  {c.title}", file=stream)
        for line in c.evidence:
            print(f"    证据 · {line}", file=stream)
        for line in c.problems:
            print(f"    失败 · {line}", file=stream)
        for line in c.pending:
            print(f"    待验 · {line}", file=stream)
    counts = collections.Counter(c.status for c in checks)
    print("", file=stream)
    print("-" * 78, file=stream)
    print(f"结论：PASS {counts.get(PASS, 0)} / FAIL {counts.get(FAIL, 0)}"
          f" / SKIP {counts.get(SKIP, 0)}（共 {len(checks)} 项）", file=stream)
    print("=" * 78, file=stream)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stage4_online_acceptance",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--uid", type=int, help="目标 UID（默认取配置）")
    parser.add_argument("--out", dest="output_dir", help="输出目录（默认取配置）")
    parser.add_argument("--latest", type=int, default=3, help="跨三类取最新 N 条（默认 3）")
    parser.add_argument("--types", help="只验收这些类型（逗号分隔，默认全部）")
    parser.add_argument("--preflight", action="store_true",
                        help="只做发现与筛选（不落盘、不下载），用于确认会选中什么")
    parser.add_argument("--media", dest="download_media", action="store_true", default=None,
                        help="强制下载视频媒体")
    parser.add_argument("--no-media", dest="download_media", action="store_false",
                        help="不下载视频媒体（只归档元数据与文字稿）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出到 stdout")
    parser.add_argument("--report", help="把 JSON 报告写入该文件")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:  # 被重定向到 StringIO 时没有 reconfigure
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from subvideo.config import load_config
    from subvideo.errors import SubVideoError
    from subvideo.log import LogAdapter, setup_logger
    from subvideo.redact import Redactor
    from subvideo.runner import RunOptions, Runner

    overrides = {
        "uid": args.uid,
        "output_dir": args.output_dir,
        "latest": args.latest,
        "kinds": [k.strip() for k in args.types.split(",")] if args.types else None,
        "download_media": args.download_media,
    }
    try:
        loaded = load_config(root=ROOT, overrides=overrides)
    except SubVideoError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return exc.exit_code
    creds = loaded.credentials
    if not creds.has_cookie:
        print("缺少 Cookie：请设置环境变量 SUBVIDEO_COOKIE（或写入 config.local.toml 的 "
              "[account] cookie）。本工具不会打印或保存 Cookie 值。", file=sys.stderr)
        return 3

    redactor = Redactor.from_cookie(creds.cookie, [creds.llm_api_key])
    logger = setup_logger(args.verbose, False, redactor=redactor)
    adapter = LogAdapter(logger)
    cfg = loaded.config
    print(f"目标：UID {cfg.uid}；输出目录 {cfg.output_dir}；"
          f"筛选 {cfg.describe_filter()}；类型 {cfg.kind_labels}；"
          f"媒体下载 {'开启' if cfg.download_media else '关闭'}；"
          f"Cookie 来源 {creds.source}", file=sys.stderr)

    runs: list[dict] = []
    checks: list[Check] = []
    runner = Runner(loaded, logger=adapter, root=ROOT)
    try:
        first = runner.sync(RunOptions(dry_run=args.preflight))
    except SubVideoError as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return exc.exit_code
    runs.append(first.to_json())
    if args.preflight:
        checks.append(_preflight_check(first))
    else:
        author_dir = Path(cfg.output_dir) / first.author_dir
        dirs_first = entry_dirs(author_dir)
        try:
            second = Runner(loaded, logger=adapter, root=ROOT).sync(RunOptions())
        except SubVideoError as exc:
            print(f"增量重跑失败：{exc}", file=sys.stderr)
            return exc.exit_code
        runs.append(second.to_json())
        checks.append(check_artifacts(author_dir, redactor))
        checks.append(check_incremental(first.to_json(), second.to_json(),
                                        dirs_first, entry_dirs(author_dir)))
        checks.append(_exit_code_check(first, second))
        if first.exit_code == 4 or second.exit_code == 4:
            checks[-1].todo("有条目未完成（退出码 4）：逐条原因见运行摘要与 metadata.json，"
                            "补做命令 python -m subvideo retry --uid <UID>")

    payload = {
        "stage": 4,
        "mode": "preflight" if args.preflight else "online",
        "uid": cfg.uid,
        "output_dir": str(cfg.output_dir),
        "runs": runs,
        "checks": [c.to_json() for c in checks],
        "summary": dict(collections.Counter(c.status for c in checks)),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_report(checks, runs)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.json:
            print(f"报告已写入：{path}")
    return 1 if any(c.status == FAIL for c in checks) else 0


def _preflight_check(summary) -> Check:
    c = Check("online-0", "预检：登录态、作者信息与将选中的条目")
    data = summary.to_json()
    if data.get("author"):
        c.ok(f"登录态有效，作者可读：{data['author']}（UID {data['uid']}）")
    else:
        c.fail("未读到作者信息")
    c.ok(f"发现 {data.get('discovered')} 条 → 选中 {data.get('selected')} 条"
         f"（{data.get('date_from') or '不限'} ~ {data.get('date_to') or '不限'}，"
         f"最新 N = {data.get('latest') or '不限'}）")
    for note in (data.get("notes") or [])[:5]:
        c.ok(f"发现说明：{note}")
    c.todo("预检不落盘；确认选中范围后再去掉 --preflight 跑首次采集 + 增量重跑")
    return c


def _exit_code_check(first, second) -> Check:
    c = Check("online-3", "退出码与失败可重试语义")
    for idx, summary in ((1, first), (2, second)):
        code = summary.exit_code
        if code == 0:
            c.ok(f"第 {idx} 次运行退出码 0（全部成功）")
        elif code == 4:
            c.ok(f"第 {idx} 次运行退出码 4（运行完成但有条目未完成，原因已逐条记录）")
        else:
            c.fail(f"第 {idx} 次运行退出码 {code}（预期 0 或 4）")
    return c


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
