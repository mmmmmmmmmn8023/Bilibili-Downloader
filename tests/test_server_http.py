"""HTTP 层集成测试：真的起一个 ThreadingHTTPServer，用 urllib 打接口。

不联网：只测路由分发、401 鉴权、静态文件、配置脱敏这些纯本地行为。
运行：
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_curl_cffi_stub():
    if "curl_cffi" in sys.modules:
        return
    mod = types.ModuleType("curl_cffi")

    class _Session:
        def __init__(self, *a, **kw):
            pass

        def get(self, *a, **kw):
            raise RuntimeError("curl_cffi stub: no network in tests")

        post = get
        close = lambda self: None

    class CurlOpt:
        pass

    mod.requests = types.SimpleNamespace(Session=_Session, Response=object)
    mod.CurlOpt = CurlOpt
    sys.modules["curl_cffi"] = mod


_install_curl_cffi_stub()

import server  # noqa: E402


class ServerHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self._old_token = server.ACCESS_TOKEN
        server.ACCESS_TOKEN = None

    def tearDown(self):
        server.ACCESS_TOKEN = self._old_token

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path, headers=None):
        req = urllib.request.Request(self._url(path), headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)

    def _get_err(self, path, headers=None):
        try:
            self._get(path, headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")
        self.fail("期望 HTTP 错误，但请求成功了")

    def _post(self, path, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self._url(path), data=data, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")

    def test_index_and_static(self):
        status, body, headers = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("<html", body.lower())

        status, body, headers = self._get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))

    def test_unknown_api_returns_404_json(self):
        code, body = self._get_err("/api/does-not-exist")
        self.assertEqual(code, 404)
        self.assertIn("Not found", body)

    def test_status_is_empty_and_has_no_params_leak(self):
        status, body, _ = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("tasks", json.loads(body))

    def test_config_masks_sessdata(self):
        tmp = tempfile.TemporaryDirectory()
        old = server.CONFIG_FILE
        try:
            server.CONFIG_FILE = os.path.join(tmp.name, "config.json")
            with open(server.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"sessdata": "abcdefghijklmnop", "access_token": "SUPERSECRET",
                           "qn": 80}, f)
            status, body, _ = self._get("/api/config")
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertEqual(data["sessdata"], "")
            self.assertTrue(data["has_sessdata"])
            self.assertIn("*", data["sessdata_masked"])
            self.assertNotIn("SUPERSECRET", body)
            self.assertNotIn("abcdefghijklmnop", body)
            self.assertNotIn("access_token", data)
        finally:
            server.CONFIG_FILE = old
            tmp.cleanup()

    def test_invalid_json_body_returns_400(self):
        data = b"{not json"
        req = urllib.request.Request(self._url("/api/config"), data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
        else:
            self.fail("非法 JSON 应返回 400")

    def test_token_auth_when_not_loopback(self):
        server.ACCESS_TOKEN = "tok123"
        # 无令牌 → 401
        code, body = self._get_err("/api/status")
        self.assertEqual(code, 401)
        self.assertIn("unauthorized", body)
        # 错误令牌 → 401
        self.assertEqual(self._get_err("/api/status", {"X-Access-Token": "nope"})[0], 401)
        # 正确令牌（请求头）→ 200
        self.assertEqual(self._get("/api/status", {"X-Access-Token": "tok123"})[0], 200)
        # 查询串令牌 → 200
        self.assertEqual(self._get("/api/status?token=tok123")[0], 200)
        # 打开首页会下发 HttpOnly cookie，浏览器请求自动带上
        _, _, headers = self._get("/")
        set_cookie = headers.get("Set-Cookie", "")
        self.assertIn("bili_token=tok123", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertEqual(self._get("/api/status", {"Cookie": "bili_token=tok123"})[0], 200)


if __name__ == "__main__":
    unittest.main()
