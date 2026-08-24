"""
db.py - 多用户存储层（SQLite）
================================
用 Python 标准库 sqlite3 单文件数据库（bili_history.db）存储：
  - downloads : 每用户独立的下载历史（按 user 列隔离）
  - users     : 自建账号（用户名 + pbkdf2 密码哈希 + 盐）
  - user_config : 每用户独立配置（SESSDATA/画质/代理/监控列表等）

对外接口与原单用户实现保持语义一致，server.py 调用方零改动（历史函数新增
可选 user 参数，默认 "default" 以兼容未登录/全局兜底场景）。

表结构：
    downloads(id, data_dyid, bvid, title, up_uid, up_name, time, created_at, user)
    users(username PK, password_hash, salt, created_at)
    user_config(username PK, config_json)
"""

import os
import json
import time
import hashlib
import hmac
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bili_history.db")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")   # 未登录/默认用户的配置兜底

_db_lock = threading.RLock()

# 默认用户（未登录 / 自动化下载兜底）。多用户体系下，未登录访问受保护接口会被
# 退回登录页，但自动化下载等后台任务仍需一个合理默认配置，故保留 "default"。
DEFAULT_USER = "default"

PBKDF2_ITERS = 100000


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
                    created_at REAL NOT NULL,
                    user      TEXT NOT NULL DEFAULT 'default'
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dyid ON downloads(data_dyid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bvid ON downloads(bvid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uid ON downloads(up_uid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON downloads(user)")

            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    created_at    REAL NOT NULL
                )"""
            )

            conn.execute(
                """CREATE TABLE IF NOT EXISTS user_config (
                    username    TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL
                )"""
            )
            conn.commit()
        finally:
            conn.close()
    # 旧数据（无 user 列时）迁移：为已有下载历史补齐 user 维度
    _ensure_user_column()


def _ensure_user_column():
    """兼容旧库：downloads 表若无 user 列则补齐并回填 default。"""
    with _db_lock:
        conn = _connect()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(downloads)")]
            if "user" not in cols:
                conn.execute("ALTER TABLE downloads ADD COLUMN user TEXT DEFAULT 'default'")
                conn.execute("UPDATE downloads SET user='default' WHERE user IS NULL OR user=''")
                conn.commit()
        finally:
            conn.close()


# ----------------------- 密码哈希 -----------------------
def hash_password(pw):
    """返回 (hex_hash, hex_salt)，使用 pbkdf2_hmac（标准库，零依赖）。"""
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS)
    return dk.hex(), salt


def verify_password(pw, salt, stored_hash):
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS)
    return hmac.compare_digest(dk.hex(), stored_hash)


# ----------------------- 用户管理 -----------------------
def create_user(username, pw):
    """创建用户；用户名已存在抛 ValueError。"""
    if not username or not pw:
        raise ValueError("用户名与密码均不能为空")
    if user_exists(username):
        raise ValueError("用户名已存在")
    h, s = hash_password(pw)
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO users(username, password_hash, salt, created_at) VALUES (?,?,?,?)",
                (username, h, s, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def get_user(username):
    """返回 {password_hash, salt} 或 None。"""
    with _db_lock:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT password_hash, salt FROM users WHERE username=?", (username,)
            ).fetchone()
            return {"password_hash": r[0], "salt": r[1]} if r else None
        finally:
            conn.close()


def user_exists(username):
    return get_user(username) is not None


# ----------------------- 用户配置（多用户 config）-----------------------
def _load_global_config():
    """读取全局 config.json（作为 default 用户兜底）。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_user_config(username=DEFAULT_USER):
    """读取某用户的配置；无记录时回落到全局 config.json（仅 default 用户）。"""
    username = username or DEFAULT_USER
    with _db_lock:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT config_json FROM user_config WHERE username=?", (username,)
            ).fetchone()
            if r:
                try:
                    return json.loads(r[0])
                except Exception:
                    pass
        finally:
            conn.close()
    # 无独立配置：default 用户回落全局文件，其他用户返回空配置（首次使用）
    if username == DEFAULT_USER:
        return _load_global_config()
    return {}


def save_user_config(username, cfg):
    """保存（覆盖）某用户的配置。"""
    username = username or DEFAULT_USER
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO user_config(username, config_json) VALUES (?, ?) "
                "ON CONFLICT(username) DO UPDATE SET config_json=excluded.config_json",
                (username, json.dumps(cfg, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()


# ----------------------- 下载历史（按 user 隔离）-----------------------
def load_history(user=DEFAULT_USER):
    """返回某用户的扁平 list，按 created_at ASC, id ASC 排序。"""
    user = user or DEFAULT_USER
    with _db_lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT data_dyid,bvid,title,up_uid,up_name,time "
                "FROM downloads WHERE user=? ORDER BY created_at ASC, id ASC",
                (user,),
            )
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


def add_to_history(data_dyid="0", bvid="0", title="", up_uid="", up_name="", user=DEFAULT_USER):
    """追加一条下载历史（按 user 去重：data-dyid 或 bvid 任一命中即跳过）。"""
    user = user or DEFAULT_USER
    did = _norm_val(data_dyid)
    bv = _norm_val(bvid)
    with _db_lock:
        conn = _connect()
        try:
            if did and conn.execute(
                "SELECT 1 FROM downloads WHERE user=? AND data_dyid=?", (user, did)
            ).fetchone():
                return
            if bv and conn.execute(
                "SELECT 1 FROM downloads WHERE user=? AND bvid=?", (user, bv)
            ).fetchone():
                return
            conn.execute(
                "INSERT INTO downloads "
                "(data_dyid,bvid,title,up_uid,up_name,time,created_at,user) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (did, bv, title or "", str(up_uid or ""), up_name or "",
                 time.strftime("%Y-%m-%d %H:%M"), time.time(), user),
            )
            conn.commit()
        finally:
            conn.close()


def is_downloaded(data_dyid="0", bvid="0", user=DEFAULT_USER):
    """按 user 查重：data-dyid 或 bvid 任一命中即已下载（空值不参与匹配）。"""
    user = user or DEFAULT_USER
    did = _norm_val(data_dyid)
    bv = _norm_val(bvid)
    with _db_lock:
        conn = _connect()
        try:
            if did and conn.execute(
                "SELECT 1 FROM downloads WHERE user=? AND data_dyid=?", (user, did)
            ).fetchone():
                return True
            if bv and conn.execute(
                "SELECT 1 FROM downloads WHERE user=? AND bvid=?", (user, bv)
            ).fetchone():
                return True
        finally:
            conn.close()
    return False


def clear_history(uid=None, user=DEFAULT_USER):
    """清空某用户历史：指定 uid 则仅清该 UP 主，否则全部清空。"""
    user = user or DEFAULT_USER
    uid = str(uid or "").strip()
    with _db_lock:
        conn = _connect()
        try:
            if uid:
                conn.execute("DELETE FROM downloads WHERE user=? AND up_uid=?", (user, uid))
            else:
                conn.execute("DELETE FROM downloads WHERE user=?", (user,))
            conn.commit()
        finally:
            conn.close()


def remove_history(dyid=None, bvid=None, user=DEFAULT_USER):
    """删除单条：按 data-dyid 或 bvid 匹配（OR 语义，与原逻辑一致）。"""
    user = user or DEFAULT_USER
    dyid = _norm_val(dyid)
    bvid = _norm_val(bvid)
    if not dyid and not bvid:
        return
    conds = ["user=?"]
    params = [user]
    if dyid:
        conds.append("data_dyid=?")
        params.append(dyid)
    if bvid:
        conds.append("bvid=?")
        params.append(bvid)
    sql = "DELETE FROM downloads WHERE " + " AND ".join(conds)
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
