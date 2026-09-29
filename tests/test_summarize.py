"""阶段 3 单元测试：分块 / 大纲 / Mermaid / LLM 客户端 / mmdc 渲染 / prompt / 编排。

全部离线：模型调用走注入的 ``transport`` 或替身，渲染走假 ``mmdc`` 执行器，
**不发任何网络请求、不调用真实 mermaid-cli**。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from bili_sub_archive.config import Config
from bili_sub_archive.summarize import (
    SUMMARY_BEGIN,
    SUMMARY_END,
    MINDMAP_MMD,
    MINDMAP_PNG,
    SUMMARY_MD,
    SummaryPlan,
    build_mindmap,
    build_summary,
    mindmap_signature,
    read_summary_body,
    summary_signature,
    transcript_digest,
)
from bili_sub_archive.summarize import chunk as ck
from bili_sub_archive.summarize import llm as llm_mod
from bili_sub_archive.summarize import mermaid as mm
from bili_sub_archive.summarize import mermaid_cli as mc
from bili_sub_archive.summarize import mindmap_style as mst
from bili_sub_archive.summarize import outline as ol
from bili_sub_archive.summarize import prompt as pm
from tests import workspace
from tests import fixtures as fx


# --------------------------------------------------------------------------- #
# 文字稿样本
# --------------------------------------------------------------------------- #
def transcript_text(pages: int = 2, rows_per_page: int = 3, filler: str = "正文内容") -> str:
    """造一份与 ``transcript.txt`` 同形状的合并文字稿（头部 + ``## Pxx`` 分节）。

    每段正文默认 50 字左右：既超过 ``[summary].min_chars`` 的默认值（200），
    又不会在小 ``chunk_chars`` 下碎成太多块。
    """
    lines = ["# 合并文字稿", "", "- 时间基准：各 P 内部计时", f"- 分 P 数：{pages}", ""]
    for page in range(1, pages + 1):
        lines += [f"## P{page:02d} 第{page}部分", "",
                  f"> 来源：平台字幕（中文（AI 生成））；{rows_per_page} 段 / 10 字", ""]
        for index in range(rows_per_page):
            lines.append(f"P{page:02d}第{index}段{filler * 6}{index:02d}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


class ChunkTest(unittest.TestCase):
    def test_header_and_body_split(self):
        header, body = ck.split_header(transcript_text())
        self.assertIn("# 合并文字稿", header)
        self.assertNotIn("## P01", header)
        self.assertTrue(body.startswith("## P01"))
        self.assertIn("P02第2段", body)

    def test_no_page_heading_means_no_header(self):
        header, body = ck.split_header("纯文本文字稿，没有分 P 标题")
        self.assertEqual(header, "")
        self.assertEqual(body, "纯文本文字稿，没有分 P 标题")

    def test_short_transcript_is_one_chunk(self):
        plan = ck.split_transcript(transcript_text(), chunk_chars=6000)
        self.assertEqual(plan.count, 1)
        self.assertFalse(plan.scaled)
        self.assertEqual(plan.chunks[0].pages, [1, 2])
        self.assertEqual(plan.to_json()["pages"], [1, 2])

    def test_chunks_cover_body_without_gaps(self):
        plan = ck.split_transcript(transcript_text(rows_per_page=40), chunk_chars=300)
        self.assertGreater(plan.count, 2)
        self.assertEqual(ck.uncovered_ranges(len(plan.body), plan.chunks), [],
                         "正文必须被分块完整覆盖（不丢尾段）")

    def test_pages_are_attributed_per_chunk(self):
        plan = ck.split_transcript(transcript_text(pages=3, rows_per_page=20), chunk_chars=300)
        seen = {page for chunk in plan.chunks for page in chunk.pages}
        self.assertEqual(seen, {1, 2, 3})
        self.assertTrue(all(chunk.pages_label().startswith("P") for chunk in plan.chunks))

    def test_overlap_is_applied_and_capped(self):
        plan = ck.split_transcript(transcript_text(rows_per_page=40), chunk_chars=400,
                                   overlap_chars=100)
        self.assertGreater(plan.count, 1)
        self.assertGreater(plan.chunks[1].overlap_chars, 0)
        self.assertEqual(plan.chunks[0].overlap_chars, 0)
        for chunk in plan.chunks:
            self.assertLessEqual(chunk.overlap_chars, plan.chunk_chars // 4)

    def test_too_many_chunks_scales_instead_of_dropping_tail(self):
        """块数超 max_chunks 时放大每块，而不是丢掉最后几个分 P。"""
        plan = ck.split_transcript(transcript_text(pages=2, rows_per_page=60),
                                   chunk_chars=300, max_chunks=3)
        self.assertTrue(plan.scaled)
        self.assertIn("不丢尾段", plan.note)
        self.assertIn("实际", plan.note)
        self.assertLessEqual(plan.count, 5)          # 块边界落在行上，允许略多于上限
        self.assertEqual(ck.uncovered_ranges(len(plan.body), plan.chunks), [])
        self.assertIn("P02第59段", plan.chunks[-1].text, "尾段必须在最后一块里")

    def test_giant_single_block_is_hard_split(self):
        body = "## P01 长块\n\n" + "甲" * 5000
        plan = ck.split_transcript(body, chunk_chars=400, max_chunks=100)
        self.assertGreater(plan.count, 5)
        self.assertEqual(ck.uncovered_ranges(len(plan.body), plan.chunks), [])
        for chunk in plan.chunks:
            self.assertLessEqual(chunk.chars, plan.chunk_chars + plan.overlap_chars + 8)

    def test_format_points_labels_each_part(self):
        plan = ck.split_transcript(transcript_text(pages=2, rows_per_page=20), chunk_chars=300)
        text = ck.format_points([f"要点{i}" for i in range(plan.count)], plan.chunks)
        self.assertIn("### 第 1/", text)
        self.assertIn("分 P：P01", text)

    def test_empty_body(self):
        plan = ck.split_transcript("")
        self.assertEqual(plan.count, 0)
        self.assertEqual(plan.body_chars, 0)


class OutlineTest(unittest.TestCase):
    MODEL_DOC = (
        "## 摘要\n\n第一段摘要。\n\n第二段摘要。\n\n"
        "## 大纲\n"
        "- 主题：美联储与利率\n"
        "  - 点阵图偏鸽\n"
        "    - 2026 年降息两次\n"
        "      - 更深的第四层（应被裁掉）\n"
        "  - 期限溢价\n"
    )

    def test_extract_outline_uses_theme_as_root(self):
        root = ol.extract_outline(self.MODEL_DOC, max_depth=3, max_nodes=60, label_chars=24)
        self.assertIsNotNone(root)
        self.assertEqual(root.label, "美联储与利率")
        labels = [node.label for _level, node in root.walk()]
        self.assertIn("点阵图偏鸽", labels)
        self.assertIn("更深的第四层（应被裁掉）", labels, "过深的层被压平而不是丢弃节点")
        self.assertEqual(root.depth, 3, "深度上限必须生效")
        deepest = [node.label for level, node in root.walk() if level == 2]
        self.assertIn("更深的第四层（应被裁掉）", deepest)

    def test_node_limit_is_enforced(self):
        doc = "## 大纲\n" + "\n".join(f"- 节点{i}" for i in range(50))
        root = ol.extract_outline(doc, max_nodes=5, max_depth=3, label_chars=24)
        self.assertLessEqual(root.node_count, 6)

    def test_label_is_truncated(self):
        doc = "## 大纲\n- 主题：主题\n  - " + "很长的要点" * 20
        root = ol.extract_outline(doc, max_depth=3, max_nodes=60, label_chars=10)
        self.assertLessEqual(len(root.children[0].label), 10)

    def test_missing_outline_section_falls_back_to_any_list(self):
        doc = "先来一段说明\n- 甲\n- 乙\n- 丙\n"
        root = ol.extract_outline(doc)
        self.assertIsNotNone(root)
        self.assertEqual([c.label for c in root.children], ["甲", "乙", "丙"])

    def test_no_list_returns_none(self):
        self.assertIsNone(ol.extract_outline("## 摘要\n\n只有段落，没有列表。\n"))

    def test_duplicate_siblings_are_deduped(self):
        doc = "## 大纲\n- 主题：T\n  - 重复\n  - 重复\n  - 另一个\n"
        root = ol.extract_outline(doc)
        self.assertEqual([c.label for c in root.children], ["重复", "另一个"])

    def test_derive_outline_from_paragraphs(self):
        doc = ("## 摘要\n\n美联储点阵图偏鸽，年内降息两次。后面还有一句。\n\n"
               "期限溢价与沃什的相关性值得关注。\n")
        root = ol.derive_outline(doc, title="视频标题")
        self.assertEqual(root.label, "视频标题")
        self.assertEqual(root.source, "derived")
        self.assertEqual(root.children[0].label, "美联储点阵图偏鸽，年内降息两次")
        self.assertEqual(len(root.children), 2)

    def test_derive_outline_from_bullets(self):
        doc = "## 摘要\n- 要点一\n- 要点二\n"
        root = ol.derive_outline(doc, title="T")
        self.assertEqual([c.label for c in root.children], ["要点一", "要点二"])

    def test_clean_label_strips_markdown_and_numbering(self):
        self.assertEqual(ol.clean_label("**加粗**要点"), "加粗要点")
        self.assertEqual(ol.clean_label("  多个   空格  "), "多个 空格")
        self.assertTrue(ol.clean_label("很长" * 30, limit=8).endswith("…"))

    def test_build_tree_with_inconsistent_indent(self):
        root = ol.build_tree([(0, "A"), (4, "A1"), (4, "A2"), (2, "B")],
                             max_depth=3, max_nodes=60, label_chars=24)
        # 缩进宽度去重排序 → 0/2/4 三层；B 比 A 深一层，因此是 A 的子节点
        self.assertEqual([c.label for c in root.children], ["A"])
        self.assertEqual([c.label for c in root.children[0].children], ["A1", "A2", "B"])


class MermaidTest(unittest.TestCase):
    def test_sanitize_replaces_mermaid_specials(self):
        out = mm.sanitize_label('标题(下)[1]{2}: "引号" #标签 %占比 | 管道 <尖> \\斜 `码`')
        for bad in '()[]{}:"#%|<>\\`':
            self.assertNotIn(bad, out, f"{bad!r} 不该留在 Mermaid 标签里")
        self.assertIn("（下）", out)
        self.assertIn("：", out)

    def test_sanitize_collapses_newlines_and_limits(self):
        self.assertEqual(mm.sanitize_label("第一行\n第二行"), "第一行 第二行")
        self.assertEqual(mm.sanitize_label("", limit=8), "未命名")
        self.assertTrue(mm.sanitize_label("很长" * 30, limit=6).endswith("…"))

    def test_to_mermaid_shape_and_determinism(self):
        root = ol.build_tree([(0, "主题：A"), (2, "B"), (4, "C")],
                             max_depth=3, max_nodes=60, label_chars=24)
        first = mm.to_mermaid(root, title="视频标题")
        second = mm.to_mermaid(root, title="视频标题")
        self.assertEqual(first.text, second.text, "同样的树必须得到同样的 .mmd")
        lines = first.text.strip().split("\n")
        self.assertTrue(lines[0].startswith("%%"))
        self.assertIn("mindmap", first.text)
        body = [line for line in lines if not line.startswith("%%")]
        self.assertEqual(body[0], "mindmap")
        self.assertTrue(body[1].strip().startswith("root(("))
        self.assertEqual(body[2], "    " + "B")      # 根在 2 空格，其子节点在 4 空格
        self.assertEqual(body[3], "      " + "C")
        self.assertEqual(first.nodes, 3)
        self.assertEqual(first.depth, 3)

    def test_node_and_depth_limits(self):
        root = ol.build_tree([(0, "主题：A")] + [(2 * (i + 1), f"L{i}") for i in range(20)],
                             max_depth=3, max_nodes=60, label_chars=24)
        result = mm.to_mermaid(root, max_nodes=4, max_depth=2, label_chars=24)
        self.assertLessEqual(result.nodes, 4)
        self.assertGreater(result.dropped, 0)
        self.assertTrue(any("超上限" in note for note in result.notes))


# --------------------------------------------------------------------------- #
# LLM 客户端
# --------------------------------------------------------------------------- #
def ok_transport(payload: dict, *, capture: dict | None = None):
    def transport(url, headers, body, timeout):
        if capture is not None:
            capture["url"] = url
            capture["headers"] = dict(headers)
            capture["body"] = json.loads(body.decode("utf-8"))
            capture["timeout"] = timeout
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")

    return transport


def reply(text: str = "模型输出", **extra) -> dict:
    payload = {"model": "m-1", "choices": [{"message": {"role": "assistant", "content": text},
                                            "finish_reason": extra.pop("finish_reason", "stop")}],
               "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
    payload.update(extra)
    return payload


class ChatClientTest(unittest.TestCase):
    def client(self, transport, **kwargs) -> llm_mod.HttpChatClient:
        kwargs.setdefault("base_url", "https://api.example.com/v1")
        kwargs.setdefault("model", "m-1")
        kwargs.setdefault("api_key", "sk-secret")
        kwargs.setdefault("sleep", lambda _s: None)
        return llm_mod.HttpChatClient(transport=transport, **kwargs)

    def test_endpoint_and_host(self):
        self.assertEqual(llm_mod.chat_endpoint("https://api.example.com/v1"),
                         "https://api.example.com/v1/chat/completions")
        self.assertEqual(llm_mod.chat_endpoint("https://api.example.com/v1/chat/completions"),
                         "https://api.example.com/v1/chat/completions")
        self.assertEqual(llm_mod.chat_endpoint("https://api.example.com/v1/"), 
                         "https://api.example.com/v1/chat/completions")
        self.assertEqual(llm_mod.endpoint_host("https://user:pw@host:8443/v1?token=abc"),
                         "user:pw@host:8443")
        self.assertEqual(llm_mod.chat_endpoint(""), "")

    def test_successful_call_payload_and_usage(self):
        capture: dict = {}
        client = self.client(ok_transport(reply("你好"), capture=capture))
        result = client.complete([{"role": "user", "content": "hi"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "你好")
        self.assertEqual(result.prompt_tokens, 11)
        self.assertEqual(result.completion_tokens, 7)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(capture["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(capture["headers"]["Authorization"], "Bearer sk-secret")
        self.assertEqual(capture["body"]["model"], "m-1")
        self.assertEqual(capture["body"]["messages"][0]["content"], "hi")
        self.assertFalse(capture["body"]["stream"])

    def test_no_api_key_sends_no_authorization(self):
        capture: dict = {}
        client = self.client(ok_transport(reply(), capture=capture), api_key="")
        client.complete([{"role": "user", "content": "hi"}])
        self.assertNotIn("Authorization", capture["headers"])

    def test_content_parts_are_joined(self):
        payload = reply("")
        payload["choices"][0]["message"]["content"] = [{"type": "text", "text": "甲"},
                                                      {"type": "text", "text": "乙"}]
        result = self.client(ok_transport(payload)).complete([{"role": "user", "content": "x"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "甲乙")

    def test_truncated_finish_reason(self):
        result = self.client(ok_transport(reply("截断", finish_reason="length"))).complete(
            [{"role": "user", "content": "x"}])
        self.assertTrue(result.truncated)

    def test_auth_error_is_not_retried(self):
        calls = []

        def transport(url, headers, body, timeout):
            calls.append(url)
            return 401, {}, json.dumps({"error": {"message": "invalid key"}}).encode()

        result = self.client(transport).complete([{"role": "user", "content": "x"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, llm_mod.KIND_LLM_AUTH)
        self.assertEqual(len(calls), 1, "401 不该重试")
        self.assertIn("invalid key", result.message)

    def test_rate_limit_is_retried_then_succeeds(self):
        calls = []
        slept: list[float] = []

        def transport(url, headers, body, timeout):
            calls.append(url)
            if len(calls) == 1:
                return 429, {}, json.dumps({"error": {"message": "slow down"}}).encode()
            return 200, {}, json.dumps(reply("重试成功")).encode()

        client = self.client(transport, retries=2)
        client._sleep = slept.append          # 不真的等
        result = client.complete([{"role": "user", "content": "x"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(client.retries_used, 1)
        self.assertEqual(len(slept), 1)

    def test_server_error_exhausts_retries(self):
        def transport(url, headers, body, timeout):
            return 500, {}, b"boom"

        client = self.client(transport, retries=3)
        client._sleep = lambda _s: None
        result = client.complete([{"role": "user", "content": "x"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, llm_mod.KIND_LLM_HTTP)
        self.assertEqual(result.attempts, 3)

    def test_network_and_timeout_classification(self):
        def network(url, headers, body, timeout):
            raise OSError("connection refused")

        result = self.client(network, retries=1).complete([{"role": "user", "content": "x"}])
        self.assertEqual(result.error_kind, llm_mod.KIND_LLM_NETWORK)

        def slow(url, headers, body, timeout):
            raise TimeoutError("timed out")

        result = self.client(slow, retries=1).complete([{"role": "user", "content": "x"}])
        self.assertEqual(result.error_kind, llm_mod.KIND_LLM_TIMEOUT)

    def test_invalid_json_and_empty_content(self):
        def bad_json(url, headers, body, timeout):
            return 200, {}, b"<html>not json</html>"

        result = self.client(bad_json).complete([{"role": "user", "content": "x"}])
        self.assertEqual(result.error_kind, llm_mod.KIND_LLM_INVALID)

        result = self.client(ok_transport(reply("   "))).complete(
            [{"role": "user", "content": "x"}])
        self.assertEqual(result.error_kind, llm_mod.KIND_LLM_INVALID)
        self.assertIn("空", result.message)

    def test_empty_content_hint_for_reasoning_model(self):
        """推理模型把预算花在思考上时，报错要给出可操作的下一步（实测 deepseek-flash）。"""
        truncated = reply("", finish_reason="length")
        truncated["choices"][0]["message"]["reasoning_content"] = "先想一想……"
        result = self.client(ok_transport(truncated)).complete([{"role": "user", "content": "x"}])
        self.assertEqual(result.error_kind, llm_mod.KIND_LLM_INVALID)
        self.assertIn("max_tokens", result.message)
        self.assertIn("reasoning_content", result.message)

        only_reasoning = reply("", finish_reason="stop")
        only_reasoning["choices"][0]["message"]["reasoning_content"] = "思考过程"
        result = self.client(ok_transport(only_reasoning)).complete([{"role": "user", "content": "x"}])
        self.assertIn("reasoning_content", result.message)

    def test_missing_configuration_short_circuits(self):
        client = self.client(ok_transport(reply()), base_url="")
        self.assertEqual(client.complete([{"role": "user", "content": "x"}]).error_kind,
                         llm_mod.KIND_LLM_INVALID)
        client = self.client(ok_transport(reply()), model="")
        self.assertEqual(client.complete([{"role": "user", "content": "x"}]).error_kind,
                         llm_mod.KIND_LLM_INVALID)

    def test_retryable_kinds_are_documented(self):
        self.assertIn(llm_mod.KIND_LLM_RATE_LIMIT, llm_mod.RETRYABLE_LLM_KINDS)
        self.assertNotIn(llm_mod.KIND_LLM_AUTH, llm_mod.RETRYABLE_LLM_KINDS)


class BuildChatClientTest(unittest.TestCase):
    def test_injected_transport_uses_stdlib_client(self):
        client = llm_mod.build_chat_client(
            Config(uid=1, summary_base_url="https://api.example.com/v1", summary_model="m"),
            "k", transport=ok_transport(reply()))
        self.assertEqual(client.name, "urllib")
        self.assertEqual(client.endpoint_host, "api.example.com")

    def test_http_mode_ignores_sdk(self):
        client = llm_mod.build_chat_client(
            Config(uid=1, summary_base_url="https://api.example.com/v1", summary_model="m",
                   summary_client="http"), "k")
        self.assertEqual(client.name, "urllib")

    def test_sdk_mode_requires_openai(self):
        config = Config(uid=1, summary_base_url="https://api.example.com/v1",
                        summary_model="m", summary_client="sdk")
        if llm_mod.module_available("openai"):
            self.skipTest("本机装了 openai，缺失路径不适用")
        with self.assertRaises(llm_mod.LlmUnavailable):
            llm_mod.build_chat_client(config, "k")

    def test_sdk_client_with_stubbed_module(self):
        """用替身模块覆盖 SDK 路径（本机没装 openai 也能验证映射逻辑）。"""
        calls: dict = {}

        class _Message:
            def __init__(self, content):
                self.content = content

        class _Choice:
            finish_reason = "stop"

            def __init__(self):
                self.message = _Message("SDK 输出")

        class _Usage:
            prompt_tokens = 3
            completion_tokens = 4

        class _Resp:
            model = "m-1"
            choices = [_Choice()]
            usage = _Usage()

        class _Completions:
            def create(self, **kwargs):
                calls["kwargs"] = kwargs
                return _Resp()

        class _Chat:
            completions = _Completions()

        class _OpenAI:
            def __init__(self, **kwargs):
                calls["client"] = kwargs
                self.chat = _Chat()

        fake = mock.MagicMock()
        fake.OpenAI = _OpenAI
        with mock.patch.dict(sys.modules, {"openai": fake}), \
                mock.patch.object(llm_mod, "module_available", return_value=True):
            client = llm_mod.build_chat_client(
                Config(uid=1, summary_base_url="https://api.example.com/v1",
                       summary_model="m", summary_client="sdk"), "k")
            self.assertEqual(client.name, "openai-sdk")
            result = client.complete([{"role": "user", "content": "hi"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "SDK 输出")
        self.assertEqual(result.prompt_tokens, 3)
        self.assertEqual(calls["kwargs"]["model"], "m")
        self.assertEqual(calls["client"]["base_url"], "https://api.example.com/v1")

    def test_sdk_exception_classification(self):
        class _Boom(Exception):
            status_code = 401

        self.assertEqual(llm_mod._classify_sdk_exception(_Boom()), llm_mod.KIND_LLM_AUTH)

        class _Timeout(Exception):
            pass

        _Timeout.__name__ = "APITimeoutError"
        self.assertEqual(llm_mod._classify_sdk_exception(_Timeout()), llm_mod.KIND_LLM_TIMEOUT)


# --------------------------------------------------------------------------- #
# mmdc 渲染
# --------------------------------------------------------------------------- #
class MermaidCliTest(unittest.TestCase):
    def setUp(self):
        self._ws = workspace()
        self.root = self._ws.__enter__()
        self.mmd = self.root / MINDMAP_MMD
        self.mmd.write_text("mindmap\n  root((标题))\n    要点\n", encoding="utf-8")
        self.png = self.root / MINDMAP_PNG

    def tearDown(self):
        self._ws.__exit__(None, None, None)

    def test_png_size_reads_ihdr(self):
        path = self.root / "ok.png"
        path.write_bytes(fx.fake_png(1200, 800))
        self.assertEqual(mc.png_size(path), (1200, 800))
        bad = self.root / "bad.png"
        bad.write_bytes(b"<html>error</html>")
        self.assertEqual(mc.png_size(bad), (0, 0))
        self.assertEqual(mc.png_size(self.root / "missing.png"), (0, 0))

    def test_missing_mmdc(self):
        outcome = mc.render_mindmap(mmd_path=self.mmd, png_path=self.png, mmdc="")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "dependency_missing")
        self.assertIn("mermaid-cli", outcome.message)

    def test_missing_source(self):
        outcome = mc.render_mindmap(mmd_path=self.root / "nope.mmd", png_path=self.png,
                                    mmdc="mmdc")
        self.assertEqual(outcome.error_kind, "missing_source")

    def test_success_writes_png_atomically(self):
        created: list = []
        outcome = mc.render_mindmap(mmd_path=self.mmd, png_path=self.png, mmdc="mmdc",
                                    width=1024, runner=fx.fake_mmdc_runner(created, width=1024,
                                                                          height=600))
        self.assertTrue(outcome.ok)
        self.assertEqual((outcome.width, outcome.height), (1024, 600))
        self.assertGreater(outcome.bytes_written, mc.MIN_PNG_BYTES)
        self.assertTrue(self.png.is_file())
        self.assertFalse((self.root / "mindmap.tmp.png").exists(), "临时文件必须被清掉")
        self.assertEqual(created[0][0], "mmdc")
        self.assertIn("-w", created[0])
        self.assertIn("1024", created[0])

    def test_build_argv_includes_background_and_puppeteer(self):
        argv = mc.build_argv("mmdc", Path("a.mmd"), Path("b.png"), width=800,
                             background="transparent", puppeteer_config="p.json")
        self.assertEqual(argv[:2], ["mmdc", "-i"])
        self.assertIn("transparent", argv)
        self.assertIn("p.json", argv)

    def test_nonzero_exit_keeps_no_png(self):
        outcome = mc.render_mindmap(mmd_path=self.mmd, png_path=self.png, mmdc="mmdc",
                                    runner=fx.fake_mmdc_runner(ok=False, stderr="语法错误"))
        self.assertEqual(outcome.error_kind, "render_failed")
        self.assertIn("语法错误", outcome.message)
        self.assertFalse(self.png.exists())
        self.assertFalse((self.root / "mindmap.tmp.png").exists())

    def test_reported_success_without_file(self):
        outcome = mc.render_mindmap(mmd_path=self.mmd, png_path=self.png, mmdc="mmdc",
                                    runner=fx.fake_mmdc_runner(write_png=False))
        self.assertEqual(outcome.error_kind, "render_invalid")
        self.assertIn("没有产出", outcome.message)

    def test_tiny_output_is_rejected(self):
        outcome = mc.render_mindmap(mmd_path=self.mmd, png_path=self.png, mmdc="mmdc",
                                    runner=fx.fake_mmdc_runner(padding=0))
        self.assertEqual(outcome.error_kind, "render_invalid")
        self.assertFalse(self.png.exists())

    def test_timeout_and_missing_executable(self):
        def timeout_runner(argv):
            raise subprocess.TimeoutExpired(argv, 1)

        outcome = mc.render_mindmap(mmd_path=self.mmd, png_path=self.png, mmdc="mmdc",
                                    runner=timeout_runner)
        self.assertEqual(outcome.error_kind, "timeout")

        def missing_runner(argv):
            raise FileNotFoundError("mmdc")

        outcome = mc.render_mindmap(mmd_path=self.mmd, png_path=self.png, mmdc="mmdc",
                                    runner=missing_runner)
        self.assertEqual(outcome.error_kind, "dependency_missing")

    def test_unexpected_exception_is_captured(self):
        def boom(argv):
            raise RuntimeError("注入的崩溃")

        outcome = mc.render_mindmap(mmd_path=self.mmd, png_path=self.png, mmdc="mmdc",
                                    runner=boom)
        self.assertEqual(outcome.error_kind, "render_failed")
        self.assertIn("注入的崩溃", outcome.message)

    def test_probe_uses_explicit_path(self):
        fake = self.root / "mmdc.cmd"
        fake.write_bytes(b"fake")
        config = Config(uid=1, mindmap_mmdc_path=str(fake))
        self.assertEqual(mc.probe_mmdc(config), str(fake))
        self.assertEqual(mc.probe_mmdc(Config(uid=1, mindmap_mmdc_path=str(self.root / "nope"))),
                         mc.probe_mmdc(None))

    def test_build_argv_passes_style_files(self):
        argv = mc.build_argv("mmdc", Path("a.mmd"), Path("b.png"), config_file="c.json",
                             css_file="s.css")
        self.assertEqual(argv[argv.index("-c") + 1], "c.json")
        self.assertEqual(argv[argv.index("--cssFile") + 1], "s.css")

    def test_build_argv_without_style_stays_clean(self):
        """classic（无样式）时命令行必须与改造前逐字一致。"""
        argv = mc.build_argv("mmdc", Path("a.mmd"), Path("b.png"), width=800,
                             background="white", puppeteer_config="p.json")
        self.assertEqual(argv, ["mmdc", "-i", "a.mmd", "-o", "b.png", "-w", "800",
                                "-b", "white", "-p", "p.json"])
        self.assertNotIn("-c", argv)
        self.assertNotIn("--cssFile", argv)


# --------------------------------------------------------------------------- #
# 导图样式预设
# --------------------------------------------------------------------------- #
class MindmapStyleTest(unittest.TestCase):
    def setUp(self):
        self._ws = workspace()
        self.root = self._ws.__enter__()

    def tearDown(self):
        self._ws.__exit__(None, None, None)

    def test_default_is_paper_and_known_set(self):
        self.assertEqual(mst.DEFAULT_STYLE, "paper")
        self.assertEqual(mst.STYLE_NAMES, ("paper", "pastel", "dark", "classic"))
        for name in mst.STYLE_NAMES:
            self.assertTrue(mst.is_known_style(name))
            self.assertTrue(mst.STYLE_LABELS[name])

    def test_unknown_style_normalizes_to_default(self):
        self.assertEqual(mst.normalize_style(""), "paper")
        self.assertEqual(mst.normalize_style(None), "paper")
        self.assertEqual(mst.normalize_style("  DARK "), "dark")
        self.assertEqual(mst.normalize_style("cute"), "paper")
        self.assertFalse(mst.is_known_style("cute"))
        self.assertIn("paper", mst.style_choices_text())

    def test_classic_adds_nothing(self):
        bundle = mst.build_style("classic")
        self.assertFalse(bundle.styled)
        self.assertEqual(bundle.config, {})
        self.assertEqual(bundle.css, "")
        self.assertEqual(bundle.background, "")
        with mst.staged_style_files(bundle) as (config_file, css_file):
            self.assertEqual((config_file, css_file), ("", ""))

    def test_paper_bundle_has_cjk_font_and_no_max_width(self):
        bundle = mst.build_style("paper")
        variables = bundle.config["themeVariables"]
        self.assertEqual(bundle.config["look"], "neo")
        self.assertEqual(bundle.config["theme"], "base")
        self.assertIn("Microsoft YaHei", variables["fontFamily"])
        self.assertEqual(variables["fontSize"], "17px")
        self.assertFalse(bundle.config["mindmap"]["useMaxWidth"])
        self.assertEqual(variables["cScale0"], mst.BRANCHES[0]["stroke"])
        # 12 支调色板：覆盖 cScale0..11，超过 12 个一级分支才会掉回默认配色
        self.assertEqual(len(mst.BRANCHES), 12)
        self.assertEqual(variables["cScale11"], mst.BRANCHES[11]["stroke"])
        self.assertIn("section-11", mst.build_style("paper").css)

    def test_paper_css_paints_white_cards_with_branch_borders(self):
        css = mst.build_style("paper").css
        self.assertIn("#ffffff", css)                       # 白底卡片
        self.assertIn("fill: #ffffff !important", css)
        self.assertIn(mst.BRANCHES[0]["stroke"], css)       # 分支色描边
        self.assertIn("stroke: #4c7df0 !important", css)
        self.assertIn("section-edge-0", css)
        self.assertIn("section-root", css)
        self.assertIn("!important", css, "mermaid 生成的规则在 SVG 内部，覆盖必须带权重")

    def test_pastel_fills_nodes_with_palette_colors(self):
        css = mst.build_style("pastel").css
        self.assertIn("fill: #e3ecfe !important", css)          # 节点填充 = 调色板柔和色
        self.assertIn(mst.BRANCHES[0]["fill"], css)
        # 通用节点块只调描边宽度，不做"全局白底"覆盖（那是 paper 的做法）
        self.assertIn("#my-svg .mindmap-node path,\n#my-svg .mindmap-node rect,\n"
                      "#my-svg .mindmap-node circle,\n#my-svg .mindmap-node polygon {\n"
                      "  stroke-width: 1.6px !important;\n}", css)

    def test_dark_style_uses_dark_background_and_light_text(self):
        bundle = mst.build_style("dark")
        self.assertEqual(bundle.background, mst.DARK_BACKGROUND)
        self.assertEqual(bundle.config["themeVariables"]["background"], mst.DARK_BACKGROUND)
        self.assertIn(mst.BRANCHES[0]["stroke_dark"], bundle.css)
        self.assertIn("#e2e8f0", bundle.css)

    def test_font_size_and_family_override_preset(self):
        bundle = mst.build_style("paper", font_size=22, font_family="思源黑体")
        self.assertEqual(bundle.config["themeVariables"]["fontSize"], "22px")
        self.assertEqual(bundle.config["themeVariables"]["fontFamily"], "思源黑体")
        # 非法字号退回预设默认，不抛异常
        self.assertEqual(mst.build_style("paper", font_size=-3).config["themeVariables"]["fontSize"],
                         f"{mst.DEFAULT_FONT_SIZE}px")
        self.assertEqual(mst.build_style("paper", font_size="x").config["themeVariables"]["fontSize"],
                         f"{mst.DEFAULT_FONT_SIZE}px")

    def test_resolve_background_prefers_explicit_value(self):
        dark = mst.build_style("dark")
        self.assertEqual(mst.resolve_background(dark, "white"), mst.DARK_BACKGROUND)
        self.assertEqual(mst.resolve_background(dark, ""), mst.DARK_BACKGROUND)
        self.assertEqual(mst.resolve_background(dark, "transparent"), "transparent")
        self.assertEqual(mst.resolve_background(dark, "#123456"), "#123456")
        paper = mst.build_style("paper")
        self.assertEqual(mst.resolve_background(paper, "white"), "white")
        self.assertEqual(mst.resolve_background(paper, ""), "white")

    def test_staged_files_are_written_then_cleaned(self):
        bundle = mst.build_style("paper")
        with mst.staged_style_files(bundle) as (config_file, css_file):
            config_path, css_path = Path(config_file), Path(css_file)
            self.assertTrue(config_path.is_file())
            self.assertTrue(css_path.is_file())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["theme"], "base")
            self.assertIn("#my-svg", css_path.read_text(encoding="utf-8"))
        self.assertFalse(config_path.exists(), "临时样式文件必须随渲染一起清掉")
        self.assertFalse(css_path.exists())

    def test_explicit_files_replace_generated_ones(self):
        mine_config = self.root / "mine.json"
        mine_css = self.root / "mine.css"
        with mst.staged_style_files(mst.build_style("paper"), config_file=str(mine_config),
                                    css_file=str(mine_css)) as (config_file, css_file):
            self.assertEqual((config_file, css_file), (str(mine_config), str(mine_css)))
            self.assertFalse(mine_config.exists(), "显式路径指向的文件由用户维护，不该被代写")

    def test_config_json_is_deterministic(self):
        first = mst.config_json(mst.build_style("paper"))
        second = mst.config_json(mst.build_style("paper"))
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["mindmap"]["padding"], 16)


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #
class PromptTest(unittest.TestCase):
    def test_defaults_have_all_sections_and_stable_fingerprint(self):
        spec = pm.load_prompt_spec()
        self.assertEqual(spec.source, "builtin")
        self.assertEqual(spec.fingerprint, pm.load_prompt_spec().fingerprint)
        self.assertIn("{transcript}", spec.full)
        self.assertIn("{points}", spec.reduce)

    def test_sections_are_parsed_and_unknown_rejected(self):
        parsed = pm.parse_prompt_text("[full]\n甲\n[reduce]\n乙\n")
        self.assertEqual(parsed, {"full": "甲", "reduce": "乙"})
        self.assertEqual(pm.parse_prompt_text("整篇都是 full"), {"full": "整篇都是 full"})
        with workspace() as root:
            path = root / "p.txt"
            path.write_text("[unknown]\n内容\n", encoding="utf-8")
            with self.assertRaises(pm.PromptError) as ctx:
                pm.load_prompt_spec(path)
            self.assertIn("未知分节", str(ctx.exception))

    def test_file_without_placeholders_is_rejected(self):
        with workspace() as root:
            path = root / "p.txt"
            path.write_text("只改一句话，没有任何占位符", encoding="utf-8")
            with self.assertRaises(pm.PromptError):
                pm.load_prompt_spec(path)

    def test_missing_file_is_rejected(self):
        with workspace() as root:
            with self.assertRaises(pm.PromptError):
                pm.load_prompt_spec(root / "nope.txt")

    def test_custom_prompt_overrides_only_given_sections(self):
        with workspace() as root:
            path = root / "p.txt"
            path.write_text("[map]\n分段要点：{transcript}\n", encoding="utf-8")
            spec = pm.load_prompt_spec(path)
            self.assertIn("分段要点", spec.map)
            self.assertEqual(spec.reduce, pm.DEFAULT_REDUCE, "未覆盖的分节用内置默认")
            self.assertEqual(spec.overridden, ("map",))
            self.assertIn("file:p.txt", spec.describe())

    def test_render_substitutes_placeholders(self):
        spec = pm.load_prompt_spec()
        messages = spec.render_full(transcript="正文甲", chars=3, pages="P01",
                                    title="标题", author="作者")
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("正文甲", messages[-1]["content"])
        self.assertNotIn("{transcript}", messages[-1]["content"])
        self.assertNotIn("{title}", messages[-1]["content"])

    def test_custom_prompt_with_braces_survives(self):
        """自定义 prompt 里写 JSON 示例（花括号）不该被当格式化语法报错。"""
        with workspace() as root:
            path = root / "p.txt"
            path.write_text("[full]\n{transcript}\n请输出 {{\"key\": \"value\"}}\n",
                            encoding="utf-8")
            spec = pm.load_prompt_spec(path)
            rendered = spec.render_full(transcript="甲", chars=1, pages="P01", title="T")[-1]
            self.assertIn('{"key": "value"}', rendered["content"])

    def test_fingerprint_changes_with_content(self):
        with workspace() as root:
            path = root / "p.txt"
            path.write_text("[full]\n{transcript}\n版本一\n", encoding="utf-8")
            first = pm.load_prompt_spec(path).fingerprint
            path.write_text("[full]\n{transcript}\n版本二\n", encoding="utf-8")
            self.assertNotEqual(first, pm.load_prompt_spec(path).fingerprint)

    def test_prompt_placeholders_report(self):
        report = pm.prompt_placeholders(pm.load_prompt_spec())
        self.assertTrue(report["full"])
        self.assertTrue(report["map"])

    def test_toml_prompt_file_is_parsed_as_toml(self):
        """``prompts/summary.toml`` 这种命名下允许写真 TOML（多行字符串）。"""
        with workspace() as root:
            path = root / "summary.toml"
            path.write_text(
                'system = """你是财经分析师"""\n'
                'map = """第 {index} 段：{transcript}"""\n'
                'full = """全文：{transcript}（{chars} 字，{title}）"""\n',
                encoding="utf-8")
            spec = pm.load_prompt_spec(path)
            self.assertEqual(spec.overridden, ("full", "map", "system"))
            self.assertIn("财经分析师", spec.system)
            self.assertEqual(spec.reduce, pm.DEFAULT_REDUCE, "未覆盖的分节用内置默认")
            rendered = spec.render_full(transcript="正文甲", chars=3, pages="P01", title="标题")
            self.assertIn("全文：正文甲（3 字，标题）", rendered[-1]["content"])

    def test_toml_prompt_missing_placeholder_is_rejected(self):
        with workspace() as root:
            path = root / "summary.toml"
            path.write_text('full = """没有占位符"""\n', encoding="utf-8")
            with self.assertRaises(pm.PromptError):
                pm.load_prompt_spec(path)

    def test_unknown_toml_key_falls_back_to_ini_semantics(self):
        """不认识顶层键时不假装是 prompt TOML：退回"整篇当 full"，由占位符校验兜住。"""
        text = 'foo = "bar"\n'
        self.assertEqual(pm.parse_prompt_text(text), {"full": text})
        with workspace() as root:
            path = root / "summary.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(pm.PromptError):
                pm.load_prompt_spec(path)

    def test_ini_style_file_with_toml_extension_still_works(self):
        with workspace() as root:
            path = root / "summary.toml"
            path.write_text("[map]\n分段要点：{transcript}\n", encoding="utf-8")
            spec = pm.load_prompt_spec(path)
            self.assertEqual(spec.overridden, ("map",))
            self.assertIn("分段要点", spec.map)


# --------------------------------------------------------------------------- #
# 编排：build_summary / build_mindmap
# --------------------------------------------------------------------------- #
class BuildSummaryTest(unittest.TestCase):
    def setUp(self):
        self._ws = workspace()
        self.root = self._ws.__enter__()
        self.config = Config(uid=1, summary_base_url="https://api.example.com/v1",
                             summary_model="fake-model-1")

    def tearDown(self):
        self._ws.__exit__(None, None, None)

    def build(self, text: str | None = None, **kwargs) -> SummaryPlan:
        return build_summary(
            transcript_text=transcript_text() if text is None else text,
            config=kwargs.pop("config", self.config), entry_dir=self.root,
            chat_client=kwargs.pop("chat_client", fx.FakeChatClient()),
            title="视频标题", author="作者", **kwargs,
        )

    def test_success_writes_summary_and_records_metadata(self):
        plan = self.build()
        self.assertEqual(plan.status, "done")
        self.assertEqual(plan.summary_md, SUMMARY_MD)
        self.assertEqual(plan.model, "fake-model-1")
        self.assertEqual(plan.client, "fake-chat")
        self.assertEqual(plan.endpoint_host, "fake.local")
        self.assertEqual(plan.prompt_source, "builtin")
        self.assertTrue(plan.prompt_fingerprint)
        self.assertTrue(plan.signature)
        self.assertTrue(plan.generated_at)
        raw = (self.root / SUMMARY_MD).read_text(encoding="utf-8")
        self.assertIn(SUMMARY_BEGIN, raw)
        self.assertIn(SUMMARY_END, raw)
        self.assertIn("fake-model-1", raw)
        self.assertIn("## 摘要", raw)
        self.assertEqual(read_summary_body(self.root / SUMMARY_MD).splitlines()[0], "## 摘要")

    def test_plan_json_round_trip(self):
        plan = self.build()
        again = SummaryPlan.from_json(plan.to_json())
        self.assertEqual(again.signature, plan.signature)
        self.assertEqual(again.model, plan.model)
        self.assertEqual(again.chunks, plan.chunks)
        self.assertEqual(again.status, "done")
        self.assertNotIn("text", plan.to_json(), "正文不写进 metadata（体积）")

    def test_not_configured_is_skipped_not_failed(self):
        config = Config(uid=1)
        plan = self.build(config=config)
        self.assertEqual(plan.status, "skipped")
        self.assertEqual(plan.reason, "llm_not_configured")
        self.assertFalse((self.root / SUMMARY_MD).exists())

    def test_disabled_is_skipped(self):
        plan = self.build(config=Config(uid=1, summary_enabled=False,
                                        summary_base_url="https://x/v1", summary_model="m"))
        self.assertEqual(plan.reason, "summary_disabled")

    def test_no_text_is_skipped(self):
        plan = self.build(text="")
        self.assertEqual(plan.reason, "no_transcript")

    def test_insufficient_text_is_skipped(self):
        plan = self.build(text="## P01 P1\n\n短\n")
        self.assertEqual(plan.reason, "insufficient_text")
        self.assertIn("min_chars", plan.message)

    def test_header_only_transcript_is_not_enough(self):
        """头部统计行（以及第一个 ``## Pxx`` 之前的一切）不算内容，不能靠它凑够 min_chars。"""
        header_only = ("# 合并文字稿\n\n- 时间基准：各 P 内部计时\n- 分 P 数：1\n- "
                       + "注" * 400 + "\n\n## P01 P1\n\n> 来源：平台字幕\n\n短\n")
        plan = self.build(text=header_only)
        self.assertEqual(plan.reason, "insufficient_text")

    def test_model_failure_is_recorded(self):
        plan = self.build(chat_client=fx.FakeChatClient(fail=True))
        self.assertEqual(plan.status, "failed")
        self.assertEqual(plan.reason, "llm_failed")
        self.assertEqual(plan.error_kind, "llm_http_error")
        self.assertFalse((self.root / SUMMARY_MD).exists())

    def test_empty_model_output_is_failure(self):
        plan = self.build(chat_client=fx.FakeChatClient(empty=True))
        self.assertEqual(plan.status, "failed")
        self.assertEqual(plan.error_kind, "llm_response_invalid")

    def test_truncated_output_adds_note(self):
        plan = self.build(chat_client=fx.FakeChatClient(truncate=True))
        self.assertEqual(plan.truncated_calls, 1)
        self.assertTrue(any("截断" in note for note in plan.notes))

    def test_single_chunk_uses_full_prompt(self):
        chat = fx.FakeChatClient()
        plan = self.build(chat_client=chat)
        self.assertEqual(plan.chunks, 1)
        self.assertEqual(len(chat.calls), 1)
        self.assertIn("完整文字稿", chat.prompts()[0])

    def test_multi_chunk_uses_map_then_reduce(self):
        text = transcript_text(pages=2, rows_per_page=30)
        chat = fx.FakeChatClient()
        plan = self.build(text=text, config=Config(
            uid=1, summary_base_url="https://api.example.com/v1", summary_model="m",
            summary_chunk_chars=500), chat_client=chat)
        self.assertEqual(plan.status, "done")
        self.assertGreater(plan.chunks, 2)
        self.assertEqual(len(chat.calls), plan.chunks + 1)
        self.assertIn("分段要点", chat.prompts()[-1])
        self.assertEqual(plan.prompt_tokens > 0, True)

    def test_scaled_chunks_are_noted(self):
        text = transcript_text(pages=1, rows_per_page=120)
        plan = self.build(text=text, config=Config(
            uid=1, summary_base_url="https://api.example.com/v1", summary_model="m",
            summary_chunk_chars=500, summary_max_chunks=3), chat_client=fx.FakeChatClient())
        self.assertEqual(plan.status, "done")
        self.assertTrue(plan.chunk_plan["scaled"])
        self.assertTrue(any("不丢尾段" in note for note in plan.notes))

    def test_reduce_groups_split_long_points(self):
        groups = _group_points("### a\n" + "甲" * 100 + "\n\n### b\n" + "乙" * 100, 120)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group.strip() for group in groups))

    def test_signature_reacts_to_config_and_prompt(self):
        spec = pm.load_prompt_spec()
        text = transcript_text()
        base = summary_signature(self.config, spec, text)
        self.assertEqual(base, summary_signature(self.config, spec, text))
        other_model = Config(uid=1, summary_base_url="https://api.example.com/v1",
                             summary_model="another")
        self.assertNotEqual(base, summary_signature(other_model, spec, text))
        self.assertNotEqual(base, summary_signature(self.config, spec, text + "尾巴"))
        with workspace() as root:
            path = root / "p.txt"
            path.write_text("[full]\n{transcript}\n不同 prompt\n", encoding="utf-8")
            self.assertNotEqual(base, summary_signature(self.config,
                                                        pm.load_prompt_spec(path), text))
        self.assertNotEqual(mindmap_signature(self.config, base),
                            mindmap_signature(Config(uid=1, mindmap_max_nodes=12), base))

    def test_transcript_digest(self):
        chars, sha1 = transcript_digest("abc")
        self.assertEqual(chars, 3)
        self.assertEqual(len(sha1), 12)

    def test_read_summary_body_without_markers(self):
        path = self.root / "plain.md"
        path.write_text("# 标题\n\n- 元信息\n\n## 摘要\n\n正文\n", encoding="utf-8")
        self.assertEqual(read_summary_body(path), "## 摘要\n\n正文")
        self.assertEqual(read_summary_body(self.root / "missing.md"), "")

        # 改名兼容（SubVideo → bili-sub-archive）：旧标记的产物仍能准确切出正文
        legacy = self.root / "legacy.md"
        legacy.write_text(
            "# 标题\n\n- 元信息\n\n<!-- subvideo:summary:begin -->\n"
            "## 摘要\n\n旧产物正文\n<!-- subvideo:summary:end -->\n",
            encoding="utf-8")
        self.assertEqual(read_summary_body(legacy), "## 摘要\n\n旧产物正文")


class BuildMindmapTest(unittest.TestCase):
    def setUp(self):
        self._ws = workspace()
        self.root = self._ws.__enter__()
        fake_mmdc = self.root / "mmdc.cmd"
        fake_mmdc.write_bytes(b"fake")
        self.config = Config(uid=1, mindmap_mmdc_path=str(fake_mmdc))

    def tearDown(self):
        self._ws.__exit__(None, None, None)

    def plan(self, body: str, status: str = "done") -> SummaryPlan:
        return SummaryPlan(status=status, text=body, signature="sig123",
                           model="m", reason="llm_failed" if status == "failed" else "")

    def test_builds_mmd_and_png(self):
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n    - B\n")
        created: list = []
        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner(created))
        self.assertEqual(plan.mindmap_status, "done")
        self.assertEqual(plan.outline_source, "llm")
        self.assertEqual(plan.mindmap_nodes, 3)
        self.assertEqual((plan.mindmap_width, plan.mindmap_height), (1600, 900))
        self.assertTrue((self.root / MINDMAP_MMD).is_file())
        self.assertTrue((self.root / MINDMAP_PNG).is_file())
        self.assertTrue(plan.mindmap_signature)
        self.assertIn("mindmap", (self.root / MINDMAP_MMD).read_text(encoding="utf-8"))
        self.assertEqual(created[0][0], str(self.config.mindmap_mmdc_path))

    def test_style_files_are_passed_to_mmdc_and_cleaned(self):
        """默认（paper）要把生成的配置 JSON 与 CSS 交给 mmdc，渲染完不留临时文件。"""
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        seen: dict = {}
        inner = fx.fake_mmdc_runner()

        def runner(argv):
            seen["argv"] = list(argv)
            config_path = Path(argv[argv.index("-c") + 1])
            css_path = Path(argv[argv.index("--cssFile") + 1])
            seen["config"] = json.loads(config_path.read_text(encoding="utf-8"))
            seen["css"] = css_path.read_text(encoding="utf-8")
            seen["config_path"] = config_path
            seen["css_path"] = css_path
            seen["font"] = seen["config"]["themeVariables"]["fontFamily"]
            return inner(argv)

        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频",
                      mmdc_runner=runner)
        self.assertEqual(plan.mindmap_status, "done")
        self.assertEqual(plan.mindmap_style, "paper")
        self.assertEqual(seen["config"]["mindmap"]["useMaxWidth"], False)
        self.assertIn("Microsoft YaHei", seen["font"])
        self.assertIn("section-0", seen["css"])
        self.assertFalse(seen["config_path"].exists(), "临时配置必须清掉")
        self.assertFalse(seen["css_path"].exists(), "临时 CSS 必须清掉")
        # 产物目录里只该有 .mmd/.png，样式文件不落进条目
        self.assertEqual(sorted(p.name for p in self.root.iterdir()
                                if p.suffix in {".json", ".css"}), [])

    def test_classic_style_passes_no_style_files(self):
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        created: list = []
        config = Config(uid=1, mindmap_mmdc_path=self.config.mindmap_mmdc_path,
                        mindmap_style="classic")
        build_mindmap(plan=plan, config=config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner(created))
        self.assertEqual(plan.mindmap_style, "classic")
        self.assertNotIn("-c", created[0])
        self.assertNotIn("--cssFile", created[0])
        self.assertNotIn("style=", (self.root / MINDMAP_MMD).read_text(encoding="utf-8"))

    def test_dark_style_switches_background_unless_user_set_it(self):
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        created: list = []
        dark = Config(uid=1, mindmap_mmdc_path=self.config.mindmap_mmdc_path,
                      mindmap_style="dark")
        build_mindmap(plan=plan, config=dark, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner(created))
        argv = created[0]
        self.assertEqual(argv[argv.index("-b") + 1], mst.DARK_BACKGROUND)

        plan2 = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        explicit = Config(uid=1, mindmap_mmdc_path=self.config.mindmap_mmdc_path,
                          mindmap_style="dark", mindmap_background="#ffffff")
        build_mindmap(plan=plan2, config=explicit, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner(created))
        argv = created[-1]
        self.assertEqual(argv[argv.index("-b") + 1], "#ffffff", "显式背景必须优先")

    def test_custom_style_files_are_used_as_given(self):
        mine_config = self.root / "mine.json"
        mine_css = self.root / "mine.css"
        mine_config.write_text('{"theme": "forest"}', encoding="utf-8")
        mine_css.write_text("/* mine */", encoding="utf-8")
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        created: list = []
        config = Config(uid=1, mindmap_mmdc_path=self.config.mindmap_mmdc_path,
                        mindmap_config_file=str(mine_config), mindmap_css_file=str(mine_css))
        build_mindmap(plan=plan, config=config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner(created))
        argv = created[0]
        self.assertEqual(argv[argv.index("-c") + 1], str(mine_config))
        self.assertEqual(argv[argv.index("--cssFile") + 1], str(mine_css))
        self.assertTrue(mine_config.is_file(), "用户文件不该被清理")

    def test_metadata_records_style(self):
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner())
        payload = plan.to_json()["mindmap"]
        self.assertEqual(payload["style"], "paper")
        self.assertIn("样式 浅色卡片", plan.mindmap_line())
        self.assertEqual(SummaryPlan.from_json({"mindmap": payload}).mindmap_style, "paper")

    def test_unknown_style_renders_default_and_notes_it(self):
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        created: list = []
        config = Config(uid=1, mindmap_mmdc_path=self.config.mindmap_mmdc_path,
                        mindmap_style="cute")
        build_mindmap(plan=plan, config=config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner(created))
        self.assertEqual(plan.mindmap_style, "paper")
        self.assertTrue(any("cute" in note for note in plan.notes), plan.notes)
        self.assertIn("-c", created[0])

    def test_style_does_not_leak_into_mmd_source(self):
        """同一棵大纲在不同样式下必须产出逐字节相同的 .mmd（样式只属于渲染）。"""
        body = "## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n    - B\n"
        first_dir = self.root / "first"
        second_dir = self.root / "second"
        for directory, style in ((first_dir, "paper"), (second_dir, "classic")):
            build_mindmap(plan=self.plan(body),
                          config=Config(uid=1, mindmap_style=style),
                          entry_dir=directory, title="视频",
                          mmdc_runner=fx.fake_mmdc_runner())
        self.assertEqual((first_dir / MINDMAP_MMD).read_bytes(),
                         (second_dir / MINDMAP_MMD).read_bytes())

    def test_style_is_part_of_signature(self):
        base = mindmap_signature(self.config, "sig123")
        for changed in (Config(uid=1, mindmap_style="dark"),
                        Config(uid=1, mindmap_font_size=22),
                        Config(uid=1, mindmap_font_family="思源黑体"),
                        Config(uid=1, mindmap_config_file="a.json"),
                        Config(uid=1, mindmap_css_file="a.css"),
                        Config(uid=1, mindmap_background="transparent")):
            self.assertNotEqual(base, mindmap_signature(changed, "sig123"))

    def test_derived_outline_when_model_ignores_format(self):
        plan = self.plan("## 摘要\n\n第一句结论。后面还有。\n\n第二句结论。\n")
        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner())
        self.assertEqual(plan.outline_source, "derived")
        self.assertTrue(any("派生" in note for note in plan.notes))
        self.assertEqual(plan.mindmap_status, "done")

    def test_disabled_switch(self):
        plan = self.plan("## 摘要\n\n内容。\n")
        build_mindmap(plan=plan, config=Config(uid=1, mindmap_enabled=False),
                      entry_dir=self.root, title="视频")
        self.assertEqual(plan.mindmap_status, "skipped")
        self.assertEqual(plan.mindmap_reason, "mindmap_disabled")

    def test_upstream_skipped_propagates_reason(self):
        plan = SummaryPlan(status="skipped", reason="llm_not_configured")
        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频")
        self.assertEqual(plan.mindmap_status, "skipped")
        self.assertEqual(plan.mindmap_reason, "llm_not_configured")

    def test_upstream_failed_marks_failed(self):
        plan = self.plan("", status="failed")
        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频")
        self.assertEqual(plan.mindmap_status, "failed")
        self.assertIn("上游总结未完成", plan.mindmap_message)
        self.assertFalse((self.root / MINDMAP_MMD).exists())

    def test_success_clears_previous_failure_reason(self):
        """补做成功：上一次失败的 reason/error_kind 必须清掉。

        否则 retry 后 metadata 里会出现 status=done 与 reason=render_failed
        并存的矛盾（按 reason 判断的人会误报"导图未完成"）。
        """
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        plan.mindmap_status = "failed"
        plan.mindmap_reason = "render_failed"
        plan.mindmap_error_kind = "render_failed"
        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner())
        self.assertEqual(plan.mindmap_status, "done")
        self.assertEqual(plan.mindmap_reason, "")
        self.assertEqual(plan.mindmap_error_kind, "")
        serialized = plan.to_json()["mindmap"]
        self.assertEqual(serialized["status"], "done")
        self.assertEqual(serialized["reason"], "")
        self.assertEqual(serialized["error_kind"], "")

    def test_render_failure_keeps_mmd(self):
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner(ok=False))
        self.assertEqual(plan.mindmap_status, "failed")
        self.assertEqual(plan.mindmap_reason, "render_failed")
        self.assertTrue((self.root / MINDMAP_MMD).is_file(), "渲染失败必须保留 .mmd")
        self.assertFalse((self.root / MINDMAP_PNG).exists())
        self.assertIn("retry", plan.mindmap_message)

    def test_missing_mmdc_marks_dependency(self):
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n  - A\n")
        with mock.patch("bili_sub_archive.summarize.probe_mmdc", return_value=""):
            build_mindmap(plan=plan, config=Config(uid=1), entry_dir=self.root, title="视频")
        self.assertEqual(plan.mindmap_reason, "dependency_missing")
        self.assertTrue((self.root / MINDMAP_MMD).is_file())

    def test_node_and_depth_limits_are_applied(self):
        outline = "".join(f"{'  ' * (i % 5)}- 节点{i}\n" for i in range(80))
        plan = self.plan("## 摘要\n\n内容。\n\n## 大纲\n- 主题：T\n" + outline)
        build_mindmap(plan=plan, config=Config(uid=1, mindmap_mmdc_path=str(
            self.config.mindmap_mmdc_path), mindmap_max_nodes=6, mindmap_max_depth=2),
            entry_dir=self.root, title="视频", mmdc_runner=fx.fake_mmdc_runner())
        self.assertLessEqual(plan.mindmap_nodes, 6)
        self.assertLessEqual(plan.outline_depth, 2)

    def test_reads_summary_file_when_text_missing(self):
        (self.root / SUMMARY_MD).write_text(
            f"# 标题\n\n- 元信息\n\n{SUMMARY_BEGIN}\n## 摘要\n\n来自文件。\n{SUMMARY_END}\n",
            encoding="utf-8")
        plan = SummaryPlan(status="done", signature="s", model="m")
        build_mindmap(plan=plan, config=self.config, entry_dir=self.root, title="视频",
                      mmdc_runner=fx.fake_mmdc_runner())
        self.assertEqual(plan.mindmap_status, "done")
        self.assertTrue(plan.outline_nodes >= 1)


def _group_points(points: str, budget: int) -> list[str]:
    from bili_sub_archive.summarize import _group_points as impl

    return impl(points, budget)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
