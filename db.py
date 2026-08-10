"""
db.py - 下载历史 SQLite 存储
================================
用 Python 标准库 sqlite3 单文件数据库（bili_history.db）存储下载历史，
支持 SQL 查询/索引/去重。对外接口与原 JSON 实现
保持语义一致，server.py 调用方零改动。

表结构：
    downloads(id, data_dyid, bvid, title, up_uid, up_name, time, created_at)
- data_dyid / bvid 可能为 ""（空值不参与去重匹配），去重在应用层完成。
- created_at 用于维持插入先后顺序（与原 JSON 的 append 顺序一致）。
- 并发：所有读写串行化在 _db_lock 下，连接每次新建（SQLite 连接轻量），
  并启用 WAL + busy_timeout 避免 "database is locked"。
"""

import os
import time
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bili_history.db")

_db_lock = threading.RLock()


# ----------------------- 工具 -----------------------
def _norm_val(v):
    """历史字段空值规范：None/空/"0" 统一为 ""。"""
    v = v or ""
    return "" if str(v) == "0" else v


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# ----------------------- 初始化 -----------------------
def init_db():
    """建表并建索引。幂等，可重复调用。"""
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS downloads (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    data_dyid TEXT NOT NULL DEFAULT '',
                    bvid      TEXT NOT NULL DEFAULT '',
                    title     TEXT NOT NULL DEFAULT '',
                    up_uid    TEXT NOT NULL DEFAULT '',
                    up_name   TEXT NOT NULL DEFAULT '',
                    time      TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dyid ON downloads(data_dyid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bvid ON downloads(bvid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uid ON downloads(up_uid)")
            conn.commit()
        finally:
            conn.close()


# ----------------------- 对外接口（语义同旧 JSON 实现） -----------------------
def load_history():
    """返回扁平 list，每条含 up_uid 字段，按 created_at ASC, id ASC 排序。"""
    with _db_lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT data_dyid,bvid,title,up_uid,up_name,time "
                "FROM downloads ORDER BY created_at ASC, id ASC")
            rows = cur.fetchall()
        finally:
            conn.close()
    return [{
        "data-dyid": data_dyid,
        "bvid": bvid,
        "title": title,
        "time": tm,
        "up_name": up_name,
        "up_uid": up_uid,
    } for data_dyid, bvid, title, up_uid, up_name, tm in rows]


def add_to_history(data_dyid="0", bvid="0", title="", up_uid="", up_name=""):
    """追加一条下载历史（全局去重：data-dyid 或 bvid 任一命中即跳过）。"""
    did = _norm_val(data_dyid)
    bv = _norm_val(bvid)
    with _db_lock:
        conn = _connect()
        try:
            if did and conn.execute("SELECT 1 FROM downloads WHERE data_dyid=?", (did,)).fetchone():
                return
            if bv and conn.execute("SELECT 1 FROM downloads WHERE bvid=?", (bv,)).fetchone():
                return
            conn.execute(
                "INSERT INTO downloads "
                "(data_dyid,bvid,title,up_uid,up_name,time,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (did, bv, title or "", str(up_uid or ""), up_name or "",
                 time.strftime("%Y-%m-%d %H:%M"), time.time()))
            conn.commit()
        finally:
            conn.close()


def is_downloaded(data_dyid="0", bvid="0"):
    """查重：按 data-dyid 或 bvid 匹配，任一命中即已下载（空值不参与匹配）。"""
    did = _norm_val(data_dyid)
    bv = _norm_val(bvid)
    with _db_lock:
        conn = _connect()
        try:
            if did and conn.execute("SELECT 1 FROM downloads WHERE data_dyid=?", (did,)).fetchone():
                return True
            if bv and conn.execute("SELECT 1 FROM downloads WHERE bvid=?", (bv,)).fetchone():
                return True
        finally:
            conn.close()
    return False


def clear_history(uid=None):
    """清空历史：指定 uid 则仅清该 UP 主，否则全部清空。"""
    uid = str(uid or "").strip()
    with _db_lock:
        conn = _connect()
        try:
            if uid:
                conn.execute("DELETE FROM downloads WHERE up_uid=?", (uid,))
            else:
                conn.execute("DELETE FROM downloads")
            conn.commit()
        finally:
            conn.close()


def remove_history(dyid=None, bvid=None):
    """删除单条：按 data-dyid 或 bvid 匹配（OR 语义，与原逻辑一致）。"""
    dyid = _norm_val(dyid)
    bvid = _norm_val(bvid)
    if not dyid and not bvid:
        return
    conds = []
    params = []
    if dyid:
        conds.append("data_dyid=?")
        params.append(dyid)
    if bvid:
        conds.append("bvid=?")
        params.append(bvid)
    sql = "DELETE FROM downloads WHERE " + " OR ".join(conds)
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
