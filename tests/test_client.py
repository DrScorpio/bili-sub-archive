"""传输层测试：WBI 签名、限速/退避、错误分类、图片下载与 URL 升级。

用注入的假传输函数（``transport``）走 :class:`HttpClient` 的真实代码路径，
不联网；这是唯一无法靠 ``FakeApi`` 覆盖的一层。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bili_sub_archive.bili.client import HttpClient, upgrade_url
from bili_sub_archive.bili.wbi import get_mixin_key, sign
from bili_sub_archive.errors import (
    KIND_AUTH,
    KIND_HTTP,
    KIND_INVISIBLE,
    KIND_NETWORK,
    KIND_OK,
    KIND_PARSE,
    KIND_RISK,
    classify,
)
from tests import workspace

IMG_URL = "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
SUB_URL = "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png"


def nav_payload() -> bytes:
    return json.dumps({
        "code": 0, "message": "0",
        "data": {"isLogin": True, "mid": 1, "wbi_img": {"img_url": IMG_URL, "sub_url": SUB_URL}},
    }).encode()


class RecordingTransport:
    """记录每个请求的 URL，并按脚本返回响应。"""

    def __init__(self, script: list[tuple[int, bytes]] | None = None,
                 nav: bytes | None = None):
        self.urls: list[str] = []
        self.headers: list[dict] = []
        self.script = list(script or [])
        self.nav = nav if nav is not None else nav_payload()

    def __call__(self, url: str, headers: dict, timeout: float):
        self.urls.append(url)
        self.headers.append(headers)
        if url.endswith("/x/web-interface/nav"):
            return 200, {"Content-Type": "application/json"}, self.nav
        if self.script:
            status, body = self.script.pop(0)
            return status, {"Content-Type": "application/json"}, body
        return 200, {"Content-Type": "application/json"}, json.dumps(
            {"code": 0, "message": "0", "data": {"ok": True}}).encode()


def client(transport, **kwargs) -> HttpClient:
    kwargs.setdefault("interval", 0.0)
    kwargs.setdefault("retries", 2)
    return HttpClient(cookie="SESSDATA=abcdef", transport=transport,
                      sleep=lambda _s: None, **kwargs)


class ClassifyTest(unittest.TestCase):
    def test_code_mapping(self):
        self.assertEqual(classify(0), KIND_OK)
        self.assertEqual(classify(-101), KIND_AUTH)
        self.assertEqual(classify(-352), KIND_RISK)
        self.assertEqual(classify(62002), KIND_INVISIBLE)
        self.assertEqual(classify(None, 412), KIND_RISK)
        self.assertEqual(classify(None, 429), "rate_limited")
        self.assertEqual(classify(None, 0), KIND_NETWORK)
        self.assertEqual(classify(None, 500), KIND_HTTP)

    def test_exit_code_mapping(self):
        from bili_sub_archive.errors import (
            EXIT_AUTH,
            EXIT_CONFIG,
            exit_code_for_kind,
        )

        # UID 不存在 / 参数错 → 配置问题；登录态 → 3；其余保守按 3
        self.assertEqual(exit_code_for_kind("not_found"), EXIT_CONFIG)
        self.assertEqual(exit_code_for_kind("bad_request"), EXIT_CONFIG)
        self.assertEqual(exit_code_for_kind("denied"), EXIT_CONFIG)
        self.assertEqual(exit_code_for_kind("invisible"), EXIT_CONFIG)
        self.assertEqual(exit_code_for_kind("auth_invalid"), EXIT_AUTH)
        self.assertEqual(exit_code_for_kind("risk_control"), EXIT_AUTH)


class WbiTest(unittest.TestCase):
    def test_mixin_key_length(self):
        self.assertEqual(len(get_mixin_key("a" * 32, "b" * 32)), 32)

    def test_sign_sorted_and_filtered(self):
        signed = sign({"b": "2", "a": "1!'()*2"}, "a" * 32, "b" * 32, wts=1700000000)
        self.assertEqual(signed["wts"], "1700000000")
        self.assertEqual(signed["a"], "12")           # 特殊字符被过滤
        self.assertIn("w_rid", signed)
        self.assertEqual(len(signed["w_rid"]), 32)

    def test_wbi_request_contains_signature(self):
        transport = RecordingTransport()
        http = client(transport)
        result = http.get_json("/x/space/wbi/arc/search", {"mid": 1}, wbi=True,
                               referer="https://space.bilibili.com/1/video")
        self.assertTrue(result.ok)
        self.assertEqual(len(transport.urls), 2)       # 先拿 nav 密钥，再发签名请求
        query = parse_qs(urlparse(transport.urls[-1]).query)
        self.assertIn("w_rid", query)
        self.assertIn("wts", query)
        self.assertEqual(query["mid"], ["1"])
        self.assertEqual(transport.headers[-1]["Cookie"], "SESSDATA=abcdef")

    def test_wbi_keys_cached(self):
        transport = RecordingTransport()
        http = client(transport)
        http.get_json("/x/space/wbi/arc/search", {"mid": 1}, wbi=True)
        http.get_json("/x/space/wbi/arc/search", {"mid": 2}, wbi=True)
        nav_calls = [u for u in transport.urls if "nav" in u]
        self.assertEqual(len(nav_calls), 1)

    def test_missing_wbi_keys_returns_parse_error(self):
        transport2 = lambda url, headers, timeout: (  # noqa: E731
            200, {}, json.dumps({"code": 0, "data": {}}).encode())
        http = client(transport2)
        result = http.get_json("/x/space/wbi/arc/search", {"mid": 1}, wbi=True)
        self.assertEqual(result.error_kind, KIND_PARSE)
        self.assertIn("wbi", result.message)


class RetryTest(unittest.TestCase):
    def test_risk_retry_then_success(self):
        transport = RecordingTransport([
            (200, json.dumps({"code": -352, "message": "风控校验失败"}).encode()),
            (200, json.dumps({"code": 0, "message": "0", "data": {"ok": 1}}).encode()),
        ])
        http = client(transport, retries=2)
        result = http.get_json("/x/player/wbi/v2", {"bvid": "BV1", "cid": 1}, wbi=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(http.retries_used, 1)
        self.assertEqual(http.risk_events, 1)

    def test_risk_exhausted_returns_risk_kind(self):
        transport = RecordingTransport([
            (200, json.dumps({"code": -352, "message": "风控"}).encode()),
            (200, json.dumps({"code": -352, "message": "风控"}).encode()),
        ])
        http = client(transport, retries=2)
        result = http.get_json("/x/player/wbi/v2", {"bvid": "BV1"}, wbi=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, KIND_RISK)
        self.assertEqual(http.consecutive_risk, 2)

    def test_http_412_is_risk(self):
        transport = RecordingTransport([(412, b""), (412, b"")])
        http = client(transport, retries=2)
        result = http.get_json("/x/player/wbi/playurl", {"bvid": "BV1"}, wbi=True)
        self.assertEqual(result.error_kind, KIND_RISK)
        self.assertEqual(result.http_status, 412)

    def test_cookie_invalid(self):
        transport = RecordingTransport([(200, json.dumps({"code": -101, "message": "未登录"}).encode())])
        http = client(transport, retries=1)
        result = http.get_json("/x/web-interface/view", {"bvid": "BV1"})
        self.assertEqual(result.error_kind, KIND_AUTH)

    def test_invisible_62002(self):
        transport = RecordingTransport([(200, json.dumps({"code": 62002, "message": "稿件不可见"}).encode())])
        result = client(transport, retries=1).get_json("/x/web-interface/view", {"bvid": "BV1"})
        self.assertEqual(result.error_kind, KIND_INVISIBLE)

    def test_non_json_response(self):
        transport = RecordingTransport([(200, "<html>风控页面</html>".encode("utf-8"))])
        result = client(transport, retries=1).get_json("/x/web-interface/view")
        self.assertEqual(result.error_kind, KIND_PARSE)
        self.assertIn("非 JSON", result.message)

    def test_network_error(self):
        def boom(url, headers, timeout):
            raise ConnectionResetError("connection reset")

        result = client(boom, retries=1).get_json("/x/web-interface/view")
        self.assertEqual(result.error_kind, KIND_NETWORK)

    def test_server_error_retried(self):
        transport = RecordingTransport([(503, b""),
                                        (200, json.dumps({"code": 0, "data": {}}).encode())])
        http = client(transport, retries=2)
        result = http.get_json("/x/web-interface/view")
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)

    def test_login_state_merges_unauthenticated_message(self):
        from bili_sub_archive.bili.api import BilibiliApi

        unauth = json.dumps({"code": -101, "data": {}, "message": "账号未登录"}).encode()
        transport = RecordingTransport(nav=unauth)
        api = BilibiliApi(client(transport, retries=1))
        logged, mid, note = api.login_state()
        self.assertFalse(logged)
        self.assertEqual(mid, 0)
        self.assertIn("不可区分", note)


class DownloadTest(unittest.TestCase):
    def test_download_writes_file_and_upgrades_http(self):
        transport = RecordingTransport([(200, b"\x89PNG-fake")])
        http = client(transport)
        with workspace() as tmp:
            dest = tmp / "images" / "01.png"
            result = http.download("http://i0.hdslb.com/bfs/new_dyn/a.png", dest)
            self.assertTrue(result.ok)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_bytes(), b"\x89PNG-fake")
            self.assertEqual(transport.urls[0][:5], "https")
            self.assertEqual(http.bytes_downloaded, 9)

    def test_download_failure_leaves_no_file(self):
        transport = RecordingTransport([(404, b""), (404, b"")])
        http = client(transport, retries=2)
        with workspace() as tmp:
            dest = tmp / "images" / "01.png"
            result = http.download("https://i0.hdslb.com/x.png", dest)
            self.assertFalse(result.ok)
            self.assertEqual(result.error_kind, KIND_HTTP)
            self.assertFalse(dest.exists())

    def test_download_empty_url(self):
        result = client(RecordingTransport()).download("", Path("x.png"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, KIND_PARSE)

    def test_upgrade_url(self):
        self.assertEqual(upgrade_url("http://a/b.png"), "https://a/b.png")
        self.assertEqual(upgrade_url("//a/b.png"), "https://a/b.png")
        self.assertEqual(upgrade_url("https://a/b.png"), "https://a/b.png")
        self.assertEqual(upgrade_url(""), "")


class ThrottleTest(unittest.TestCase):
    def test_interval_respected_between_requests(self):
        sleeps: list[float] = []
        transport = RecordingTransport()
        http = HttpClient(cookie="", interval=1.0, transport=transport,
                          sleep=lambda s: sleeps.append(s))
        http.get_json("/x/web-interface/view", {"bvid": "BV1"})
        http.get_json("/x/web-interface/view", {"bvid": "BV2"})
        self.assertTrue(sleeps, "第二次请求前应等待，保证请求间隔")
        self.assertGreaterEqual(max(sleeps), 0.5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
