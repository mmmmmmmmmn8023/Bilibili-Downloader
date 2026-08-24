"""
db.py - 多用户存储层（SQLite）
================================
用 Python 标准库 sqlite3 单文件数据库（bili_history.db）存储：
  - downloads : 每用户独立的下载历史（按 user 列隔离）
  - users     : 自建账号（用户名 + pbkdf2 密码哈希 + 盐 + 角色）

  用户【设置/配置】（SESSDATA/画质/代理/下载目录/监控列表等）不再进 SQLite，
  统一存放在同目录的 config.json 中，按 username 分键隔离：
      {"default": {...}, "alice": {...}, "bob": {...}}
  兼容旧版扁平 config.json（整份当作 default 用户）。

  对外接口与原单用户实现保持语义一致，server.py 调用方零改动（历史函数新增
  可选 user 参数，默认 "default" 以兼容未登录/全局兜底场景）。

  角色：'admin'（可管理其他账号）/ 'user'（普通用户）。
  """

import os
import json
import time
import hashlib
import hmac
import threading
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bili_history.db")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")   # 用户设置配置（按 username 分键：{default:{...}, 用户名:{...}}）

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
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    created_at    REAL NOT NULL,
                    role          TEXT NOT NULL DEFAULT 'user'
                )"""
            )

            # 列迁移（针对旧库：downloads 无 user 列 / users 无 role 列）必须在建索引前、
            # 且复用同一连接执行，否则旧库建 idx_user 会因 schema 缓存失败
            _ensure_user_column(conn)
            _ensure_role_column(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dyid ON downloads(data_dyid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bvid ON downloads(bvid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uid ON downloads(up_uid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON downloads(user)")
            conn.commit()
            # 一次性把遗留的 user_config 表迁到 config.json（之后设置不再进 SQLite）
            _migrate_user_config_to_file()
        finally:
            conn.close()


def _ensure_user_column(conn=None):
    """兼容旧库：downloads 表若无 user 列则补齐并回填 default。
    传入 conn 时直接在该连接上迁移（用于 init_db 内、建索引前）。"""
    if conn is not None:
        _migrate_user_column(conn)
        return
    with _db_lock:
        c = _connect()
        try:
            _migrate_user_column(c)
        finally:
            c.close()


def _migrate_user_column(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='downloads'")]
    if "downloads" not in tables:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(downloads)")]
    if "user" not in cols:
        conn.execute("ALTER TABLE downloads ADD COLUMN user TEXT DEFAULT 'default'")
        conn.execute("UPDATE downloads SET user='default' WHERE user IS NULL OR user=''")
        conn.commit()


def _ensure_role_column(conn=None):
    """兼容旧库：users 表若无 role 列则补齐并回填 'user'。
    传入 conn 时直接在该连接上迁移（用于 init_db 内、建索引前）。"""
    if conn is not None:
        _migrate_role_column(conn)
        return
    with _db_lock:
        c = _connect()
        try:
            _migrate_role_column(c)
        finally:
            c.close()


def _migrate_role_column(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'")]
    if "users" not in tables:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        conn.execute("UPDATE users SET role='user' WHERE role IS NULL OR role=''")
        conn.commit()


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
def create_user(username, pw, role="user"):
    """创建用户；用户名已存在抛 ValueError。role 默认 'user'，可为 'admin'。"""
    if not username or not pw:
        raise ValueError("用户名与密码均不能为空")
    if role not in ("user", "admin"):
        raise ValueError("非法角色")
    if user_exists(username):
        raise ValueError("用户名已存在")
    h, s = hash_password(pw)
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO users(username, password_hash, salt, created_at, role) "
                "VALUES (?,?,?,?,?)",
                (username, h, s, time.time(), role),
            )
            conn.commit()
        finally:
            conn.close()


def get_user(username):
    """返回 {password_hash, salt, role} 或 None。"""
    with _db_lock:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT password_hash, salt, role FROM users WHERE username=?", (username,)
            ).fetchone()
            return {"password_hash": r[0], "salt": r[1], "role": r[2] or "user"} if r else None
        finally:
            conn.close()


def user_exists(username):
    return get_user(username) is not None


def get_user_role(username):
    """返回 'admin' / 'user' / None。"""
    rec = get_user(username)
    return rec["role"] if rec else None


def is_admin(username):
    return get_user_role(username) == "admin"


def set_user_role(username, role):
    """设置用户角色（'user' / 'admin'）。"""
    if role not in ("user", "admin"):
        raise ValueError("非法角色")
    if not user_exists(username):
        raise ValueError("用户不存在")
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("UPDATE users SET role=? WHERE username=?", (role, username))
            conn.commit()
        finally:
            conn.close()


def ensure_admin_user(username, pw):
    """确保管理员账号存在且密码/角色正确（供启动引导使用）。
    若账号不存在则创建为 admin；若存在则同步密码与角色（使 .env 中的
    ADMIN_PASSWORD 始终为权威值，忘记密码时可重置）。"""
    if not username or not pw:
        return
    role = "admin"
    h, s = hash_password(pw)
    with _db_lock:
        conn = _connect()
        try:
            if user_exists(username):
                conn.execute(
                    "UPDATE users SET password_hash=?, salt=?, role=? WHERE username=?",
                    (h, s, role, username),
                )
            else:
                conn.execute(
                    "INSERT INTO users(username, password_hash, salt, created_at, role) "
                    "VALUES (?,?,?,?,?)",
                    (username, h, s, time.time(), role),
                )
            conn.execute(
                "UPDATE users SET role='admin' WHERE username=?", (username,)
            )
            conn.commit()
        finally:
            conn.close()


def reset_user_password(username, new_pw):
    """管理员代重置某用户密码。"""
    if not new_pw:
        raise ValueError("新密码不能为空")
    if not user_exists(username):
        raise ValueError("用户不存在")
    h, s = hash_password(new_pw)
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE users SET password_hash=?, salt=? WHERE username=?",
                (h, s, username),
            )
            conn.commit()
        finally:
            conn.close()


def list_users():
    """返回全部用户列表（含角色、注册时间、下载数），按注册时间升序。"""
    with _db_lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT username, role, created_at FROM users ORDER BY created_at ASC"
            ).fetchall()
        finally:
            conn.close()
    result = []
    for username, role, created_at in rows:
        result.append({
            "username": username,
            "role": role or "user",
            "created_at": created_at,
            "download_count": count_user_downloads(username),
        })
    return result


def count_user_downloads(username):
    with _db_lock:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE user=?", (username,)
            ).fetchone()
            return r[0] if r else 0
        finally:
            conn.close()


def delete_user(username, delete_history=False):
    """删除账号（含其独立配置）；delete_history=True 时连带删除其下载历史。"""
    if not user_exists(username):
        raise ValueError("用户不存在")
    with _db_lock:
        conn = _connect()
        try:
            if delete_history:
                conn.execute("DELETE FROM downloads WHERE user=?", (username,))
            conn.execute("DELETE FROM users WHERE username=?", (username,))
            conn.commit()
        finally:
            conn.close()
    # 同时清理该用户在 config.json 中的设置（文件侧，独立于 db 事务）
    delete_user_config(username)


# ----------------------- 用户配置（仅 config.json，按用户分键）-----------------------
_config_lock = threading.RLock()


def _read_config_file():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_config_file(data):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def _normalize_config_store(data):
    """统一 config.json 为 {username: {settings}} 形式；兼容旧版扁平格式
    （旧版整份就是一份配置，当作 default 用户）。"""
    if not isinstance(data, dict):
        return {}
    # 旧格式特征：顶层所有值都不是 dict（直接是 sessdata/qn 等字段）
    if data and not any(isinstance(v, dict) for v in data.values()):
        return {"default": data}
    return data


def load_user_config(username=DEFAULT_USER):
    """读取某用户的配置（来自 config.json，按 username 分键）。无记录返回 {}。"""
    username = username or DEFAULT_USER
    with _config_lock:
        data = _normalize_config_store(_read_config_file())
        cfg = data.get(username)
        return dict(cfg) if isinstance(cfg, dict) else {}


def save_user_config(username, cfg):
    """保存（覆盖）某用户的配置到 config.json。"""
    username = username or DEFAULT_USER
    with _config_lock:
        data = _normalize_config_store(_read_config_file())
        data[username] = cfg
        _write_config_file(data)


def delete_user_config(username):
    """删除某用户在 config.json 中的设置（default 不可删）。"""
    username = username or DEFAULT_USER
    if username == DEFAULT_USER:
        return
    with _config_lock:
        data = _normalize_config_store(_read_config_file())
        if username in data:
            data.pop(username)
            _write_config_file(data)


def _migrate_user_config_to_file():
    """一次性迁移：把遗留的 user_config 表内容写进 config.json 后删除该表，
    确保『设置不再进 SQLite』。全新库无该表则直接跳过。"""
    rows = None
    try:
        with _db_lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT username, config_json FROM user_config"
                ).fetchall()
            finally:
                conn.close()
    except Exception:
        rows = None  # 表不存在等情况，视为无遗留数据

    if rows:
        with _config_lock:
            data = _normalize_config_store(_read_config_file())
            for uname, cj in rows:
                try:
                    cfg = json.loads(cj)
                except Exception:
                    cfg = None
                if isinstance(cfg, dict) and cfg:
                    data.setdefault(uname, cfg)
            _write_config_file(data)

    # 无论是否有数据，都卸下遗留的 user_config 表
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute("DROP TABLE IF EXISTS user_config")
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass

    # 顺带把 config.json 统一规范化为『按用户分键』格式（兼容旧扁平）
    with _config_lock:
        raw = _read_config_file()
        norm = _normalize_config_store(raw)
        if raw != norm:
            _write_config_file(norm)


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
