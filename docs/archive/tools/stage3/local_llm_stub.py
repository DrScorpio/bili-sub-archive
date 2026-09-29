"""阶段 3 验证工具：本地 OpenAI 兼容**替身服务**，用来跑通真实的 HTTP 总结链路。

目的（计划第 6.3 节"本机功能测试"）：在没有云端模型、也不想花钱的前提下，验证

1. 真实的 ``urllib`` 传输链路：POST ``/v1/chat/completions``、请求头、JSON 载荷、响应解析；
2. 分块 → 逐块要点 → 合并总结的编排在**真实长文字稿**（.smoke_sync 里 9000+ 字的真产物）
   上确实不丢尾段（替身会把每块结尾原文带进要点，最终总结里能查到）；
3. 只发送文字稿：替身把每次收到的请求记进 JSONL，可直接核对"没有 Cookie / 没有媒体地址"。

两种用法：

```powershell
# 1) 只当服务跑（另开一个终端用 subvideo 指过来）
python -m tools.stage3.local_llm_stub --port 8799 --log .test_tmp/stub_llm.jsonl
python -m subvideo retry --uid 1039025435 --steps summary,mindmap `
  --summary-base-url http://127.0.0.1:8799/v1 --summary-model stub-1

# 2) 自检模式：起服务 → 拿真实条目的 transcript.txt 跑完整总结与导图 → 打印报告
python -m tools.stage3.local_llm_stub --check-entry ".smoke_sync/1039025435_xxx/2026-09-16_视频_xxx"
```

退出码：0 链路验证通过；2 参数/条目问题；1 链路失败（尾段丢失 / 产物结构不对 / 请求里出现凭据）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 与 summarize.prompt.DEFAULT_MAP 一致的分块识别标记
MAP_MARKER = "请只针对**这一部分**"
TRANSCRIPT_BEGIN = "----- 文字稿开始 -----"
TRANSCRIPT_END = "----- 文字稿结束 -----"
POINTS_BEGIN = "----- 分段要点开始 -----"
POINTS_END = "----- 分段要点结束 -----"
#: 只可能出现在正文里的敏感串（用于自检"没把凭据发给模型"）
SECRET_HINTS = ("SESSDATA", "bili_jct", "Authorization", "Cookie")

PAGE_HEADING_RE = re.compile(r"^##\s*P\d+", re.MULTILINE)


def extract_transcript(user: str) -> str:
    """取 prompt 里被标记包裹的内容：先找"分段要点"，再找"文字稿"，都没有则整段。"""
    for begin, end in ((POINTS_BEGIN, POINTS_END), (TRANSCRIPT_BEGIN, TRANSCRIPT_END)):
        if begin in user and end in user:
            return user.split(begin, 1)[1].split(end, 1)[0].strip()
    return user.strip()


def stub_reply(user: str) -> str:
    """确定性替身回复：分块要点回要点，合并阶段回"摘要 + 大纲"。

    **把收到内容的结尾原样带回**：这样自检脚本能证明"最后一块的尾段确实
    经过 分块 → 要点 → 合并 一路进到了最终总结里"（不丢尾段的端到端证据）。
    """
    section = " ".join(extract_transcript(user).split())
    tail = section[-60:]
    if MAP_MARKER in user:
        return f"- 本段要点（替身）\n- 本段结尾原文：{tail}"
    return ("## 摘要\n\n"
            f"替身模型依据文字稿给出的摘要。结尾内容：{tail}\n\n"
            "## 大纲\n"
            f"- 主题：替身主题：{section[-16:]}\n"
            "  - 第一部分要点\n"
            "    - 细节\n"
            "  - 第二部分要点\n")


class _Handler(BaseHTTPRequestHandler):
    server_version = "SubVideoStubLLM/1.0"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        messages = payload.get("messages") or []
        user = str(messages[-1].get("content") or "") if messages else ""
        text = stub_reply(user)
        self.server.record(payload, user, self.headers.get("Authorization") or "")
        body = json.dumps({
            "model": str(payload.get("model") or "stub-1"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": max(1, len(user) // 3),
                      "completion_tokens": max(1, len(text) // 3)},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # pragma: no cover - 静音访问日志
        return


class StubServer:
    """带请求记录与调用次数上限的替身服务。"""

    def __init__(self, *, port: int = 0, log: Path | None = None, max_requests: int = 0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.httpd.record = self._record          # type: ignore[attr-defined]
        self.httpd.allow_reuse_address = True
        self.port = int(self.httpd.server_address[1])
        self.log = Path(log) if log else None
        self.max_requests = int(max_requests or 0)
        self.requests: list[dict] = []
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _record(self, payload: dict, user: str, auth: str) -> None:
        messages = payload.get("messages") or []
        entry = {
            "n": len(self.requests) + 1,
            "model": payload.get("model"),
            "temperature": payload.get("temperature"),
            "stream": payload.get("stream"),
            "messages": len(messages),
            "system_len": len(str(messages[0].get("content") or "")) if messages else 0,
            "user_len": len(user),
            "authorization": auth,
            "leaked": [hint for hint in SECRET_HINTS if hint.lower() in user.lower()],
            "tail": " ".join(user.split())[-60:],
        }
        with self._lock:
            self.requests.append(entry)
            if self.log is not None:
                self.log.parent.mkdir(parents=True, exist_ok=True)
                with self.log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            reached = self.max_requests and len(self.requests) >= self.max_requests
        if reached:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def start(self) -> "StubServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# 自检模式
# --------------------------------------------------------------------------- #
def find_entries(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.exists():
        return out
    for child in sorted(root.rglob("transcript.txt")):
        entry = child.parent
        if "_视频_" in entry.name:
            out.append(entry)
    return out


def transcript_of(entry: Path) -> str:
    text = (entry / "transcript.txt").read_text(encoding="utf-8", errors="replace")
    match = PAGE_HEADING_RE.search(text)
    return text[match.start():].strip() if match else text.strip()


def run_check(entry: Path, *, out_dir: Path, chunk_chars: int, log: Path) -> int:
    from subvideo.config import Config
    from subvideo.summarize import MINDMAP_MMD, SUMMARY_MD, build_mindmap, build_summary
    from subvideo.summarize.mermaid_cli import probe_mmdc

    server = StubServer(log=log).start()
    try:
        config = Config(uid=1, summary_base_url=server.base_url, summary_model="stub-1",
                        summary_chunk_chars=chunk_chars, summary_client="http",
                        summary_overlap_chars=200, mindmap_max_nodes=40)
        text = transcript_of(entry)
        print(f"条目：{entry}")
        print(f"文字稿正文：{len(text)} 字（{text.count('## P')} 个分 P 小节）")
        print(f"替身服务：{server.base_url}（记录写入 {log}）")
        plan = build_summary(transcript_text=text, config=config, entry_dir=out_dir,
                             title=entry.name, author="(自检)")
        print(f"总结：status={plan.status} chunks={plan.chunks} calls={plan.calls} "
              f"tokens={plan.prompt_tokens}/{plan.completion_tokens}")
        if plan.status != "done":
            print(f"失败：{plan.reason} {plan.message}", file=sys.stderr)
            return 1
        plan = build_mindmap(plan=plan, config=config, entry_dir=out_dir, title=entry.name)
        print(f"导图：status={plan.mindmap_status} nodes={plan.mindmap_nodes} "
              f"depth={plan.outline_depth} outline={plan.outline_source} "
              f"mmdc={probe_mmdc(config) or '未找到'}")

        tail_marker = " ".join(text.split())[-60:]
        summary_text = (out_dir / SUMMARY_MD).read_text(encoding="utf-8")
        mmd_text = (out_dir / MINDMAP_MMD).read_text(encoding="utf-8")
        leaked = sum(1 for r in server.requests if r["leaked"])
        tail_ok = tail_marker in summary_text
        mmd_ok = "mindmap" in mmd_text and "root((" in mmd_text
        print(f"产物：{out_dir / SUMMARY_MD} / {out_dir / MINDMAP_MMD}")
        print(f"尾段是否进了最终总结：{'是 ✔' if tail_ok else '否 ✘'}")
        print(f"mindmap.mmd 结构：{'通过 ✔' if mmd_ok else '不通过 ✘'}")
        print(f"替身收到调用 {len(server.requests)} 次；"
              f"请求里出现凭据的次数：{leaked}")
        print(f"最后一次请求的结尾：{server.requests[-1]['tail'] if server.requests else '(无)'}")
        print("--- mindmap.mmd ---")
        for line in mmd_text.splitlines():
            print(line)
        if plan.mindmap_status != "done":
            print("说明：mindmap.mmd 已生成；PNG 需要 mermaid-cli（本机未安装），"
                  "装上后可用 tools.stage3.mindmap_check 验证渲染")
        return 0 if (tail_ok and mmd_ok and not leaked) else 1
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local_llm_stub", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8799, help="监听端口（0 = 随机，默认 8799）")
    parser.add_argument("--log", default=str(ROOT / ".test_tmp" / "stage3_stub_llm.jsonl"),
                        help="请求记录 JSONL 路径")
    parser.add_argument("--max-requests", type=int, default=0,
                        help="收到 N 次请求后自动退出（0 = 一直跑）")
    parser.add_argument("--check-entry", help="自检模式：指定条目目录（用它的 transcript.txt）")
    parser.add_argument("--root", default=str(ROOT / ".smoke_sync"), help="候选条目根目录")
    parser.add_argument("--out-dir", default=str(ROOT / ".test_tmp" / "stage3_selfcheck"),
                        help="自检产物目录")
    parser.add_argument("--chunk-chars", type=int, default=3000, help="每块字数（默认 3000）")
    parser.add_argument("--list", action="store_true", help="列出候选条目后退出")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    log = Path(args.log)

    if args.list:
        entries = find_entries(Path(args.root))
        print(f"候选视频条目 {len(entries)} 个：")
        for entry in entries:
            print(f"  [{(entry / 'transcript.txt').stat().st_size:>7d} B] {entry}")
        return 0 if entries else 2

    if args.check_entry:
        entry = Path(args.check_entry)
        if not (entry / "transcript.txt").is_file():
            print(f"条目里没有 transcript.txt：{entry}", file=sys.stderr)
            return 2
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if log.exists():
            log.unlink()
        return run_check(entry, out_dir=out_dir, chunk_chars=args.chunk_chars, log=log)

    server = StubServer(port=args.port, log=log, max_requests=args.max_requests).start()
    print(f"替身 OpenAI 兼容服务已启动：{server.base_url}")
    print(f"请求记录：{log}")
    print("在另一个终端执行：")
    print(f'  python -m subvideo sync --uid <UID> --summary-base-url {server.base_url} '
          '--summary-model stub-1')
    print("按 Ctrl+C 退出。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    print(f"共收到 {len(server.requests)} 次请求。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
