"""
cloudsync.py - 下载目录 → 百度网盘 自动同步
==============================================
背景：本项目部署在服务器上，下载完成的资源希望自动备份到百度网盘。

实现方式（刻意做得「笨」但可靠）：
  1. 后台线程定时【扫描】所有用户的下载目录，找出新文件，而不是在下载代码里
     逐个埋钩子 —— 这样历史文件、手动放进目录的文件、同步失败重试都能覆盖到。
  2. 用 bypy（百度网盘的第三方命令行客户端）上传，子进程调用，失败不影响主服务。
  3. 每个文件的同步状态落 SQLite（sync_files 表）：已完成的不会重复上传，
     失败的按次数退避重试；服务重启后状态不丢。
  4. 只上传"稳定"的文件：mtime 距今不足 settle_seconds 的先跳过，避免把
     正在写入的文件传上去（下载本身有临时目录，这里只是双保险）。

bypy 的路径语义（容易踩坑，务必注意）：
  bypy 只能操作 /apps/bypy 这一个应用目录，命令行里写 `bilibili` 实际就是
  /apps/bypy/bilibili；写 `/apps/bypy/bilibili` 会变成
  /apps/bypy/apps/bypy/bilibili。所以配置里 remote_dir 一律存【相对路径】。

授权：百度网盘走 OAuth 设备码（redirect_uri=oob）。
  - `auth_url()` 生成授权链接，用户在浏览器里打开、登录、复制授权码；
  - `authorize(code)` 把授权码喂给 `bypy info`，成功后 token 落在
    <config_dir>/bypy.json，之后所有命令都免授权。
"""

import json
import os
import subprocess
import sys
import threading
import time

import db  # 复用 write_json_file（兼容 Docker 单文件 bind mount）

# 页面里展示/校验用的常量
APPS_ROOT = "/apps/bypy"
REMOTE_DEFAULT = "bilibili"

# bypy 内嵌的应用 key（1.8.9）。可用环境变量覆盖，便于换成自己的应用。
_DEFAULT_API_KEY = "q8WE4EpCsau1oS0MplgMKNBn"
AUTH_URL = ("https://openapi.baidu.com/oauth/2.0/authorize"
            "?client_id={key}&response_type=code&redirect_uri=oob&scope=basic+netdisk")

DEFAULT_CONFIG = {
    "enabled": False,               # 总开关：默认关闭，避免刚部署就刷网盘
    "remote_dir": REMOTE_DEFAULT,   # 相对 /apps/bypy 的目标目录
    "bypy_config_dir": "",          # 空 = <程序目录>/.bypy
    "interval_seconds": 60,         # 扫描间隔
    "settle_seconds": 300,          # 文件最后修改后至少等这么久才上传
    "on_dup": "skip",               # 远端已存在同名文件时：skip / overwrite
    "delete_local": False,          # 上传成功后是否删除本地文件（默认保留）
    "exclude_suffixes": [".m4s", ".part", ".tmp", ".download", ".crdownload", ".log"],
    "upload_timeout": 7200,         # 单个文件上传超时（秒）
}

_MAX_ATTEMPTS = 5                    # 失败重试上限
_MAX_UPLOADS_PER_SCAN = 50           # 单轮最多上传多少个，避免一次卡太久


def _unique_roots(roots):
    """规范化 + 去重下载根目录（父目录已包含时丢弃子目录，避免同一文件扫两次）。"""
    norm = []
    for r in roots or []:
        if not r:
            continue
        try:
            p = os.path.abspath(os.path.expanduser(str(r)))
        except Exception:
            continue
        if os.path.isdir(p):
            norm.append(p)
    out = []
    for p in sorted(set(norm), key=len):
        if any(p == q or p.startswith(q.rstrip(os.sep) + os.sep) for q in out):
            continue
        out.append(p)
    return out


class CloudSync:
    def __init__(self, base_dir, roots_getter, db_module):
        self.base_dir = base_dir
        self._roots_getter = roots_getter
        self._db = db_module
        self._config_path = (os.environ.get("SYNC_CONFIG_PATH")
                             or os.path.join(base_dir, "sync.json"))

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._busy = False
        self._made_dirs = set()      # 本轮已 mkdir 过的远端目录
        self._last = {"ts": 0, "found": 0, "uploaded": 0, "failed": 0, "skipped": 0, "error": ""}
        self._logs = []              # [(ts, level, text)] 最近 50 条

    # ---------------------------- 配置 ----------------------------
    def _load(self):
        cfg = dict(DEFAULT_CONFIG)
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in DEFAULT_CONFIG and v is not None:
                        cfg[k] = v
        except Exception:
            pass
        return cfg

    def _save(self, cfg):
        db.write_json_file(self._config_path, cfg)

    def config(self):
        return self._load()

    def update_config(self, patch):
        """更新同步配置（只认已知键，做基本类型收敛）。"""
        cfg = self._load()
        for k, v in (patch or {}).items():
            if k not in DEFAULT_CONFIG or v is None:
                continue
            try:
                if k in ("interval_seconds", "settle_seconds", "upload_timeout"):
                    v = max(5, int(v))
                elif k in ("enabled", "delete_local"):
                    v = bool(v)
                elif k == "remote_dir":
                    v = str(v).strip().strip("/")
                    # 用户可能误填完整路径，这里容错：/apps/bypy/xxx → xxx
                    if v.lower().startswith(APPS_ROOT.strip("/")):
                        v = v[len(APPS_ROOT.strip("/")):].strip("/")
                elif k == "on_dup":
                    v = v if v in ("skip", "overwrite") else "skip"
                elif k == "exclude_suffixes":
                    v = [str(x).strip().lower() for x in (v or []) if str(x).strip()]
                elif k == "bypy_config_dir":
                    v = str(v).strip()
            except Exception:
                continue
            cfg[k] = v
        if not cfg.get("remote_dir"):
            cfg["remote_dir"] = REMOTE_DEFAULT
        self._save(cfg)
        if cfg.get("enabled"):
            self.kick()
        return {"ok": True, "config": self._public_config(cfg)}

    def _public_config(self, cfg):
        out = dict(cfg)
        out["config_dir"] = self.config_dir(cfg)
        out["remote_display"] = APPS_ROOT + "/" + (cfg.get("remote_dir") or REMOTE_DEFAULT)
        return out

    def config_dir(self, cfg=None):
        cfg = cfg or self._load()
        d = (cfg.get("bypy_config_dir") or "").strip()
        return os.path.abspath(d) if d else os.path.join(self.base_dir, ".bypy")

    # ---------------------------- 状态 ----------------------------
    def _log(self, level, text):
        with self._lock:
            self._logs.append((time.time(), level, str(text)[:500]))
            if len(self._logs) > 50:
                del self._logs[:-50]

    def _roots(self):
        try:
            return _unique_roots(self._roots_getter())
        except Exception:
            return []

    def installed(self):
        try:
            import importlib.util
            return importlib.util.find_spec("bypy") is not None
        except Exception:
            return False

    def authorized(self, cfg=None):
        return os.path.exists(os.path.join(self.config_dir(cfg), "bypy.json"))

    def status(self):
        cfg = self._load()
        db_counts = {}
        recent = []
        try:
            db_counts = self._db.sync_counts()
        except Exception:
            db_counts = {}
        try:
            recent = self._db.sync_recent(20)
        except Exception:
            recent = []
        return {
            "ok": True,
            "enabled": bool(cfg.get("enabled")),
            "config": self._public_config(cfg),
            "config_dir": self.config_dir(cfg),
            "bypy_installed": self.installed(),
            "authorized": self.authorized(cfg),
            "running": bool(self.is_running()),
            "busy": bool(self._busy),
            "roots": self._roots(),
            "counts": db_counts,
            "recent": recent,
            "last_scan": dict(self._last),
            "logs": [{"ts": int(t), "level": lv, "text": tx} for t, lv, tx in self._logs[-20:]],
        }

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------- 生命周期 ----------------------------
    def start(self):
        if self.is_running():
            return
        self._stop.clear()
        t = threading.Thread(target=self._loop, name="cloudsync", daemon=True)
        t.start()
        self._thread = t
        self._log("info", f"同步线程已启动，配置目录 {self.config_dir()}")

    def stop(self):
        self._stop.set()
        self._wake.set()

    def kick(self):
        """下载完成后调用：立即唤醒一次扫描。"""
        self._wake.set()

    def scan_now(self):
        if not self.installed():
            return {"ok": False, "error": "服务器未安装 bypy，先执行 pip install bypy"}
        if not self.authorized():
            return {"ok": False, "error": "尚未授权百度网盘，请先在弹窗里完成授权"}
        self.kick()
        return {"ok": True, "started": True}

    def _loop(self):
        while not self._stop.is_set():
            cfg = self._load()
            interval = max(15, int(cfg.get("interval_seconds") or 60))
            self._wake.wait(interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                if self._load().get("enabled"):
                    self._scan_and_upload()
            except Exception as e:      # 线程不能死
                self._log("error", f"扫描异常：{e}")

    # ---------------------------- 扫描 + 上传 ----------------------------
    def _scan_and_upload(self):
        with self._lock:
            if self._busy:
                return
            self._busy = True
        stat = {"ts": int(time.time()), "found": 0, "uploaded": 0,
                "failed": 0, "skipped": 0, "error": ""}
        try:
            cfg = self._load()
            if not cfg.get("enabled"):
                return
            if not self.installed():
                stat["error"] = "未安装 bypy"
                return
            if not self.authorized(cfg):
                stat["error"] = "尚未授权百度网盘（先在设置里完成授权）"
                return

            roots = self._roots()
            if not roots:
                stat["error"] = "没有可扫描的下载目录"
                return

            settle = int(cfg.get("settle_seconds") or 300)
            exts = set(cfg.get("exclude_suffixes") or [])
            now = time.time()

            todo = []
            for root in roots:
                for path, st in self._iter_files(root, exts, now, settle):
                    state = self._db.sync_get(path) or {}
                    if (state.get("status") == "done"
                            and int(state.get("size") or -1) == st.st_size
                            and abs(float(state.get("mtime") or 0) - st.st_mtime) < 1.0):
                        stat["skipped"] += 1
                        continue
                    if state.get("status") == "failed" and int(state.get("attempts") or 0) >= _MAX_ATTEMPTS:
                        stat["skipped"] += 1
                        continue
                    todo.append((root, path, st, state))
            stat["found"] = len(todo)

            for root, path, st, state in todo[:_MAX_UPLOADS_PER_SCAN]:
                if self._stop.is_set():
                    break
                if self._upload_one(cfg, root, path, st, state):
                    stat["uploaded"] += 1
                else:
                    stat["failed"] += 1
        except Exception as e:
            stat["error"] = str(e)[:300]
            self._log("error", f"同步失败：{e}")
        finally:
            with self._lock:
                self._busy = False
                self._last = stat

    def _iter_files(self, root, exts, now, settle):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d != "__pycache__")
            for fn in filenames:
                if fn.startswith("."):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext in exts:
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_size <= 0 or now - st.st_mtime < settle:
                    continue
                yield path, st

    def _upload_one(self, cfg, root, path, st, state):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        remote_dir = (cfg.get("remote_dir") or REMOTE_DEFAULT).strip("/")
        remote_file = f"{remote_dir}/{rel}"
        attempts = int(state.get("attempts") or 0) + 1
        self._db.sync_upsert(path, st.st_size, st.st_mtime, "pending",
                             remote_file, "", attempts)
        try:
            if not self._ensure_remote_dir(cfg, os.path.dirname(remote_file)):
                raise RuntimeError("创建远端目录失败")
            rc, out = self._run_bypy(cfg, ["--on-dup", cfg.get("on_dup") or "skip",
                                           "upload", path, remote_file],
                                     timeout=int(cfg.get("upload_timeout") or 7200))
            if rc != 0:
                raise RuntimeError(_tail(out) or f"bypy 退出码 {rc}")
            self._db.sync_upsert(path, st.st_size, st.st_mtime, "done",
                                 remote_file, "", attempts)
            self._log("info", f"已上传 {rel} → {APPS_ROOT}/{remote_file}")
            if cfg.get("delete_local"):
                try:
                    os.remove(path)
                    self._log("info", f"已删除本地文件 {rel}")
                except OSError as e:
                    self._log("warn", f"删除本地文件失败 {rel}：{e}")
            return True
        except Exception as e:
            msg = str(e)[:300]
            self._db.sync_upsert(path, st.st_size, st.st_mtime, "failed",
                                 remote_file, msg, attempts)
            self._log("error", f"上传失败 {rel}（第 {attempts} 次）：{msg}")
            return False

    def _ensure_remote_dir(self, cfg, remote_dir):
        """递归创建远端目录（bypy mkdir 只建一级），进程内缓存已建过的。"""
        parts = [p for p in (remote_dir or "").split("/") if p]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}" if cur else p
            key = (self.config_dir(cfg), cur)
            if key in self._made_dirs:
                continue
            rc, out = self._run_bypy(cfg, ["mkdir", cur], timeout=120)
            # 目录已存在时 bypy 返回非 0，视为成功
            if rc != 0 and "exist" not in (out or "").lower():
                self._log("warn", f"mkdir {cur} 失败：{_tail(out)}")
                return False
            self._made_dirs.add(key)
        return True

    # ---------------------------- bypy 调用 ----------------------------
    def _bypy_cmd(self):
        return [sys.executable, "-m", "bypy"]

    def _prepare_config_dir(self, cfg):
        cdir = self.config_dir(cfg)
        os.makedirs(cdir, exist_ok=True)
        # 预置 lastUpdateCheck，跳过 bypy 联网检查更新（国内会卡住很久）
        path = os.path.join(cdir, "bypy.setting.json")
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        if not data.get("lastUpdateCheck"):
            data["lastUpdateCheck"] = int(time.time())
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except OSError:
                pass
        return cdir

    def _run_bypy(self, cfg, args, timeout=600, stdin_text=None):
        cdir = self._prepare_config_dir(cfg)
        cmd = [*self._bypy_cmd(), "--config-dir", cdir, *args]
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            proc = subprocess.run(cmd, input=stdin_text, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=env, cwd=self.base_dir)
            out = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            return 124, f"bypy 超时（{timeout}s）：{' '.join(args[:3])}"
        except FileNotFoundError:
            return 127, "找不到 Python 解释器或 bypy 模块"
        except Exception as e:
            return 1, str(e)

    # ---------------------------- 授权 ----------------------------
    def auth_url(self):
        key = os.environ.get("BAIDU_API_KEY") or _DEFAULT_API_KEY
        return AUTH_URL.format(key=key)

    def authorize(self, code):
        """用用户粘贴的授权码换取 token。授权码长度约 32 字符。"""
        code = (code or "").strip()
        if len(code) < 16:
            return {"ok": False, "error": "授权码看起来不完整，请重新复制"}
        cfg = self._load()
        if not self.installed():
            return {"ok": False, "error": "服务器未安装 bypy"}
        rc, out = self._run_bypy(cfg, ["info"], timeout=120, stdin_text=code + "\n")
        ok = self.authorized(cfg)
        if not ok:
            self._log("error", f"授权失败：{_tail(out)}")
            return {"ok": False, "error": _tail(out) or "授权失败，请检查授权码是否过期"}
        self._log("info", "百度网盘授权成功")
        self.kick()
        return {"ok": True, "message": "授权成功，已保存凭据"}

    def reset_auth(self):
        cfg = self._load()
        token = os.path.join(self.config_dir(cfg), "bypy.json")
        try:
            if os.path.exists(token):
                os.remove(token)
        except OSError:
            pass
        return {"ok": True}


def _tail(text, limit=400):
    """取输出最后一段有效内容，用于错误提示。"""
    if not text:
        return ""
    lines = [ln.strip() for ln in str(text).replace("\r", "\n").split("\n") if ln.strip()]
    if not lines:
        return ""
    return " | ".join(lines[-3:])[:limit]
