"""核心改造的回归测试（不联网、不需要 curl_cffi / ffmpeg）。

覆盖：
  - db.py：迁移到 v2、历史重复行清理、部分唯一索引、add_to_history 返回值
  - bilibili.py：画质表单一数据源、任务快照不泄露 params(cookie)、任务清理
  - server.py：路由表与 Handler 方法一一对应、SESSDATA 脱敏、端口解析

运行：
    python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_curl_cffi_stub():
    """server.py/bilibili.py 顶层 import curl_cffi，本机没装依赖时用最小桩顶替。"""
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

import bilibili  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


class DbMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmpdir.name, "history.db")

    def tearDown(self):
        db.DB_PATH = self._old_path
        self.tmpdir.cleanup()

    def _raw(self):
        conn = sqlite3.connect(db.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def test_init_is_idempotent_and_sets_version(self):
        db.init_db()
        db.init_db()
        conn = self._raw()
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            names = {r["name"] for r in conn.execute("PRAGMA index_list(downloads)")}
            self.assertIn("uq_dyid", names)
            self.assertIn("uq_bvid", names)
        finally:
            conn.close()

    def test_legacy_duplicates_are_cleaned_before_unique_index(self):
        # 手工造一个 v1 老库：有重复行、没有唯一索引、user_version=1
        conn = self._raw()
        conn.execute(
            "CREATE TABLE downloads (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " data_dyid TEXT NOT NULL DEFAULT '', bvid TEXT NOT NULL DEFAULT '',"
            " title TEXT NOT NULL DEFAULT '', up_uid TEXT NOT NULL DEFAULT '',"
            " up_name TEXT NOT NULL DEFAULT '', time TEXT NOT NULL DEFAULT '',"
            " created_at REAL NOT NULL)"
        )
        rows = [
            ("dy1", ""), ("dy1", ""),      # data_dyid 重复
            ("", "BV1"), ("", "BV1"),      # bvid 重复
            ("dy2", "BV2"),                # 正常
            ("", ""), ("", ""),            # 两个空键：部分索引不管，允许并存
        ]
        for did, bv in rows:
            conn.execute(
                "INSERT INTO downloads (data_dyid,bvid,title,up_uid,up_name,time,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (did, bv, "t", "", "", "2026-01-01", 1.0),
            )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        db.init_db()

        conn = self._raw()
        try:
            total = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            self.assertEqual(total, 5)   # 7 行 → 去掉 2 行（dy1 与 BV1 各留最早一条）
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            # 唯一索引生效后，重复写入被数据库拦下
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO downloads (data_dyid,bvid,title,up_uid,up_name,time,created_at)"
                    " VALUES ('dy1','','x','','','t',2.0)"
                )
        finally:
            conn.close()

    def test_add_to_history_returns_bool_and_dedupes(self):
        db.init_db()
        self.assertTrue(db.add_to_history(bvid="BV1", title="a"))
        self.assertFalse(db.add_to_history(bvid="BV1", title="b"))
        self.assertTrue(db.add_to_history(data_dyid="99", title="c"))
        self.assertTrue(db.is_downloaded(bvid="BV1"))
        self.assertEqual(len(db.load_history()), 2)


class QualityTableTest(unittest.TestCase):
    def test_single_source_of_truth(self):
        self.assertEqual(bilibili.DASH_QNS, [q for q, _ in bilibili.QN_TABLE])
        self.assertEqual(bilibili.QN_LABEL, dict(bilibili.QN_TABLE))
        self.assertEqual(len(bilibili.DASH_QNS), len(set(bilibili.DASH_QNS)))
        # 必须是从高到低的降级顺序
        self.assertEqual(bilibili.DASH_QNS, sorted(bilibili.DASH_QNS, reverse=True))
        self.assertEqual(bilibili.DASH_QNS[0], 127)
        self.assertIn(80, bilibili.DASH_QNS)


class TaskSnapshotTest(unittest.TestCase):
    def setUp(self):
        with bilibili.tasks_lock:
            bilibili.download_tasks.clear()

    def tearDown(self):
        with bilibili.tasks_lock:
            bilibili.download_tasks.clear()

    def test_public_snapshot_strips_params(self):
        with bilibili.tasks_lock:
            bilibili.download_tasks["t1"] = {
                "status": "downloading", "progress": 12, "time": 1.0,
                "title": "标题", "params": {"cookie": "SECRET", "dynamic": {"id": "1"}},
            }
        snap = bilibili.snapshot_tasks()
        self.assertEqual(snap["t1"]["status"], "downloading")
        self.assertEqual(snap["t1"]["progress"], 12)
        self.assertNotIn("params", snap["t1"])
        self.assertNotIn("SECRET", repr(snap))
        # 内部用途仍能拿到完整参数（重试需要）
        self.assertEqual(bilibili.snapshot_tasks(public=False)["t1"]["params"]["cookie"], "SECRET")

    def test_snapshot_is_a_copy(self):
        with bilibili.tasks_lock:
            bilibili.download_tasks["t2"] = {"status": "queued", "progress": 0}
        snap = bilibili.snapshot_tasks()
        snap["t2"]["status"] = "hacked"
        self.assertEqual(bilibili.get_task("t2")["status"], "queued")

    def test_cleanup_keeps_active_and_newest(self):
        with bilibili.tasks_lock:
            for i in range(5):
                bilibili.download_tasks[f"d{i}"] = {"status": "done", "time": float(i)}
            bilibili.download_tasks["run"] = {"status": "downloading", "time": 0.0}
        removed = bilibili.cleanup_finished_tasks(max_total=3, keep=2)
        self.assertEqual(removed, 3)
        self.assertIn("run", bilibili.download_tasks)
        self.assertIn("d4", bilibili.download_tasks)
        self.assertIn("d3", bilibili.download_tasks)


class ServerWiringTest(unittest.TestCase):
    def test_every_route_maps_to_existing_handler(self):
        for table in (server.GET_ROUTES, server.POST_ROUTES):
            for path, name in table.items():
                self.assertTrue(path.startswith("/api/"), path)
                self.assertTrue(callable(getattr(server.Handler, name, None)),
                                f"路由 {path} 指向不存在的 Handler.{name}")

    def test_get_and_post_tables_have_no_duplicate_paths(self):
        self.assertEqual(len(server.GET_ROUTES), len(set(server.GET_ROUTES)))
        self.assertEqual(len(server.POST_ROUTES), len(set(server.POST_ROUTES)))

    def test_mask_secret(self):
        self.assertEqual(server._mask_secret(""), "")
        self.assertEqual(server._mask_secret("short"), "*****")
        masked = server._mask_secret("abcdefghijklmnop")
        self.assertEqual(masked[:4], "abcd")
        self.assertEqual(masked[-4:], "mnop")
        self.assertNotIn("efgh", masked)

    def test_resolve_port_priority(self):
        old = os.environ.get("BILI_DL_PORT")
        try:
            os.environ["BILI_DL_PORT"] = "9123"
            self.assertEqual(server._resolve_port({"port": 8001})[0], 9123)
            os.environ.pop("BILI_DL_PORT", None)
            self.assertEqual(server._resolve_port({"port": 8001})[0], 8001)
            self.assertEqual(server._resolve_port({})[0], server.DEFAULT_PORT)
        finally:
            if old is None:
                os.environ.pop("BILI_DL_PORT", None)
            else:
                os.environ["BILI_DL_PORT"] = old

    def test_loopback_detection(self):
        self.assertTrue(server._is_loopback_host("127.0.0.1"))
        self.assertTrue(server._is_loopback_host("localhost"))
        self.assertFalse(server._is_loopback_host("0.0.0.0"))
        self.assertFalse(server._is_loopback_host("192.168.1.10"))


if __name__ == "__main__":
    unittest.main()
