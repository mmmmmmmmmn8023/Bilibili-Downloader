"""
server.py - HTTP 服务器（程序入口）
=====================================
用 Python 自带的 http.server 模块搭建一个 Web 服务器。
  - 提供前端网页（static/index.html）
  - 处理前端的 API 请求（搜索UP主、下载视频、下载动态、自动化下载等）

运行方式：python server.py
然后浏览器打开 http://localhost:8000
"""

import os
import json
import uuid
import time
import base64
import threading
import logging
from logging.handlers import TimedRotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request

import bilibili
from bilibili import (
    BilibiliAPI, download_tasks, update_task, sanitize_filename, dyn_folder_type_from_dynamic,
    DownloadCancelled, cancel_task, is_cancelled, clear_cancel, clear_all_cancels, _classify_error, tasks_lock,
)

try:
    import qrcode
    from io import BytesIO
    _QR_AVAILABLE = True
except Exception:
    _QR_AVAILABLE = False

# ========================================================
# 配置
# ========================================================

PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
HISTORY_FILE = os.path.join(BASE_DIR, "download_history.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# ========================================================
# 日志系统（控制台 + 按天滚动文件）
# ========================================================

os.makedirs(LOGS_DIR, exist_ok=True)
_logger = logging.getLogger("bili-dl")
_logger.setLevel(logging.DEBUG)

# 控制台 handler
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
_logger.addHandler(_console)

# 文件 handler（每天滚动，保留 7 天）
_file_handler = TimedRotatingFileHandler(
    os.path.join(LOGS_DIR, "server.log"),
    when="midnight", interval=1, backupCount=7, encoding="utf-8",
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
_logger.addHandler(_file_handler)

# 静默 urllib / urllib3 的调试输出
logging.getLogger("urllib3").setLevel(logging.WARNING)

# 下载历史和配置的读写锁
_file_lock = threading.RLock()  # 可重入锁，允许 add_to_history 在读-改-写全程持锁

# 默认画质：1080P
_DEFAULT_QN = 80

# ========================================================
# 下载调度（并发队列）
#   用线程池限制同时下载数量：先下载一部分，完成的槽位腾出再派下一批，
#   避免批量下载时几十个任务同时堆出、界面卡顿、轮询 payload 暴涨。
# ========================================================
_download_executor = None
_executor_workers = 0
_executor_lock = threading.Lock()
DEFAULT_MAX_CONCURRENT = 2          # 默认同时下载数量（用户可在设置里调）
TASK_KEEP_DONE = 50                 # 自动清理后保留的已完成任务上限
TASK_AUTO_CLEANUP_THRESHOLD = 150   # 任务总数超过此值时触发自动清理

# ========================================================
# 扫码登录状态（内存态，重启即丢，符合「本地临时票据」语义）
#   _qr_state: {qrcode_key, image, expires_at, last_status, last_message}
#   expires_at 用于前端倒计时与后端过期判定（B站 默认 180 秒）
# ========================================================
_qr_lock = threading.Lock()
_qr_state = {"qrcode_key": None, "image": None, "expires_at": 0, "last_status": None, "last_message": ""}
QR_TTL = 180  # 秒，B站 二维码有效期



def get_max_concurrent():
    try:
        return max(1, min(8, int(load_config().get("max_concurrent", DEFAULT_MAX_CONCURRENT) or DEFAULT_MAX_CONCURRENT)))
    except Exception:
        return DEFAULT_MAX_CONCURRENT


def get_download_executor():
    """获取（按需重建）下载线程池。max_concurrent 变更后下次调用自动套用。"""
    global _download_executor, _executor_workers
    n = get_max_concurrent()
    with _executor_lock:
        if _download_executor is None or _executor_workers != n:
            if _download_executor is not None:
                try:
                    _download_executor.shutdown(wait=False)
                except Exception:
                    pass
            _download_executor = ThreadPoolExecutor(max_workers=n)
            _executor_workers = n
        return _download_executor


def _autoclean_tasks():
    """任务总数过多时，清理最旧的已完成任务，保留最近的 TASK_KEEP_DONE 条。"""
    try:
        with tasks_lock:
            if len(download_tasks) <= TASK_AUTO_CLEANUP_THRESHOLD:
                return
            finished = [tid for tid, t in download_tasks.items()
                        if t.get("status") in ("done", "error", "cancelled")]
            finished.sort(key=lambda tid: download_tasks[tid].get("time", 0))
            remove = finished[:-TASK_KEEP_DONE] if len(finished) > TASK_KEEP_DONE else []
            for tid in remove:
                download_tasks.pop(tid, None)
    except Exception:
        pass


def _norm_val(v):
    """历史字段空值规范：None/空/"0" 统一为 ""。"""
    v = v or ""
    return "" if str(v) == "0" else v


def _norm_record(r):
    """把一条记录规范化：去掉 up_uid（由分组表达），空值转 ""。"""
    if not isinstance(r, dict):
        return {}
    return {
        "data-dyid": _norm_val(r.get("data-dyid")),
        "bvid": _norm_val(r.get("bvid")),
        "title": r.get("title") or "",
        "time": r.get("time") or "",
        "up_name": r.get("up_name") or "",
    }


def _load_history_raw():
    """读取原始历史，返回 [{"up_uid": "...", "records": [...]}, ...]。
    记录内不含 up_uid 字段（由分组表达）；空值统一为 ""。
    兼容：旧 videos/dynamics dict → 返回空列表（不迁移）；
          上一版 dict-by-uid / 扁平 list → 当场归一化（不写回）。"""
    with _file_lock:
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        # 旧格式 {"videos":[...], "dynamics":[...]} → 不迁移
        if isinstance(data, dict) and ("videos" in data or "dynamics" in data):
            return []
        # 目标格式：数组 of 分组对象（含 "records" 键）
        if isinstance(data, list):
            if data and isinstance(data[0], dict) and "records" in data[0]:
                return [{
                    "up_uid": str(g.get("up_uid", "")),
                    "records": [_norm_record(r) for r in g.get("records", []) if isinstance(r, dict)],
                } for g in data if isinstance(g, dict)]
            # 扁平 list（每条含 up_uid）→ 按 uid 分组
            by_uid = {}
            for r in data:
                if not isinstance(r, dict):
                    continue
                uid = str(r.get("up_uid") or "0")
                by_uid.setdefault(uid, []).append(_norm_record(r))
            return [{"up_uid": uid, "records": lst} for uid, lst in by_uid.items()]
        # 上一版 dict-by-uid：{uid: [records...]}
        if isinstance(data, dict):
            return [{
                "up_uid": str(uid),
                "records": [_norm_record(r) for r in lst if isinstance(r, dict)],
            } for uid, lst in data.items() if isinstance(lst, list)]
        return []


def load_history():
    """返回扁平 list（兼容所有调用方与前端），每条注入 up_uid。"""
    raw = _load_history_raw()
    out = []
    for grp in raw:
        uid = grp.get("up_uid", "")
        for r in grp.get("records", []):
            item = dict(r)
            item["up_uid"] = uid
            out.append(item)
    return out


def save_history(history):
    with _file_lock:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)


def add_to_history(data_dyid="0", bvid="0", title="", up_uid="", up_name=""):
    # 读-改-写全程持 _file_lock（RLock），防止并发写入互相覆盖
    with _file_lock:
        raw = _load_history_raw()
        did = _norm_val(data_dyid)
        bv = _norm_val(bvid)
        # 去重（跨分组全局）：data-dyid 或 bvid 任一命中即为重复（空值不参与）
        for grp in raw:
            for item in grp.get("records", []):
                if did and item.get("data-dyid") == did:
                    return
                if bv and item.get("bvid") == bv:
                    return
        uid = str(up_uid or "")
        grp = next((g for g in raw if g.get("up_uid") == uid), None)
        if grp is None:
            grp = {"up_uid": uid, "records": []}
            raw.append(grp)
        grp["records"].append({
            "data-dyid": did,
            "bvid": bv,
            "title": title or "",
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "up_name": up_name or "",
        })
        save_history(raw)


def is_downloaded(data_dyid="0", bvid="0"):
    """查重：按 data-dyid 或 bvid 匹配，任一命中即已下载（空值不参与匹配）"""
    did = _norm_val(data_dyid)
    bv = _norm_val(bvid)
    for item in load_history():
        if did and item.get("data-dyid") == did:
            return True
        if bv and item.get("bvid") == bv:
            return True
    return False


def load_config():
    """读取配置文件（保存SESSDATA等）"""
    with _file_lock:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def get_download_base():
    """下载根目录：优先用配置里的 download_dir，否则用默认 DOWNLOAD_DIR"""
    d = (load_config().get("download_dir") or "").strip()
    return d or DOWNLOAD_DIR


def save_config(config):
    with _file_lock:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)


# ========================================================
# 动态分页缓冲（按 UID 缓存已拉取的动态，避免翻页重复请求 & 一次性狂拉触发风控）
# ========================================================
# ThreadingHTTPServer 每个请求新建 Handler 实例，分页状态必须放在模块级全局，
# 不能放在实例属性里。
_DYN_STATE = {}   # uid -> {"items":[parsed...], "offset":"", "has_more":bool, "cached_at":float}
_dyn_lock = threading.Lock()
_DYN_TTL = 300    # 缓冲 TTL（秒）：5 分钟，超时自动失效重新拉取


def _reset_dyn_state(uid):
    """新搜索时清空该 UID 的动态缓冲，拿到最新数据"""
    with _dyn_lock:
        _DYN_STATE.pop(uid, None)


def _ensure_dyn_buffer(uid, api, target_count):
    """确保缓冲里至少有 target_count 条动态；不够就按游标继续翻页拉取。
    每次翻页至多补【一批】（B站 每批约 10~20 条），网络 IO 在锁外进行，
    并且每批之间 sleep 0.3s 限流，避免连续请求触发 B站「请求过于频繁」(-412/-799)。
    按 id 去重，防止游标分页批间重叠导致重复。
    """
    while True:
        with _dyn_lock:
            st = _DYN_STATE.setdefault(uid, {"items": [], "offset": "", "has_more": True, "cached_at": time.time()})
            if len(st["items"]) >= target_count or not st["has_more"]:
                return
            offset = st["offset"]
        # —— 在锁外做网络请求，避免持锁阻塞其他请求 ——
        try:
            batch, next_offset, has_more = api.fetch_dynamics_batch(uid, offset)
        except Exception:
            # 网络异常不置 has_more=False（下次调用仍可重试），仅回退本次翻页
            return
        with _dyn_lock:
            st = _DYN_STATE.setdefault(uid, {"items": [], "offset": "", "has_more": True})
            # 去重键：动态 id 唯一；同 bvid 仅对同类标签去重（投稿视频/动态视频），
            # 不互相过滤转发（type_label="转发"）与投稿视频（type_label="视频"）。
            existing = {d.get("id") for d in st["items"]}
            existing_bvid_labels = {}
            for d in st["items"]:
                if d.get("type") == "video" and d.get("bvid"):
                    lbl = d.get("type_label", "")
                    existing_bvid_labels.setdefault(d["bvid"], set()).add(lbl)
            for p in batch:
                pid = p.get("id")
                if not pid:
                    continue
                if pid in existing:
                    continue
                pbv = p.get("bvid") if (p.get("type") == "video" and p.get("bvid")) else None
                if pbv and pbv in existing_bvid_labels:
                    plbl = p.get("type_label", "")
                    if plbl in existing_bvid_labels[pbv]:
                        continue
                st["items"].append(p)
                existing.add(pid)
                if pbv:
                    existing_bvid_labels.setdefault(pbv, set()).add(p.get("type_label", ""))
            if not has_more or not next_offset:
                st["has_more"] = False
            else:
                st["offset"] = next_offset
        if not batch:
            with _dyn_lock:
                _DYN_STATE.setdefault(uid, {"items": [], "offset": "", "has_more": True})["has_more"] = False
            break
        time.sleep(0.3)  # 限流保护


def _dyn_buffer_snapshot(uid):
    """读取缓冲快照：已解析列表、是否还有更多、累计条数、图文(type==image)条数。
    如果缓冲已超过 TTL，视为失效并清空，让调用方重新拉取。"""
    with _dyn_lock:
        st = _DYN_STATE.get(uid, {"items": [], "offset": "", "has_more": False})
        cached_at = st.get("cached_at", 0)
        if cached_at and time.time() - cached_at > _DYN_TTL:
            _DYN_STATE.pop(uid, None)
            st = {"items": [], "offset": "", "has_more": False}
        items = st["items"]
        return (
            items,
            st["has_more"],
            len(items),
            sum(1 for d in items if d.get("type") == "image"),
        )


def _dyn_type_label(d):
    """计算动态的类型标签（与前端 dy-type 徽章、筛选标签一致）：
    充电专属 > 联合投稿 > 动态视频/投稿视频 > 图文/文字/转发等默认标签。
    联合投稿是基于内容关键词的「互斥」分类，优先级与前端 getDynTypeLabel 对齐，
    保证计数互斥、徽章与筛选分类一致（不再既计入真实类型又计入联合投稿）。"""
    if d.get("charge_only"):
        return "充电专属"
    if _is_joint_submission(d):
        return "联合投稿"
    if d.get("type_label") == "视频" and d.get("dyn_video_type"):
        return d["dyn_video_type"]
    return d.get("type_label") or "其他"


# 联合投稿分类的抓取关键词（标题/正文含其一即归为联合投稿）
JOINT_SUBMISSION_KEYWORDS = ("合作视频", "联合投稿")

def _is_joint_submission(d):
    """联合投稿分类：动态标题或正文含关键词「合作视频」或「联合投稿」。
    与类型标签（投稿视频/动态视频/图文…）正交——联合投稿可以是其中任意一种类型。"""
    blob = f"{d.get('title') or ''}\n{d.get('text') or ''}"
    return any(kw in blob for kw in JOINT_SUBMISSION_KEYWORDS)

def _dyn_type_counts(items):
    """统计整个缓冲里各类型标签的数量（TAB 级筛选栏的计数来源）"""
    counts = {}
    for d in items:
        label = _dyn_type_label(d)
        counts[label] = counts.get(label, 0) + 1
    # 联合投稿：基于内容关键词的「正交」分类，与上面类型独立统计
    joint = sum(1 for d in items if _is_joint_submission(d))
    if joint:
        counts["联合投稿"] = joint
    return counts


def _fallback_videos_from_dynamics(uid, api, page, per_page=12):
    """视频列表接口(arc/search)受限时的降级数据源：从动态缓冲提取视频类动态。
    动态缓冲按需翻页拉取（_ensure_dyn_buffer 自带限流），过滤 type=="video"
    且 type_label=="视频" 的条目（投稿视频/动态视频，不含转发），转成视频卡
    结构返回。返回 (videos, total, has_more)。
    """
    _ensure_dyn_buffer(uid, api, max(page * per_page, 12))
    items, has_more, loaded, _ = _dyn_buffer_snapshot(uid)
    dyn_videos = [
        d for d in items
        if d.get("type") == "video" and d.get("type_label") == "视频"
    ]
    total = len(dyn_videos)
    start = (page - 1) * per_page
    page_items = dyn_videos[start:start + per_page]
    videos = []
    for d in page_items:
        videos.append({
            "bvid": d.get("bvid", ""),
            "title": d.get("title", ""),
            "cover": d.get("cover", ""),
            "duration": d.get("duration", ""),
            "play": 0,  # 动态流不提供播放量，前端显示 ▶ 0
            "created": d.get("timestamp", 0),
            "description": "",
            "charge_only": d.get("charge_only", False),
            "dyid": d.get("id", ""),  # 动态id，供已下载判断（动态历史按 data-dyid）
            "fallback": True,
        })
    return videos, total, has_more

# ========================================================
# 自动化下载后台线程
# ========================================================

_auto_busy = False  # 检查是否进行中（用于前端"运行中"指示器）

# 自动化下载实时日志缓冲（前端轮询拉取）
_auto_log = []            # 最近日志，元素 {"id","ts","msg","level"}
_auto_log_seq = 0
_auto_log_lock = threading.Lock()
# 串行化所有"检查"操作（手动检查 / 立即检查 / 定期检查），避免并发线程同时下载同一UP
# 导致重复下载、历史误判和 part 文件/视频流竞争（[Errno 2] / "视频流文件不存在"）。
_auto_check_lock = threading.Lock()

# 后台定时调度器（仅一个实例，线程安全）
_auto_schedule_timer = None     # threading.Timer 引用
_auto_schedule_lock = threading.Lock()

DEFAULT_AUTO_INTERVAL = 1800    # 默认 30 分钟
MIN_AUTO_INTERVAL = 300         # 最短 5 分钟，防止误设过短触发风控


def _auto_notify(cfg, label, total_new):
    """自动检查结束后推送通知（webhook / Server酱 / 企业微信等）。

    配置 notify_urls（逗号分隔的 URL 列表，支持 query 占位）：
      - 普通 webhook：POST JSON {event, label, total_new, time}
      - Server酱(https://sctapi.ftqq.com/SCTxxxx.send)：GET ?title=...&desp=...
    未配置 notify_urls 则静默跳过。任意单条推送失败不影响其余。
    """
    raw = (cfg.get("notify_urls") or "").strip()
    if not raw:
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    payload = {"event": "auto_check_done", "label": label, "total_new": total_new, "time": now}
    for u in urls:
        try:
            if "sctapi.ftqq.com" in u or "sc.ftqq.com" in u:
                # Server酱：GET，title/desp 做 URL 编码
                title = urllib.parse.quote(f"[{label}] 自动下载完成")
                desp = urllib.parse.quote(f"时间：{now}\n本次新增：{total_new} 条")
                url = u + (f"&title={title}&desp={desp}" if "?" in u else f"?title={title}&desp={desp}")
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    auto_log(f"通知已发送(Server酱) 状态={resp.status}")
            else:
                # 普通 webhook：POST JSON
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    u, data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    auto_log(f"通知已发送(webhook {u}) 状态={resp.status}")
        except Exception as e:
            auto_log(f"通知发送失败({u}): {e}", "error")


def _do_auto_check(label="定时检查"):
    """执行一次完整的自动化检查（所有启用 UP 主）。
    供手动触发和定时调度共用，内部加锁保证串行。
    label: 日志前缀（"手动检查"/"定时检查"）。
    """
    global _auto_busy
    _auto_busy = True
    if not _auto_check_lock.acquire(timeout=300):
        auto_log("上一次自动化检查仍在执行（超过 5 分钟），跳过本次", "warn")
        _auto_busy = False
        return
    try:
        cfg = load_config()
        uids = cfg.get("auto_uids") or []
        if not uids:
            auto_log("监控列表为空，跳过检查")
            return
        cookie = cfg.get("sessdata", "")
        proxy = cfg.get("proxy", "")
        # 注：拉动态列表不下载，speed_limit 对 fetch_dynamics_batch 无意义，故不传（下载由 _run_download 内部另行读取）。
        api = BilibiliAPI(cookie, proxy=proxy) if cookie else BilibiliAPI(proxy=proxy)
        seen = set()
        total_new = 0
        for u in uids:
            if not isinstance(u, dict):
                uid = str(u) if u else ""
                uname = uid
            else:
                if not u.get("enabled", True):
                    continue
                uid = u.get("uid", "")
                uname = u.get("name", uid)
            if uid:
                auto_log(f"{label} {uname}({uid})")
                total_new += _auto_download_up(api, uid, uname, seen)
        auto_log(f"{label}完成，本次新增 {total_new} 条")
        # 完成通知（webhook / Server酱等），配置为空则跳过
        _auto_notify(cfg, label, total_new)
    except Exception as e:
        auto_log(f"{label}异常: {e}", "error")
    finally:
        _auto_check_lock.release()
        _auto_busy = False


def _schedule_next_check():
    """安排下一次定时检查（由前一次检查完成时调用，形成闭环）。
    每次读取最新 config 中的 auto_interval 和 auto_schedule_enabled，
    如果关闭则不安排下一次。
    """
    global _auto_schedule_timer
    try:
        with _auto_schedule_lock:
            cfg = load_config()
            enabled = cfg.get("auto_schedule_enabled", False)
            if not enabled:
                auto_log("定时检查已关闭，停止调度")
                _auto_schedule_timer = None
                return
            interval = max(MIN_AUTO_INTERVAL, int(cfg.get("auto_interval", DEFAULT_AUTO_INTERVAL) or DEFAULT_AUTO_INTERVAL))
            _auto_schedule_timer = threading.Timer(interval, _run_scheduled_check)
            _auto_schedule_timer.daemon = True
            _auto_schedule_timer.start()
            mm = interval // 60
            auto_log(f"下一次定时检查: {mm} 分钟后")
    except Exception:
        # 异常时清除占位符，防止 start_auto_scheduler 误判"已在运行"而导致调度器无法恢复
        with _auto_schedule_lock:
            _auto_schedule_timer = None
        raise


def _run_scheduled_check():
    """定时器回调：执行检查，完成后安排下一次。"""
    _do_auto_check(label="定时检查")
    _schedule_next_check()


def start_auto_scheduler():
    """启动定时检查调度器（由 main() 调用）。
    如果 config 中 auto_schedule_enabled 为 false，不启动。
    已启动时重复调用是安全的（幂等）。
    """
    with _auto_schedule_lock:
        global _auto_schedule_timer
        if _auto_schedule_timer is not None:
            return  # 已在运行
        cfg = load_config()
        if not cfg.get("auto_schedule_enabled", False):
            return
        _auto_schedule_timer = True  # 占位，防重入
    interval = max(MIN_AUTO_INTERVAL, int(cfg.get("auto_interval", DEFAULT_AUTO_INTERVAL) or DEFAULT_AUTO_INTERVAL))
    mm = interval // 60
    auto_log(f"定时检查已启用，间隔 {mm} 分钟")
    # 首次不立即检查，等一个间隔后再开始（避免启动时立即消耗请求额度）
    _schedule_next_check()


def stop_auto_scheduler():
    """停止定时检查调度器（供配置变更时重建）。"""
    global _auto_schedule_timer
    with _auto_schedule_lock:
        t = _auto_schedule_timer
        _auto_schedule_timer = None
        if t and t is not True:
            t.cancel()


def auto_log(msg, level="info"):
    """记录一条自动化日志：同时输出到控制台/文件、存入环形缓冲供前端轮询展示"""
    global _auto_log_seq
    with _auto_log_lock:
        _auto_log_seq += 1
        _auto_log.append({
            "id": _auto_log_seq,
            "ts": time.strftime("%H:%M:%S"),
            "msg": msg,
            "level": level,
        })
        # 仅保留最近 1000 条，避免内存无限增长
        if len(_auto_log) > 1000:
            del _auto_log[:len(_auto_log) - 1000]
    if level == "error":
        _logger.error(msg)
    else:
        _logger.info(msg)


# 单 UP 主自动检查连续失败计数（用于风控冷却退避，见 _auto_download_up）
_auto_fail_count = {}
_auto_fail_time = {}


def _auto_submit(kind, params, retry_on_full=True):
    """通过内部 HTTP 复用统一下载队列（与前端点击下载走完全相同的代码路径）。

    这样自动下载也能：进「下载任务」面板（可见 / 可取消 / 重试）、使用全局画质 / 文件夹 /
    文件设置、独立缓存目录、与手动下载行为完全一致。
    kind: 'video' | 'dynamic'。server 是 ThreadingHTTPServer，内部 HTTP 自调用会开新线程处理，
    不会死锁。
    retry_on_full: 当队列满（HTTP 429）时，做 1 次轻量延迟重试，避免瞬时并发打满导致漏下。
    """
    url = f"http://127.0.0.1:{PORT}/api/download/{kind}"
    data = json.dumps(params, ensure_ascii=False).encode("utf-8")

    def _do_post():
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200

    try:
        return _do_post()
    except urllib.error.HTTPError as e:
        # 队列满：做 1 次轻量延迟重试（其他错误直接失败）
        if retry_on_full and getattr(e, "code", 0) == 429:
            auto_log(f"自动提交队列已满({kind})，2 秒后重试 1 次")
            time.sleep(2)
            try:
                return _do_post()
            except Exception as e2:
                auto_log(f"自动提交下载重试失败({kind}): {e2}", "error")
                return False
        auto_log(f"自动提交下载失败({kind}): {e}", "error")
        return False
    except Exception as e:
        auto_log(f"自动提交下载失败({kind}): {e}", "error")
        return False


def _auto_download_up(api, uid, uname, seen=None):
    """自动下载UP主的最新内容（遵循下载类型设置）。
    逐批检查，遇到全部已下载的批就停止，不继续向后翻。
    seen: 本次检查的去重集合（跨UP主共享）。
    """
    if seen is None:
        seen = set()
    try:
        cfg = load_config()
        types = cfg.get("download_types") if "download_types" in cfg else ["投稿视频", "动态视频", "图文", "文字", "转发", "联合投稿"]
        cookie = cfg.get("sessdata", "")
        # 风控冷却：连续失败达到阈值则该 UP 冷却 N 分钟，本次跳过（避免被风控时全量狂打）
        cooldown_min = int(cfg.get("auto_cooldown_minutes", 30) or 0)
        # 多 P 集数上限：0 表示不限（仅视频类生效）
        max_pages = int(cfg.get("max_pages", 0) or 0)

        # 起始日期过滤
        cutoff_end = None
        cd = cfg.get("auto_cutoff_date", "")
        if cd:
            try:
                from datetime import datetime
                cutoff_end = datetime.strptime(cd, "%Y-%m-%d").timestamp() + 86400
            except Exception:
                pass

        # —— 风控冷却检查 ——
        if cooldown_min > 0 and uid in _auto_fail_time:
            last_fail = _auto_fail_time[uid]
            fails = _auto_fail_count.get(uid, 0)
            if fails >= 3 and (time.time() - last_fail) < cooldown_min * 60:
                auto_log(f"{uname}({uid}): 近期连续失败 {fails} 次，冷却中（{cooldown_min} 分钟），本次跳过")
                return 0
            elif fails >= 3 and (time.time() - last_fail) >= cooldown_min * 60:
                # 冷却结束，重置计数再试
                _auto_fail_count[uid] = 0
                _auto_fail_time.pop(uid, None)

        offset = ""
        total_new = 0
        empty_batch_count = 0  # 连续空批计数，稳定后停止

        for batch_no in range(1, 51):  # 安全上限 50 批
            dyns, next_offset, has_more = api.fetch_dynamics_batch(uid, offset)
            if not dyns:
                empty_batch_count += 1
                if empty_batch_count >= 3:
                    break
                if not has_more:
                    break
                offset = next_offset or ""
                if not offset:
                    break
                continue

            empty_batch_count = 0
            # passed_filter：本批是否有「通过类型+日期过滤」的动态。
            # 与旧版 batch_all_old（是否有提交下载）不同 —— 即便本批全因类型/日期被跳过，
            # 只要确实扫到了「该类型/日期范围内」的动态，就不应误判整批已下载而 break，
            # 否则可能漏掉排在批后面、真正通过过滤的新内容。
            passed_filter = False
            sub_new = 0

            for d in dyns:
                dtype = d.get("type", "")
                # 类型标签映射
                if d.get("charge_only"):
                    label = "充电专属"
                elif _is_joint_submission(d):
                    label = "联合投稿"
                elif dtype == "video":
                    label = "动态视频"
                elif dtype == "image":
                    label = "图文"
                elif dtype == "text":
                    label = "文字"
                elif dtype == "forward":
                    label = "转发"
                else:
                    label = "未知"

                # 日期过滤
                if cutoff_end is not None:
                    pub_ts = d.get("timestamp") or 0
                    if not pub_ts or int(pub_ts) <= cutoff_end:
                        continue
                # 类型过滤
                if label not in types:
                    continue

                # 已通过类型+日期过滤，标记本批确有相关内容（用于「提前停」判据）
                passed_filter = True

                # 去重检查
                is_old = False
                if dtype == "video" and d.get("bvid"):
                    is_old = is_downloaded(data_dyid=str(d.get("id", "")), bvid=str(d.get("bvid", "")))
                elif dtype == "forward" and d.get("bvid"):
                    is_old = is_downloaded(data_dyid=str(d.get("id", "")), bvid=str(d.get("bvid", "")))
                else:
                    did = d.get("id", "")
                    is_old = bool(did) and is_downloaded(data_dyid=did)

                if is_old:
                    continue

                sub_new += 1

                # --- 提交下载 ---
                if dtype == "video" and d.get("bvid"):
                    bvid = d["bvid"]
                    # 原创视频与转发视频用不同 key 区分，避免互相“吃掉”下载
                    key = ("video", bvid)
                    if key in seen:
                        continue
                    seen.add(key)
                    params = {
                        "dynamic": d,
                        "title": (d.get("title") or d.get("text") or "")[:30],
                        "username": uname, "uid": uid, "cookie": cookie,
                        "task_type": "dynamic", "max_pages": max_pages,
                    }
                    if _auto_submit("dynamic", params):
                        auto_log(f"{label} 已提交下载: {bvid} {d.get('title','')}")
                        total_new += 1
                    else:
                        auto_log(f"{label} 提交失败: {bvid}", "error")
                elif dtype == "forward" and d.get("bvid"):
                    bvid = d["bvid"]
                    key = ("forward", bvid)
                    if key in seen:
                        continue
                    seen.add(key)
                    params = {
                        "bvid": bvid,
                        "title": d.get("title", ""),
                        "username": uname, "uid": uid,
                        "dynType": dyn_folder_type_from_dynamic(d),
                        "cookie": cookie, "task_type": "video", "max_pages": max_pages,
                    }
                    if _auto_submit("video", params):
                        auto_log(f"{label} 已提交下载: {bvid} {d.get('title','')}")
                        total_new += 1
                    else:
                        auto_log(f"{label} 提交失败: {bvid}", "error")
                else:
                    did = d.get("id", "")
                    key = ("dyn", str(did))
                    if not did or key in seen:
                        continue
                    seen.add(key)
                    params = {
                        "dynamic": d,
                        "title": (d.get("title") or d.get("text") or "")[:30],
                        "username": uname, "uid": uid, "cookie": cookie,
                        "task_type": "dynamic", "max_pages": max_pages,
                    }
                    if _auto_submit("dynamic", params):
                        auto_log(f"{label} 已提交下载: {did}")
                        total_new += 1
                    else:
                        auto_log(f"{label} 提交失败: {did}", "error")

            # 整批扫完：若本批「有通过过滤的动态、且全部已下载」，则后续批次更老 -> 提前停止。
            # 注意判据是 passed_filter（而非“有提交下载”）—— 即便本批全因去重/已下载被跳过，
            # 只要它确属该类型+日期范围，就说明已扫到历史边界；反之若整批都被类型/日期过滤掉
            # （passed_filter=False），则不提前停，继续翻页找真正的目标内容。
            if passed_filter and sub_new == 0 and dyns:
                auto_log(f"{uname}({uid}): 第 {batch_no} 批均为已下载内容，停止继续扫描")
                break

            if not has_more or not next_offset:
                auto_log(f"{uname}({uid}): 已翻到底，共 {total_new} 条新内容")
                break

            offset = next_offset
            time.sleep(0.3)

        # 成功扫完（无论是否有新内容都算本次成功），清除该 UP 失败计数
        _auto_fail_count[uid] = 0
        _auto_fail_time.pop(uid, None)

        if total_new == 0:
            auto_log(f"{uname}({uid}): 无新内容")

        return total_new

    except Exception as e:
        auto_log(f"获取动态失败 uid={uid}: {e}", "error")
        # 记录风控/接口失败，用于冷却退避
        _auto_fail_count[uid] = _auto_fail_count.get(uid, 0) + 1
        _auto_fail_time[uid] = time.time()
        return 0




# ========================================================
# 请求处理器
# ========================================================

class Handler(BaseHTTPRequestHandler):

    # ---- 静态文件 ----

    def do_GET(self):
        """处理 GET 请求（网页访问 + API 查询）"""
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                self._serve_static("index.html", "text/html")
            elif path.startswith("/static/"):
                self._serve_static(path[8:])
            elif path == "/api/search":
                self._api_search(params)
            elif path == "/api/videos":
                self._api_videos(params)
            elif path == "/api/dynamics":
                self._api_dynamics(params)
            elif path == "/api/status":
                self._api_status()
            elif path == "/api/history":
                self._api_history()
            elif path == "/api/config":
                self._api_get_config(params)
            elif path == "/api/check_cookie":
                self._api_check_cookie(params)
            elif path == "/api/self":
                self._api_self(params)
            elif path == "/api/watchlater":
                self._api_watchlater(params)
            elif path == "/api/favorites":
                self._api_favorites(params)
            elif path == "/api/video_pages":
                self._api_video_pages(params)
            elif path == "/api/video_page_counts":
                self._api_video_page_counts(params)
            elif path == "/api/auto/log":
                self._api_auto_log(params)
            elif path == "/api/logs/download":
                self._api_logs_download()
            elif path == "/api/qr/status":
                self._api_qr_status()
            else:
                self._json({"error": "Not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        """处理 POST 请求（设置cookie、发起下载）"""
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_body()
            data = json.loads(body) if body else {}
            if path == "/api/download/video":
                self._api_download_video(data)
            elif path == "/api/download/dynamic":
                self._api_download_dynamic(data)
            elif path == "/api/config":
                self._api_save_config(data)
            elif path == "/api/check_cookie":
                self._api_check_cookie(data)
            elif path == "/api/history/clear":
                self._api_clear_history(data)
            elif path == "/api/history/remove":
                self._api_history_remove(data)
            elif path == "/api/auto/check":
                self._api_auto_check()
            elif path == "/api/auto/status":
                self._api_auto_status(data)
            elif path == "/api/download/cancel":
                self._api_cancel_task(data)
            elif path == "/api/download/retry":
                self._api_retry_task(data)
            elif path == "/api/tasks/clear":
                self._api_clear_tasks(data)
            elif path == "/api/qr/generate":
                self._api_qr_generate()
            elif path == "/api/qr/poll":
                self._api_qr_poll(data)
            else:
                self._json({"error": "Not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ---- 辅助方法 ----

    def _serve_static(self, filename, content_type=None):
        """读取并返回 static 目录下的文件"""
        # 防止路径穿越攻击
        filename = os.path.basename(filename)
        filepath = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(filepath):
            self.send_error(404)
            return

        ext = os.path.splitext(filename)[1].lower()
        types = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
        }
        ct = content_type or types.get(ext, "application/octet-stream")

        with open(filepath, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", f"{ct}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        # 禁用静态文件缓存，避免浏览器沿用旧的 app.js/index.html（前端改动后必须强刷才生效）
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, data, code=200):
        try:
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            # 客户端在响应写完前断开连接（如刷新/关闭页面/取消请求），
            # 属正常网络抖动，静默忽略，不刷 traceback
            pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return ""
        return self.rfile.read(length).decode("utf-8")

    # ---- API 接口 ----

    def _api_search(self, params):
        """搜索UP主：根据 UID 或链接获取信息 + 视频列表 + 动态列表
        每个API调用独立容错，部分失败不影响其他结果返回。
        """
        query = params.get("query", [""])[0]
        cookie = params.get("cookie", [""])[0]
        # 前端未传 cookie 时，从服务端配置兜底取 SESSDATA（与下载逻辑一致）
        if not cookie:
            cookie = load_config().get("sessdata", "")

        api = BilibiliAPI(cookie) if cookie else BilibiliAPI()
        uid = api.parse_uid(query)
        if not uid:
            self._json({"error": "无法解析UID，请输入纯数字UID或B站主页链接"})
            return

        # 三个 API 独立调用，各自容错
        errors = []

        # 1. 用户信息（现在有多种备用方案，基本不会完全失败）
        try:
            user = api.get_user_info(uid)
        except Exception as e:
            user = {
                "uid": int(uid) if str(uid).isdigit() else 0,
                "name": f"UID:{uid}",
                "face": "",
                "sign": "",
                "level": 0,
                "archive_count": 0,
                "album_count": 0,
            }
            errors.append(f"用户信息: {e}")

        # 2. 视频列表（分页，每页12条）
        videos = []
        video_total = 0
        video_page = 1
        video_source = "api"
        video_limited = False
        try:
            result = api.get_user_videos(uid, page=1, page_size=12)
            videos = result["videos"]
            video_total = result["total"]
            video_source = result.get("source", "api")
            video_limited = result.get("limited", False)
            video_page = 1
            # 视频列表接口受限时降级到动态流（首屏同逻辑，保证视频 Tab 有内容）
            if video_limited:
                try:
                    videos, video_total, _ = _fallback_videos_from_dynamics(uid, api, 1, 12)
                    video_source = "dynamic_fallback"
                except Exception as e:
                    errors.append(f"视频降级到动态流: {e}")
        except Exception as e:
            errors.append(f"视频列表: {e}")

        # 3. 动态列表（分页：首屏只拉第1页12条，翻页由前端 /api/dynamics 按需拉取，
        #    避免一次性狂拉触发 B站 风控）
        dynamics = []
        dyn_has_more = False
        dyn_loaded = 0
        dyn_image_text = 0
        dyn_type_counts = {}
        try:
            _reset_dyn_state(uid)
            _ensure_dyn_buffer(uid, api, 12)
            buf_items, has_more, loaded, image_text = _dyn_buffer_snapshot(uid)
            dynamics = buf_items[:12]
            dyn_has_more = has_more or loaded > 12
            dyn_loaded = loaded
            dyn_image_text = image_text
            dyn_type_counts = _dyn_type_counts(buf_items)
        except Exception as e:
            errors.append(f"动态: {e}")

        # 最新投稿：记录最近时间 + 标题 + 直达链接（优先视频，否则动态）
        last_post = 0
        last_post_title = ""
        last_post_url = ""
        for v in videos:
            c = v.get("created") or 0
            if isinstance(c, str):
                try: c = int(c)
                except ValueError: c = 0
            if c > last_post:
                last_post = c
                last_post_title = v.get("title", "")
                bvid = v.get("bvid", "")
                last_post_url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
        if not last_post:
            for d in dynamics:
                t = d.get("timestamp") or 0
                if isinstance(t, str):
                    try: t = int(t)
                    except ValueError: t = 0
                if t > last_post:
                    last_post = t
                    last_post_title = d.get("title") or d.get("text", "") or ""
                    did = d.get("id", "")
                    last_post_url = f"https://t.bilibili.com/{did}" if did else ""
        user["last_post"] = last_post
        user["last_post_title"] = last_post_title[:40]
        user["last_post_url"] = last_post_url

        # 读取下载历史，标记已下载的项目
        for v in videos:
            v["downloaded"] = is_downloaded(bvid=v.get("bvid", ""))
        for d in dynamics:
            d["downloaded"] = is_downloaded(data_dyid=str(d.get("id", "")), bvid=str(d.get("bvid", "")))

        result = {
            "user": user,
            "videos": videos,
            "video_total": video_total,
            "video_source": video_source,
            "video_limited": video_limited,
            "video_fallback": video_source == "dynamic_fallback",
            "video_page": video_page,
            "dynamics": dynamics,
            "dyn_has_more": dyn_has_more,
            "dyn_loaded": dyn_loaded,
            "dyn_image_text": dyn_image_text,
            "dyn_type_counts": dyn_type_counts,
        }
        if errors:
            result["warnings"] = errors
            if not videos and not dynamics and user.get("name", "").startswith("UID:"):
                result["error"] = (
                    "所有请求均失败。请在右上角设置Cookie后重试。"
                    "获取方法：浏览器登录B站 → F12 → Application → Cookies → "
                    "复制 SESSDATA 的值。"
                )

        self._json(result)

    def _api_videos(self, params):
        """获取UP主指定页码的视频列表（分页）"""
        uid = params.get("uid", [""])[0]
        page = int(params.get("page", ["1"])[0])
        cookie = params.get("cookie", [""])[0]

        if not uid:
            self._json({"error": "缺少UID"})
            return

        # 前端未传 cookie 时，从服务端配置兜底取 SESSDATA（与搜索/下载逻辑一致）
        if not cookie:
            cookie = load_config().get("sessdata", "")

        api = BilibiliAPI(cookie) if cookie else BilibiliAPI()
        try:
            result = api.get_user_videos(uid, page=page, page_size=12)
            videos = result["videos"]
            limited = result.get("limited", False)
            source = result.get("source", "api")
            total = result["total"]
            # 视频列表接口受限(-403等)时降级到动态流：从动态缓冲提取视频类动态，
            # 保证视频 Tab 仍有内容可看（近期动态里的视频，非完整投稿列表）
            if limited:
                try:
                    videos, total, _ = _fallback_videos_from_dynamics(uid, api, page, 12)
                    source = "dynamic_fallback"
                except Exception as e:
                    _logger.warning("视频列表降级到动态流失败 uid=%s page=%s: %s", uid, page, e)
            # 标记已下载
            for v in videos:
                v["downloaded"] = is_downloaded(
                    data_dyid=str(v.get("dyid", "")), bvid=v.get("bvid", "")
                )
            self._json({
                "videos": videos,
                "total": total,
                "source": source,
                "limited": limited,
                "fallback": source == "dynamic_fallback",
                "page": page,
            })
        except Exception as e:
            self._json({"error": str(e)})

    def _api_dynamics(self, params):
        """分页获取UP主动态：每次只返回一页（默认12条），按需翻页拉取。
        翻页时由 _ensure_dyn_buffer 按游标补足缓冲（每次至多一批请求），
        避免一次性狂拉触发 B站 风控。同时回传累计条数与图文(type==image)条数。
        """
        uid = params.get("uid", [""])[0]
        page = int(params.get("page", ["1"])[0])
        cookie = params.get("cookie", [""])[0]

        if not uid:
            self._json({"error": "缺少UID"})
            return
        if page < 1:
            page = 1

        # 前端未传 cookie 时，从服务端配置兜底取 SESSDATA
        if not cookie:
            cookie = load_config().get("sessdata", "")

        api = BilibiliAPI(cookie) if cookie else BilibiliAPI()
        per_page = 12
        # 类型筛选（TAB 级）：在整个已加载缓冲上过滤，而不是只过滤当前页
        dtype = params.get("dtype", [""])[0]
        if dtype == "全部":
            dtype = ""
        try:
            if not dtype:
                # 首屏预加载 12 条（适度，降低首次请求面降低风控风险）
                _ensure_dyn_buffer(uid, api, max(page * per_page, 12))
                items, has_more, loaded, image_text = _dyn_buffer_snapshot(uid)
                filtered = items
            elif dtype == "联合投稿":
                # 内容关键词分类：标题/正文含「合作视频」或「联合投稿」。
                # 与类型筛选共用「稀少类型自动多补批次」逻辑，自动多补批次寻找；
                # 封顶 12 批（约 144 条）平衡"找全"与风控，避免连环请求触发 -412/-352。
                need = page * per_page
                batches, max_batches = 0, 12
                while True:
                    items, has_more, loaded, image_text = _dyn_buffer_snapshot(uid)
                    filtered = [d for d in items if _is_joint_submission(d)]
                    if len(filtered) >= need or not has_more or batches >= max_batches:
                        break
                    _ensure_dyn_buffer(uid, api, loaded + per_page)  # 补一批
                    batches += 1
            else:
                # 类型筛选：保证筛选结果够填满第 page 页；该类型稀少时自动多补批次寻找，
                # 封顶 12 批（约 144 条）平衡"找全"与风控，避免连环请求触发 -412/-352。
                need = page * per_page
                batches, max_batches = 0, 12
                while True:
                    items, has_more, loaded, image_text = _dyn_buffer_snapshot(uid)
                    filtered = [d for d in items if _dyn_type_label(d) == dtype]
                    if len(filtered) >= need or not has_more or batches >= max_batches:
                        break
                    _ensure_dyn_buffer(uid, api, loaded + per_page)  # 补一批
                    batches += 1
        except Exception as e:
            self._json({"error": f"动态加载失败: {e}"})
            return

        start = (page - 1) * per_page
        page_items = filtered[start:start + per_page]
        # 筛选视图的 has_more：原始缓冲还有更多，或筛选结果还有后续页
        view_has_more = has_more or len(filtered) > start + per_page

        # 标记已下载（与服务端历史比对；前端还会用本地集合再合并）
        for d in page_items:
            d["downloaded"] = is_downloaded(data_dyid=str(d.get("id", "")), bvid=str(d.get("bvid", "")))

        self._json({
            "dynamics": page_items,
            "page": page,
            "per_page": per_page,
            "has_more": view_has_more,
            "loaded": loaded,
            "image_text": image_text,
            "filtered_loaded": len(filtered),          # 当前筛选类型在缓冲里的总条数
            "type_counts": _dyn_type_counts(items),    # 整个缓冲的各类型计数（筛选栏用）
        })

    def _api_download_video(self, data):
        """发起视频下载（进入并发队列）。支持 page 参数只下载指定分P。"""
        bvid = data.get("bvid", "")
        title = data.get("title", bvid)
        username = data.get("username", "未知UP主")
        cookie = data.get("cookie", "")
        page = data.get("page")  # 可选：只下载指定分P（1-based 整数）
        # 前端未传 cookie 时，从服务端配置兜底取 SESSDATA，保证下载用登录态
        if not cookie:
            cookie = load_config().get("sessdata", "")

        if not bvid:
            self._json({"error": "缺少BV号"})
            return

        # 去重：整视频按 bvid；单集按 bvid#P{n}（互不影响）
        if page:
            if is_downloaded(bvid=f"{bvid}#P{page}"):
                self._json({"task_id": "", "already_downloaded": True})
                return
        else:
            if is_downloaded(bvid=bvid):
                self._json({"task_id": "", "already_downloaded": True})
                return

        # 下载前预检 SESSDATA 有效性（过期直接提示，避免白等）
        err = self._preflight_check(cookie)
        if err:
            self._json({"error": err})
            return

        params = {
            "bvid": bvid, "title": title, "username": username,
            "cookie": cookie, "uid": data.get("uid", ""),
            "qn": data.get("qn") or _DEFAULT_QN, "task_type": "video", "page": page or None,
            "dynType": data.get("dynType") or "",
            "source": data.get("source") or "",          # watchlater / favorites（我的来源归档）
            "self_name": data.get("self_name") or "",     # 登录用户名（稍后再看/收藏夹顶层目录）
            "fav_name": data.get("fav_name") or "",       # 收藏夹名字（仅 favorites 来源，用于二级子目录）
        }
        task_id = self._submit_download("video", params)
        self._json({"task_id": task_id})

    def _api_video_pages(self, params):
        """返回某视频的分P列表（前端展示集数 / 逐集下载用）。GET /api/video_pages?bvid=xxx"""
        bvid = (params.get("bvid") or [""])[0]
        if not bvid:
            self._json({"error": "缺少BV号"}, 400)
            return
        cookie = load_config().get("sessdata", "")
        api = BilibiliAPI(cookie) if cookie else BilibiliAPI()
        try:
            data = api.get_video_pages(bvid)
            self._json(data)
        except Exception as e:
            code, reason = _classify_error(e)
            self._json({"error": f"{reason}（{code}）"})

    def _api_video_page_counts(self, params):
        """批量返回多个视频的分P信息（判断哪些是多P，决定是否显示「📂 分P」按钮）。
        GET /api/video_page_counts?bvids=BV1,BV2,BV3
        返回 {"bvids": {bvid: {bvid,title,count,pages}}}，请求里没有/全部失败时 bvids 为空。"""
        raw = (params.get("bvids") or [""])[0]
        bvids = [b.strip() for b in raw.split(",") if b.strip()]
        if not bvids:
            self._json({"bvids": {}})
            return
        cookie = load_config().get("sessdata", "")
        api = BilibiliAPI(cookie) if cookie else BilibiliAPI()
        try:
            data = api.get_video_pages_batch(bvids)
            self._json({"bvids": data})
        except Exception as e:
            code, reason = _classify_error(e)
            self._json({"error": f"{reason}（{code}）", "bvids": {}})

    def _api_download_dynamic(self, data):
        """发起动态下载（进入并发队列）"""
        dynamic = data.get("dynamic", {})
        username = data.get("username", "未知UP主")
        cookie = data.get("cookie", "")
        # 前端未传 cookie 时，从服务端配置兜底取 SESSDATA
        if not cookie:
            cookie = load_config().get("sessdata", "")
        dynamic_id = dynamic.get("id", "unknown")
        page = data.get("page")  # 可选：只下载指定分P

        if not dynamic:
            self._json({"error": "缺少动态数据"})
            return

        # 已下载去重：整视频按 dynamic id / bvid；单集按 id#P{n}（互不影响）
        dyn_hist_id = f"{dynamic_id}#P{page}" if page else dynamic_id
        if is_downloaded(data_dyid=dyn_hist_id):
            self._json({"task_id": "", "already_downloaded": True})
            return
        bvid = dynamic.get("bvid", "")
        vid_hist_id = f"{bvid}#P{page}" if (bvid and page) else bvid
        if bvid and is_downloaded(bvid=vid_hist_id):
            self._json({"task_id": "", "already_downloaded": True})
            return

        # 下载前预检 SESSDATA 有效性（过期直接提示，避免白等）
        err = self._preflight_check(cookie)
        if err:
            self._json({"error": err})
            return

        # 动态类型 → 任务类型标签（用于前端图标/分类）
        dtype = dynamic.get("type", "")
        type_label = {"video": "dynamic_video", "image": "image", "text": "text"}.get(dtype, "dynamic")
        params = {
            "dynamic": dynamic, "username": username, "cookie": cookie,
            "uid": data.get("uid", ""), "qn": data.get("qn") or _DEFAULT_QN,
            "title": dynamic.get("title", dynamic.get("text", "")[:30]),
            "task_type": type_label, "page": page or None,
        }
        task_id = self._submit_download("dynamic", params)
        self._json({"task_id": task_id})

    # ---- 下载调度核心 ----

    def _submit_download(self, kind, params):
        """把一条下载提交进并发队列。返回新建的 task_id。
        params 必须包含重试所需的全部字段，并被写入任务元信息。"""
        task_id = uuid.uuid4().hex[:8]
        title = params.get("title", "")
        upname = params.get("username", "未知UP主")
        ttype = params.get("task_type", kind)
        # 写入完整元信息（含 params 供失败重试）
        update_task(task_id, "queued", 0, "排队中...", title=title,
                    upname=upname, task_type=ttype, params=params)
        get_download_executor().submit(self._run_download, task_id, kind, params)
        return task_id

    def _run_download(self, task_id, kind, params):
        """在 executor 线程中真正执行下载。取消/失败都会被正确标记。"""
        try:
            # 入队后、未启动时就被取消
            if is_cancelled(task_id):
                update_task(task_id, "cancelled", 0, "已取消")
                clear_cancel(task_id)
                return
            # 下载执行前的最后一道去重校验（防并发竞态/重复提交导致的重复落盘）。
            # 此处复用与提交时一致的『互认』逻辑，历史写入发生在下载成功后，
            # 因此第一遍下载写历史前不会误伤；仅拦截真正已存在的资源。
            if kind == "video":
                _bvid = params.get("bvid", "")
                _page = params.get("page")
                _hid = f"{_bvid}#P{_page}" if _page else _bvid
                if _bvid and is_downloaded(bvid=_hid):
                    update_task(task_id, "done", 100, "已完成（资源已下载，已跳过）")
                    return
            else:
                _dyn = params.get("dynamic") or {}
                _dtype = _dyn.get("type", "")
                _did = _dyn.get("id", "")
                _dbvid = _dyn.get("bvid", "")
                if _dtype == "video" and _dbvid:
                    if is_downloaded(data_dyid=str(_did), bvid=str(_dbvid)):
                        update_task(task_id, "done", 100, "已完成（资源已下载，已跳过）")
                        return
                elif _did and is_downloaded(data_dyid=_did):
                    update_task(task_id, "done", 100, "已完成（资源已下载，已跳过）")
                    return
            cookie = params.get("cookie", "")
            cfg = load_config()
            # 画质以全局 config.qn 为准（"下载设置"为唯一权威；自动下载也读同一字段，二者统一）
            qn = int(cfg.get("qn") or _DEFAULT_QN)
            folder_tpl = cfg.get("folder_template") or None
            file_tpl = cfg.get("file_template") or None
            max_dur = int(cfg.get("max_duration", 0) or 0)
            num_threads = max(1, min(8, int(cfg.get("download_threads", 3) or 3)))
            # 缓存中转：默认开启（固定行为，不暴露配置）；cache_root 用系统临时目录
            use_cache = True
            cache_root = None
            api = BilibiliAPI(cookie, proxy=cfg.get("proxy", ""), speed_limit=cfg.get("speed_limit", 0)) if cookie else BilibiliAPI(proxy=cfg.get("proxy", ""), speed_limit=cfg.get("speed_limit", 0))
            if kind == "video":
                bvid = params["bvid"]
                page = params.get("page")
                source = params.get("source") or ""
                if source:
                    # 「我的」来源（稍后再看 / 收藏夹）：归档到 我的用户名/来源/... 下，
                    # 并套用下载设置里的全局 folder_template / file_template 变量。
                    # 结构示例：
                    #   稍后再看  → 用户名/稍后再看/{folder_template}/
                    #   收藏夹    → 用户名/收藏夹/{收藏夹名字}/{folder_template}/
                    my_name = params.get("self_name") or "我"
                    if source == "watchlater":
                        base_dir = os.path.join(get_download_base(), sanitize_filename(my_name), "稍后再看")
                    else:  # favorites
                        fav_name = params.get("fav_name") or "收藏夹"
                        base_dir = os.path.join(
                            get_download_base(), sanitize_filename(my_name), "收藏夹",
                            sanitize_filename(fav_name),
                        )
                    os.makedirs(base_dir, exist_ok=True)
                    api.download_video(bvid, base_dir, task_id, qn=qn,
                                       folder_template=folder_tpl, file_template=file_tpl,
                                       max_duration=max_dur, num_threads=num_threads,
                                       target_page=page, use_stage=use_cache, cache_root=cache_root,
                                       max_pages=params.get("max_pages", 0) or 0)
                else:
                    base_dir = os.path.join(get_download_base(), sanitize_filename(params.get("username", "未知UP主")))
                    os.makedirs(base_dir, exist_ok=True)
                    extra = {"dynType": params["dynType"]} if params.get("dynType") else None
                    api.download_video(bvid, base_dir, task_id, qn=qn,
                                       folder_template=folder_tpl, file_template=file_tpl,
                                       max_duration=max_dur, num_threads=num_threads,
                                       target_page=page, extra_vars=extra,
                                       use_stage=use_cache, cache_root=cache_root,
                                       max_pages=params.get("max_pages", 0) or 0)
                # 历史：整视频记 bvid；单集记 bvid#P{n}（互不影响，单集不会阻止整视频再下）
                hist_id = f"{bvid}#P{page}" if page else bvid
                add_to_history(data_dyid="0", bvid=hist_id, title=params.get("title", ""),
                                up_uid=params.get("uid", ""), up_name=params.get("username", ""))
            else:
                dynamic = params["dynamic"]
                save_dir = os.path.join(get_download_base(), sanitize_filename(params.get("username", "未知UP主")))
                os.makedirs(save_dir, exist_ok=True)
                dtype = dynamic.get("type", "")
                if dtype == "video" and dynamic.get("bvid"):
                    page = params.get("page")
                    api.download_video(dynamic["bvid"], save_dir, task_id, qn=qn,
                                       folder_template=folder_tpl, file_template=file_tpl,
                                       max_duration=max_dur, num_threads=num_threads,
                                       extra_vars={"dynType": dyn_folder_type_from_dynamic(dynamic), "dynamicId": str(dynamic.get("id", ""))},
                                       target_page=page,
                                       use_stage=use_cache, cache_root=cache_root,
                                       max_pages=params.get("max_pages", 0) or 0)
                    # 动态历史：整视频按 dynamic id；单集按 id#P{n}
                    dyn_hist_id = f"{dynamic.get('id')}#P{page}" if page else dynamic.get("id")
                    vid_hist_id = f"{dynamic['bvid']}#P{page}" if page else dynamic["bvid"]
                    add_to_history(data_dyid=dyn_hist_id, bvid=vid_hist_id,
                                    title=dynamic.get("title", "")[:30],
                                    up_uid=params.get("uid", ""), up_name=params.get("username", ""))
                else:
                    api.download_dynamic(dynamic, save_dir, task_id, qn=qn,
                                         folder_template=folder_tpl, file_template=file_tpl,
                                         uid=str(params.get("uid", "")), up_name=params.get("username", ""),
                                         use_stage=use_cache, cache_root=cache_root)
                    add_to_history(data_dyid=dynamic.get("id"), bvid="0",
                                    title=dynamic.get("title", dynamic.get("text", "")[:30]),
                                    up_uid=params.get("uid", ""), up_name=params.get("username", ""))
        except DownloadCancelled:
            update_task(task_id, "cancelled", 0, "已取消")
            clear_cancel(task_id)
        except Exception as e:
            code, reason = _classify_error(e)
            # 可恢复错误码：传输层/连接/超时类错误，外加临时风控/限流类
            # （BILI -352 风控、-412 请求拦截、BILI -509/-799 频繁限制、HTTP 412/HTTP 503）；
            # 这类多为临时异常，稍后重试常能恢复，配合下方指数退避避免对风控接口狂打。
            RETRYABLE_CODES = frozenset({"CURL 7", "CURL 28", "CURL 92", "CURL 18", "CURL 35", "CURL 56", "CURL 6",
                                          "TIMEOUT", "CONN", "UNKNOWN",
                                          "HTTP 500", "HTTP 502", "HTTP 503",
                                          "BILI -352", "HTTP 412", "BILI -509", "BILI -799"})
            MAX_AUTO_RETRIES = 3
            RETRY_DELAYS = [30, 60, 120]  # 秒
            with tasks_lock:
                t = download_tasks.get(task_id, {})
                retry_count = t.get("retry", 0)
            if code in RETRYABLE_CODES and retry_count < MAX_AUTO_RETRIES:
                delay = RETRY_DELAYS[retry_count] if retry_count < len(RETRY_DELAYS) else 120
                retry_count += 1
                update_task(
                    task_id, "retrying", 0,
                    f"下载失败，{delay}秒后自动重试 ({retry_count}/{MAX_AUTO_RETRIES})：{reason}",
                    error_code=code, error_detail=str(e)[:600],
                )
                with tasks_lock:
                    t = download_tasks.get(task_id, {})
                    t["retry"] = retry_count
                    download_tasks[task_id] = t
                # 延迟后回队列（新的 task_id 不变，完全复用 download_tasks 里的状态）
                def _retry(task_id=task_id, kind=kind, params=params):
                    clear_cancel(task_id)
                    update_task(task_id, "queued", 0,
                                f"正在重试 ({retry_count}/{MAX_AUTO_RETRIES})...")
                    try:
                        self._run_download(task_id, kind, params)
                    except Exception:
                        pass  # 已由 _run_download 自身处理
                threading.Timer(delay, _retry).start()
                return
            # 不可恢复或重试耗尽 → 标记为 error
            detail = str(e)[:600]
            if retry_count >= MAX_AUTO_RETRIES:
                detail = f"[已重试{MAX_AUTO_RETRIES}次] " + detail
            update_task(
                task_id, "error", 0,
                f"下载失败：{reason}",
                error_code=code,
                error_detail=detail,
            )
        finally:
            clear_cancel(task_id)
    def _api_status(self):
        """查询所有下载任务的状态（前端轮询这个接口获取进度）"""
        _autoclean_tasks()
        with tasks_lock:
            snapshot = dict(download_tasks)
        self._json({"tasks": snapshot})

    def _api_cancel_task(self, data):
        """取消一条进行中/排队的下载任务。"""
        task_id = data.get("task_id", "")
        with tasks_lock:
            t = download_tasks.get(task_id)
            if not t:
                self._json({"ok": False, "error": "任务不存在"})
                return
            status = t.get("status")
            progress = t.get("progress", 0)
        if status in ("done", "error", "cancelled"):
            self._json({"ok": False, "error": "任务已结束，无法取消"})
            return
        # 标记取消：排队中的任务在 run 开头即退出；下载中的在循环检查点退出
        cancel_task(task_id)
        update_task(task_id, "cancelling", progress, "正在取消...")
        self._json({"ok": True})

    def _task_bvid(self, t):
        """从任务对象提取去重键（video 取 bvid；dynamic 取 bvid 或动态 id）。"""
        params = t.get("params") or {}
        if t.get("type") == "video":
            return params.get("bvid") or ""
        dyn = params.get("dynamic") or {}
        return dyn.get("bvid") or str(dyn.get("id") or "")

    def _api_retry_task(self, data):
        """重试一条失败/已取消的任务（用其保存的 params 重新入队）。"""
        task_id = data.get("task_id", "")
        with tasks_lock:
            t = download_tasks.get(task_id)
            if not t:
                self._json({"ok": False, "error": "任务不存在"})
                return
            params = t.get("params")
            dup_key = self._task_bvid(t) if params else ""
            # 去重：若已有同 bvid/动态 id 的进行中任务，跳过重复提交，避免列表出现重复任务
            if dup_key:
                for tid, ot in list(download_tasks.items()):
                    if tid == task_id:
                        continue
                    if ot.get("status") in ("queued", "downloading", "merging", "cancelling"):
                        if self._task_bvid(ot) == dup_key:
                            self._json({"ok": False, "reason": "active_exists",
                                        "message": f"该视频正在下载中（任务 {tid[:6]}），已跳过重复提交"})
                            return
        if not params:
            self._json({"ok": False, "error": "该任务缺少重试参数"})
            return
        kind = "video" if t.get("type") == "video" else "dynamic"
        # 用保存的 params 重新入队（新建 task_id，旧任务保留为历史）
        new_id = self._submit_download(kind, params)
        self._json({"ok": True, "task_id": new_id})

    def _api_clear_tasks(self, data):
        """清理任务列表。scope: 'finished'=仅已完成(done)；'all'=全部（含失败/取消/进行中）。"""
        scope = data.get("scope", "finished")
        with tasks_lock:
            if scope == "all":
                download_tasks.clear()
                clear_all_cancels()  # 同步清理取消标记集
            else:
                # 「清空已完成」只清真正成功的(done)；失败(error)/取消(cancelled)保留，便于重试/查看
                for tid in list(download_tasks.keys()):
                    if download_tasks[tid].get("status") == "done":
                        download_tasks.pop(tid, None)
        self._json({"ok": True})

    def _api_history(self):
        self._json(load_history())

    def _api_clear_history(self, data=None):
        data = data or {}
        uid = str(data.get("uid") or "").strip()
        raw = _load_history_raw()
        if uid:
            raw = [g for g in raw if g.get("up_uid") != uid]
        else:
            raw = []
        save_history(raw)
        self._json({"ok": True, "uid": uid or None})

    def _api_history_remove(self, data):
        dyid = _norm_val(data.get("dyid") or data.get("id"))
        bvid = _norm_val(data.get("bvid"))
        if not dyid and not bvid:
            self._json({"error": "missing dyid/bvid"}, 400)
            return
        raw = _load_history_raw()
        changed = False
        for grp in raw:
            new_lst = [x for x in grp.get("records", [])
                       if not (dyid and str(x.get("data-dyid", "")) == dyid)
                       and not (bvid and str(x.get("bvid", "")) == bvid)]
            if len(new_lst) != len(grp.get("records", [])):
                grp["records"] = new_lst
                changed = True
        if changed:
            save_history(raw)
        self._json({"ok": True})

    def _api_get_config(self, params):
        config = load_config()
        # 本地部署：返回明文 SESSDATA（本机浏览器即用户本人，无脱敏必要）。
        # 前端 initApp 依赖 data.sessdata 同步 cookie；脱敏会导致 cookie=undefined 连锁故障。
        cfg_out = dict(config)
        cfg_out["has_sessdata"] = bool(cfg_out.get("sessdata"))
        self._json(cfg_out)

    def _api_save_config(self, data):
        config = load_config()
        config.update(data)
        save_config(config)
        # 若涉及 TLS 校验开关，立即同步到运行中的 API 客户端
        if "insecure_tls" in data:
            bilibili.VERIFY_SSL = not config.get("insecure_tls", True)
        # 如果保存的配置涉及定时检查，重建调度器
        if "auto_interval" in data or "auto_schedule_enabled" in data:
            stop_auto_scheduler()
            start_auto_scheduler()
        self._json({"ok": True})

    def _api_auto_check(self):
        """手动触发一次检查（后台线程执行，不阻塞响应）"""
        threading.Thread(target=lambda: _do_auto_check(label="手动检查"), daemon=True).start()
        self._json({"ok": True})

    def _api_auto_status(self, data):
        """查询监控UP主的最新内容状态。

        复用动态缓冲(_ensure_dyn_buffer / _DYN_STATE，TTL 5 分钟)，与手动检查/动态页共享，
        前端每切到监控页不会狂打接口（同一 UP 5 分钟内只打一次真实请求）。
        """
        uids_data = data.get("uids") or load_config().get("auto_uids") or []
        cookie = load_config().get("sessdata", "")
        types = load_config().get("download_types") if "download_types" in load_config() else ["投稿视频", "动态视频", "图文", "文字", "转发", "联合投稿"]
        api = BilibiliAPI(cookie) if cookie else BilibiliAPI()
        result = {}
        for u in uids_data:
            uid = u.get("uid", u) if isinstance(u, dict) else str(u)
            if not uid: continue
            try:
                # 仅预拉首批（与自动化下载取最新内容的视角一致），命中缓冲则不打接口
                _ensure_dyn_buffer(uid, api, 12)
                items, _, _, _ = _dyn_buffer_snapshot(uid)
                new = []
                for d in (items or []):
                    if d.get("charge_only"):
                        label = "充电专属"
                    elif _is_joint_submission(d):
                        label = "联合投稿"
                    else:
                        label = {"video": "动态视频", "image": "图文", "text": "文字"}.get(d.get("type", ""), "转发")
                    if label not in types:
                        continue
                    if is_downloaded(bvid=d.get("bvid", "")) or is_downloaded(data_dyid=str(d.get("id", ""))):
                        continue
                    new.append(d)
                result[uid] = {"has_new": len(new) > 0, "count": len(new)}
            except Exception as e:
                result[uid] = {"has_new": False, "count": 0, "error": str(e)}
        self._json(result)

    def _api_auto_log(self, params):
        """返回 after 之后的新日志，供前端实时轮询展示自动化下载进度"""
        try:
            after = int(params.get("after", ["0"])[0])
        except Exception:
            after = 0
        with _auto_log_lock:
            logs = [x for x in _auto_log if x["id"] > after]
            last_id = _auto_log[-1]["id"] if _auto_log else 0
        running = _auto_busy
        self._json({"logs": logs, "last_id": last_id, "running": running})

    def _api_logs_download(self):
        """下载完整日志文件"""
        log_path = os.path.join(LOGS_DIR, "server.log")
        if not os.path.isfile(log_path):
            self._json({"error": "日志文件不存在"}, 404)
            return
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="bili-dl-log-{time.strftime("%Y%m%d")}.log"')
        self.send_header("Content-Length", str(len(content.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _api_check_cookie(self, src):
        """验证 SESSDATA 是否有效，返回登录状态（供前端显示 + 下载前预检）"""
        # 兼容 GET(parse_qs, 值为列表) 和 POST(dict)
        if isinstance(src, dict):
            cookie = src.get("cookie", "") or src.get("sessdata", "")
        else:
            cookie = src.get("cookie", [""])[0] if src.get("cookie") else ""
        # 若未显式传入，尝试从服务端配置读取
        if not cookie:
            cookie = load_config().get("sessdata", "")
        if not cookie:
            self._json({"login": False, "code": -101, "msg": "未设置 SESSDATA"})
            return
        try:
            api = BilibiliAPI(cookie)
            result = api.check_login()
        except Exception as e:
            result = {"login": False, "code": -1, "msg": str(e)}
        # 本地部署：回传明文 SESSDATA（前端 verifyCookie 依赖 d.sessdata 同步 cookie 到 localStorage；
        # 仅返回布尔会导致 cookie=undefined 连锁故障）。同时保留 has_sessdata 兼容旧前端。
        result["has_sessdata"] = bool(cookie)
        result["sessdata"] = cookie
        self._json(result)

    def _api_self(self, params):
        """返回当前登录用户的信息（头像/昵称/UID/等级），供前端『我的』栏展示。"""
        cookie = params.get("cookie", [""])[0] or load_config().get("sessdata", "")
        if not cookie:
            self._json({"login": False})
            return
        try:
            info = BilibiliAPI(cookie).get_self_info()
        except Exception as e:
            self._json({"login": False, "error": str(e)})
            return
        self._json(info)

    def _api_watchlater(self, params):
        """返回登录用户的『稍后再看』列表。"""
        cookie = params.get("cookie", [""])[0] or load_config().get("sessdata", "")
        if not cookie:
            self._json({"error": "未设置 SESSDATA，无法获取稍后再看", "code": -101})
            return
        try:
            items = BilibiliAPI(cookie).get_watch_later()
        except Exception as e:
            self._json({"error": str(e)})
            return
        self._json({"items": items, "count": len(items)})

    def _api_favorites(self, params):
        """返回登录用户创建的收藏夹列表（每个收藏夹含首页资源）。"""
        cookie = params.get("cookie", [""])[0] or load_config().get("sessdata", "")
        if not cookie:
            self._json({"error": "未设置 SESSDATA，无法获取收藏夹", "code": -101})
            return
        try:
            folders = BilibiliAPI(cookie).get_favorites()
        except Exception as e:
            self._json({"error": str(e)})
            return
        self._json({"folders": folders})

    # ---- 扫码登录（二维码）----

    def _api_qr_generate(self):
        """生成登录二维码：返回 base64 PNG 图片 + 票据 key。
        若 qrcode 库缺失，返回 qr_unavailable 提示前端改用 Cookie 输入。
        """
        if not _QR_AVAILABLE:
            self._json({"qr_unavailable": True,
                        "error": "后端未安装 qrcode 库，无法生成二维码，请使用 Cookie 登录"})
            return
        try:
            api = BilibiliAPI()
            gen = api.qr_generate()
            qr = qrcode.QRCode(box_size=8, border=2,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(gen["url"])
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            with _qr_lock:
                _qr_state["qrcode_key"] = gen["qrcode_key"]
                _qr_state["image"] = b64
                _qr_state["expires_at"] = time.time() + QR_TTL
                _qr_state["last_status"] = "scanning"
                _qr_state["last_message"] = "等待扫码"
            self._json({"ok": True, "qrcode_key": gen["qrcode_key"],
                        "image": b64, "expires_in": QR_TTL})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    def _api_qr_poll(self, data):
        """轮询扫码状态。前端传入 qrcode_key（或直接用服务端缓存的）。
        成功时把完整 cookie 同步进配置（sessdata 取 SESSDATA 值），并回传登录信息。
        """
        key = (data or {}).get("qrcode_key") or _qr_state.get("qrcode_key")
        if not key:
            self._json({"ok": False, "error": "请先生成二维码"})
            return
        try:
            api = BilibiliAPI()
            res = api.qr_poll(key)
        except Exception as e:
            self._json({"ok": False, "error": str(e)})
            return

        with _qr_lock:
            _qr_state["last_status"] = res["status"]
            _qr_state["last_message"] = res.get("message", "")

        if res["status"] == "success" and res.get("cookie"):
            cookie = res["cookie"]
            # 解析 SESSDATA 值（兼容将来多字段并存），写入 config.sessdata
            sess = ""
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("SESSDATA="):
                    sess = part[len("SESSDATA="):]
                    break
            cfg = load_config()
            if not sess:
                # 理论上不会走到这里（qr_poll 已校验 cookie 含 SESSDATA）
                self._json({"ok": False, "error": "登录成功但未取到 SESSDATA，登录态无效"})
                return
            # sessdata 字段只存 SESSDATA 的值本身（不含 "SESSDATA=" 前缀），
            # 其它接口自行拼 "SESSDATA=" + 值；不再兜底存整串，避免混入其它字段。
            cfg["sessdata"] = sess
            save_config(cfg)
            bilibili.VERIFY_SSL = not cfg.get("insecure_tls", True)
            # 用新 cookie 取登录信息，回传前端
            info = BilibiliAPI(cookie).get_self_info() if cookie else {"login": False}
            self._json({"ok": True, "status": "success", "cookie": cookie,
                        "sessdata": sess, "self": info})
            return

        # 未成功：回传当前状态（含失效/待确认/未扫码）
        self._json({"ok": True, "status": res["status"],
                    "message": res.get("message", ""),
                    "code": res.get("code")})

    def _api_qr_status(self):
        """查询当前二维码的剩余有效期与最近状态（供前端刷新倒计时/过期判定）。"""
        with _qr_lock:
            expires = _qr_state.get("expires_at", 0)
            remain = max(0, int(expires - time.time()))
            self._json({
                "ok": True,
                "has_qr": bool(_qr_state.get("qrcode_key")),
                "expires_in": remain,
                "status": _qr_state.get("last_status"),
            })

    def _preflight_check(self, cookie):
        """下载前预检 SESSDATA 有效性。
        返回错误提示字符串（应直接拦截下载）；返回 None 表示可继续。
        仅当明确判定为『未登录/过期』(-101 或 nav 成功但 isLogin=False) 才拦截，
        风控类错误(-352/-412 等)不拦截，留给实际下载流程处理。
        """
        if not cookie:
            return None
        try:
            st = BilibiliAPI(cookie).check_login()
            if not st.get("login") and st.get("code") in (-101, 0):
                return (f"SESSDATA 已失效或过期（{st.get('msg', '')}），"
                        f"请在右上角『Cookie设置』重新填入有效的 SESSDATA 后再下载。")
        except Exception:
            pass
        return None

    # ---- 日志（静默处理，不刷屏）----

    def log_message(self, *args):
        pass


# ========================================================
# 启动
# ========================================================

def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    # 根据配置初始化 TLS 校验开关（默认 insecure_tls=true → 不校验，兼容代理/抓包）
    bilibili.VERIFY_SSL = not load_config().get("insecure_tls", True)
    # 监听地址：默认仅本机 127.0.0.1（安全，不对外暴露）
    host = load_config().get("host") or "127.0.0.1"
    server = ThreadingHTTPServer((host, PORT), Handler)
    print("=" * 50)
    print("  B站下载器 已启动！")
    print(f"  浏览器打开: http://localhost:{PORT}")
    print(f"  下载目录:   {get_download_base()}")
    print("  按 Ctrl+C 停止服务器")
    print("=" * 50)
    # 启动定时检查调度器（若配置启用）
    start_auto_scheduler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")
        stop_auto_scheduler()
        server.shutdown()


if __name__ == "__main__":
    main()
