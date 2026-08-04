"""
bilibili.py - B站 API 封装 + 下载逻辑
=====================================
这个文件负责和B站服务器通信，包括：
  1. WBI 签名（B站 API 的安全验证，不签名会报错）
  2. 获取UP主信息、视频列表、动态列表
  3. 下载视频（DASH格式，用FFmpeg合并音视频）
  4. 下载图文动态（图片+文字）
"""

import os
import re
import time
import uuid
import hashlib
import logging
import threading
import subprocess
import urllib.parse
import tempfile
import shutil

from curl_cffi import requests, CurlOpt

# ========================================================
# 全局状态
# ========================================================

# TLS 证书校验开关：默认 False（不校验，兼容代理/抓包/部分网络环境）。
# 在不受信任的网络上建议置为 True（通过 server 配置 insecure_tls=false 控制）。
VERIFY_SSL = False

# 模块级 logger（与 server.py 的 "bili-dl" 同名，日志统一进 logs/server.log）
_logger = logging.getLogger("bili-dl")

# 所有下载任务的状态存在这里，前端可以轮询查询进度
download_tasks = {}
# 保护 download_tasks 的读写锁：下载线程池/Web 线程/自动下载线程/重试定时器
# 都会访问该字典，复合操作（遍历+pop、判阈值+清理、快照）必须持锁，避免
# "dictionary changed size during iteration" 与并发写覆盖。
tasks_lock = threading.Lock()

# WBI 签名密钥（缓存，1小时刷新一次）
_wbi_keys = None
_wbi_keys_time = 0
_wbi_lock = threading.Lock()

# 分P信息缓存：{bvid: (timestamp, data)}，减少重复请求与被风控风险
_PAGES_CACHE = {}

# 浏览器伪装头（B站会检查这些，缺了就拒绝请求）
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

# WBI 混淆表 —— B站前端源码里的一组固定数字，用于打乱密钥顺序
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 8, 19, 13, 49, 28, 10, 44, 17, 51, 46,
    12, 27, 1, 32, 31, 23, 36, 27, 12, 24, 27, 41, 54, 7, 4, 25,
    35, 28, 18, 42, 15, 0, 6, 29, 54, 28, 23, 45, 3, 1, 44, 25,
]


# ========================================================
# 辅助函数
# ========================================================

# 项目内置 FFmpeg 路径（已集成到 项目/ffmpeg/ffmpeg.exe，不再依赖外部路径）
_FFMPEG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "ffmpeg.exe")


def find_ffmpeg():
    """返回 FFmpeg 可执行文件路径。

    优先使用项目内置的 ffmpeg/ffmpeg.exe（Windows 随项目分发）；
    非 Windows 或内置文件缺失时，回退到系统 PATH 中的 ffmpeg。
    两者皆无则返回 None。
    """
    if os.path.isfile(_FFMPEG_PATH):
        return _FFMPEG_PATH
    # 跨平台回退：Linux/macOS 等环境使用系统 ffmpeg
    sys_ff = shutil.which("ffmpeg")
    return sys_ff or None


# Windows 保留设备名（用作文件名/目录名会直接触发 [WinError 123]）
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def _sort_cdn_urls(urls):
    """按 CDN 节点可达性排序候选下载地址。

    B站 playurl 每个流会返回多个候选地址（baseUrl + backupUrl）。
    - `upos-*` 节点走标准端口(80/443)，家庭网络/防火墙通常放行 -> 优先；
    - `mcdn*` 节点走非标准端口(8082/8084)，家庭网络常被拦截 -> 垫后。
    这样遇到 mcdn 连不上的情况时，会优先用 upos 源，连不上再回退到 mcdn。
    """
    def score(u):
        try:
            host = u.split("//", 1)[1].split("/", 1)[0].lower()
        except Exception:
            host = u.lower()
        if host.startswith("upos"):
            return 0
        if "mcdn" in host:
            return 2
        return 1
    return sorted(urls, key=score)


def _is_conn_error(e):
    """判断是否为网络层连接/超时类错误（可换源重试）；
    鉴权/风控/文件类错误则不重试直接抛出。"""
    msg = str(e)
    patterns = (
        "curl: (7)", "curl: (28)", "Could not connect", "Connection refused",
        "ConnectionError", "ConnectTimeout", "ReadTimeout", "timed out",
        "Failed to connect",
        # HTTP/2 流被服务端/代理中途重置，以及其它传输层错误（间歇性，可重试）
        "curl: (92)", "INTERNAL_ERROR", "HTTP/2",
        "curl: (18)", "curl: (56)", "connection reset", "reset by peer",
    )
    return any(p in msg for p in patterns)


def _classify_error(e):
    """把下载异常归类成 (error_code, friendly_reason)。
    error_code: 简短错误码，用于前端徽章展示（如 CURL 7 / HTTP 403 / WIN 123 / BILI -352）。
    friendly_reason: 给用户看的中文原因（不含整坨堆栈/URL）。
    原始异常文本由调用方存到 task['error_detail'] 供 hover 查看。
    """
    msg = str(e)
    # 1) curl 传输错误码：curl: (N)
    m = re.search(r"curl:\s*\((\d+)\)", msg)
    if m:
        n = m.group(1)
        cur_map = {
            "7": "下载源连接被拒绝（CDN 节点不可达，多为 mcdn:8082 被网络/防火墙拦截）",
            "28": "连接超时（网络较慢或 CDN 节点响应慢）",
            "92": "HTTP/2 流被重置（多线程分段下载时服务端中断）",
            "18": "传输被提前关闭（文件不完整）",
            "35": "SSL/TLS 握手失败",
            "56": "接收数据失败（连接被对端重置）",
            "6": "无法解析域名（DNS 失败）",
        }
        reason = cur_map.get(n, f"curl 传输错误 ({n})")
        return f"CURL {n}", reason
    # 2) Windows 系统错误码：[WinError NNN]
    m = re.search(r"\[WinError\s*(\d+)\]", msg)
    if m:
        n = m.group(1)
        win_map = {
            "123": "文件名/目录名含非法字符（如换行、/、: 等）",
            "2": "文件或目录不存在（路径错误或并发写冲突）",
            "32": "文件被占用（可能被其它程序打开）",
            "5": "拒绝访问（权限不足）",
        }
        reason = win_map.get(n, f"Windows 系统错误 ({n})")
        return f"WIN {n}", reason
    # 3) HTTP 状态码：HTTP 403 / 403 Forbidden / HTTPError: 403 / status=403
    m = re.search(r"(?:HTTP|HTTPError)[^\d]*(\d{3})", msg) or re.search(r"status[=:\s]+(\d{3})", msg)
    if m:
        n = m.group(1)
        http_map = {
            "403": "无权限：视频需大会员 / SESSDATA 失效 / 区域限制",
            "404": "资源不存在（视频可能已下架或地址失效）",
            "412": "请求被 B站 拦截（风控 / 请求过于频繁）",
            "452": "版权限制，禁止下载",
            "480": "未登录或登录态失效",
            "500": "B站 服务器内部错误，请稍后重试",
            "502": "网关错误（CDN 节点异常）",
            "503": "服务不可用（CDN 限流）",
        }
        reason = http_map.get(n, f"HTTP 错误 ({n})")
        return f"HTTP {n}", reason
    # 4) B站 业务码：{"code": -352} / code: -352 / -352 等
    m = re.search(r"code[\"'\s:=:]+(-?\d+)", msg)
    if m:
        n = m.group(1)
        bili_map = {
            "-101": "未登录（SESSDATA 缺失或过期）",
            "-352": "风控拦截（请求被 B站 判定为异常）",
            "-403": "访问被拒绝（可能需要大会员 / 区域限制）",
            "-404": "资源不存在",
            "-509": "请求过于频繁，请稍后重试",
            "-799": "请求过于频繁，请稍后重试",
        }
        if n in bili_map:
            return f"BILI {n}", bili_map[n]
    # 5) 已知关键字兜底
    low = msg.lower()
    if "timed out" in low or "timeout" in low:
        return "TIMEOUT", "请求超时（网络较慢或 CDN 不稳定）"
    if "could not connect" in low or "connection refused" in low:
        return "CONN", "连接被拒绝（CDN 节点不可达）"
    if "no such file" in low or "视频流文件不存在" in msg:
        return "FS", "文件/路径不存在（并发写冲突或路径非法）"
    if "ffmpeg" in low:
        return "FFMPEG", "音视频合并失败（FFmpeg 报错）"
    if "命名模板渲染失败" in msg:
        return "TEMPLATE", "命名模板渲染失败（模板语法错误）"
    if "超过设置上限" in msg or "所有分P均超过" in msg:
        return "SKIP_DUR", "已跳过：超过时长上限设置"
    if "大会员" in msg or "付费" in msg:
        return "PAY", "视频需大会员 / 付费专享"
    # 6) 业务流程类文案（download_video / download_dynamic 抛出的已知业务异常）
    if "缺少BV号" in msg:
        return "NOBVID", "视频动态缺少BV号（动态数据异常，无法下载）"
    if "没有可用的视频流" in msg:
        return "NOSTREAM", "没有可用的视频流（可能需要登录或视频受限）"
    if "没有可下载的分P" in msg:
        return "NOPAGE", "没有可下载的分P"
    if "未找到第" in msg and "集" in msg:
        return "NOPAGE", "指定的分P不存在（页码超出范围）"
    if "未找到前" in msg and "集内容" in msg:
        return "NOPAGE", "max_pages 上限内无可下载内容"
    if "暂不支持下载此类型动态" in msg:
        return "BADTYPE", "暂不支持下载此类型动态"
    if "无法获取下载地址" in msg:
        return "NOURl", "无法获取下载地址（可能需要登录B站）"
    return "UNKNOWN", "未知下载错误"


def sanitize_filename(name):
    """清理文件名/目录名，去掉 Windows 不允许的字符，避免 [WinError 123]。

    处理范围：
    - 控制字符 0x00-0x1F：含换行 \\n、回车 \\r、制表符 \\t、空字符等。
      这些是不可见字符，但 Windows 路径一律拒绝（图文/动态标题常带 \\n）。
    - 文件名非法字符（半角）：\\ / : * ? " < > |
    - 全角标点（网盘常拒、Windows 允许）：： ？ ＊ ＂ ＇ ＜ ＞ ｜ ＼ ／
    - 额外符号（OneDrive 等会拒）：# % &
    - 全角空格（　）与不间断空格（\\xa0）：部分同步盘按空白处理会异常
    - 每个路径段尾部的 '.' 和空格（Windows 不允许名称以 . 或空格结尾）
    - 保留设备名（CON/PRN/AUX/NUL/COM1..9/LPT1..9）加后缀避免冲突
    """
    name = str(name)
    # 1) 控制字符 + 全角空格/不间断空格 -> 下划线
    name = re.sub(r"[\x00-\x1f\u3000\xa0]", "_", name)
    # 2) 文件名非法字符（半角 + 全角标点 + #%&）-> 下划线
    name = re.sub(r'[\\/:*?"<>|#%&：？＊＂＇＜＞｜＼／]', "_", name)
    # 3) 逐路径段清理尾部 '.'/空格，并防护保留设备名
    out_parts = []
    for p in name.split("/"):
        p = p.rstrip(". ").strip()
        if not p:
            p = "_"
        base = p.split(".")[0].upper()
        if base in _RESERVED_NAMES:
            p = p + "_"
        out_parts.append(p)
    name = "/".join(out_parts)
    return name.strip("/")[:100]  # 限制长度，避免路径太长


# 画质代码 → 标签
QN_LABEL = {127: "最高画质", 120: "4K", 116: "1080P60", 112: "1080P+",
            80: "1080P", 64: "720P", 32: "480P", 16: "360P"}


def _resolve_quality_label(play_data, chosen_id):
    """把清晰度 id 解析成可读标签：优先用 B站官方描述(accept_description)，
    其次 QN_LABEL，最后 {id}P 兜底。保证绝不返回空字符串，避免前端画质标签缺失。"""
    if chosen_id:
        _pd = play_data or {}
        aq = _pd.get("accept_quality") or []
        ad = _pd.get("accept_description") or []
        try:
            idx = aq.index(chosen_id)
            if 0 <= idx < len(ad) and ad[idx]:
                return str(ad[idx])
        except Exception:
            pass
        if chosen_id in QN_LABEL:
            return QN_LABEL[chosen_id]
        return f"{chosen_id}P"
    return ""


def _qn_label_from_play(play_data):
    """从播放地址数据里提取清晰度标签（用于任务面板显示）。"""
    if "dash" in play_data:
        _videos = play_data["dash"].get("video", [])
        if _videos:
            _vid = max(_videos, key=lambda x: x.get("id", 0))
            return _resolve_quality_label(play_data, _vid.get("id", 0))
    elif "durl" in play_data:
        return "480P"
    return ""


def update_task(task_id, status, progress, message, quality=None,
                title=None, upname=None, task_type=None, params=None,
                error_code=None, error_detail=None, file_path=None):
    """更新下载任务状态，前端通过轮询 /api/status 来获取这些信息。
    所有扩展字段（quality/title/upname/task_type/params/error_code/error_detail/file_path）为
    None 时保留原有值（保留式更新），让下载全过程不丢失元信息。"""
    if task_id:
        with tasks_lock:
            t = download_tasks.get(task_id, {})
            t["status"] = status
            t["progress"] = progress
            t["message"] = message
            t["time"] = time.time()
            if quality is not None:
                t["quality"] = quality
            if error_code is not None:
                t["error_code"] = error_code
            if error_detail is not None:
                t["error_detail"] = error_detail
            if title is not None:
                t["title"] = title
            if upname is not None:
                t["upname"] = upname
            if task_type is not None:
                t["type"] = task_type
            if params is not None:
                t["params"] = params
            if file_path is not None:
                t["file_path"] = file_path
            download_tasks[task_id] = t


# ========================================================
# 下载取消机制
#   服务端 /api/download/cancel 往 _cancel_tasks 标记；
#   下载循环定期检查，一旦命中即抛 DownloadCancelled 中断。
# ========================================================
_cancel_tasks = set()


class DownloadCancelled(Exception):
    """下载被用户取消时抛出，由 server.run 捕获后标记为 cancelled。"""
    pass


def cancel_task(task_id):
    if task_id:
        _cancel_tasks.add(task_id)


def is_cancelled(task_id):
    return bool(task_id) and task_id in _cancel_tasks


def clear_cancel(task_id):
    if task_id:
        _cancel_tasks.discard(task_id)


def clear_all_cancels():
    """清空全部取消标记（与 download_tasks.clear() 同步清理）"""
    _cancel_tasks.clear()


def render_template(template, variables):
    """渲染下载命名模板。支持：
    - 裸变量名：avTitle, UpName, bvid, qn, pubdate, pubtime, dynamicId, dynType
    - 条件替换：(:变量名 默认值) —— 变量为空时用默认值（默认值可含/，如 视频/:avTitle）
    - / 或 \\ 作为子文件夹分隔符
    示例：UpName/avTitle → {UpName}/{avTitle}.mp4
    空模板 → 默认「视频/avTitle」
    """
    import re as _re
    if not template or not template.strip():
        template = "视频/avTitle"
    # 变量值（来自B站标题/bvid/UP名等）里可能含 Windows 文件名非法字符：
    # 例如标题「【(G)I-DLE】[M/V] - 'TOMBOY'」里的 "/" 会被误导成子文件夹
    # 分隔符造成多建目录；标题里的 ":" "*" "?" 等虽不造目录，但进入最终文件名
    # 会让 Windows 拒绝创建文件(WinError 123)。故替换进模板前先把值里的这些
    # 字符都换成 "_" —— 与 BilibiliDown 思路一致：变量值层清理完整非法集。
    # 清理范围对齐 sanitize_filename：控制字符 + 全角空格/不间断空格 + 半角非法集
    # + 全角标点（：？＊＂＇＜＞｜＼／）+ # % &（网盘安全命名，避免上传失败）。
    # 模板字面量里的 / \（用户设计的子文件夹结构）不受影响，仍作分隔符。
    def _safe_val(v):
        s = str(v) if v is not None else ""
        s = _re.sub(r"[\x00-\x1f\u3000\xa0]", "_", s)
        s = _re.sub(r'[\\/:*?"<>|#%&：？＊＂＇＜＞｜＼／]', "_", s)
        # 方案二：命名模板变量层把 emoji 替换为字面符号 [emoji]
        # （仅影响进入文件名/目录名的变量值；用户名/收藏夹名目录前缀走 sanitize_filename，不受影响）
        # 注意：❤️ 由 U+2764 + U+FE0F 两个码点组成，必须「基础 emoji + 跟随修饰符(VS16/ZWJ/肤色)」
        # 整体匹配一次替换为一个 [emoji]，否则变体选择符会被当成第二个 emoji
        s = _re.sub(
            r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF][\uFE0F\u200D\U0001F3FB-\U0001F3FF]*",
            "[emoji]", s)
        # 清理孤立的变体选择符 / 零宽连接符（前面没有基础 emoji、单独出现的情况）
        s = _re.sub(r"[\uFE0F\u200D]", "", s)
        return s
    # 单遍 re.sub + 单词边界，避免旧循环 replace 导致标题中的变量子串被二次替换
    _token_re = _re.compile(
        r'\(:(\w+)\s+(.+?)\)'                                       # 条件替换 (:var 默认)
        r'|(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])'  # 裸变量名(单词边界)
    )
    def _repl(m):
        if m.group(1):  # 条件替换分支
            var = m.group(1).strip()
            default = m.group(2).strip()
            val = variables.get(var, "")
            # 默认值里的 / 保留（作者设计的子文件夹结构），仅变量实际值做清理
            return _safe_val(val) if val else str(default)
        name = m.group(3)  # 裸变量名分支
        if name in variables:
            return _safe_val(variables[name])
        return name  # 未识别的标识符原样保留
    template = _token_re.sub(_repl, template)
    # 标准化路径分隔符
    template = template.replace("\\", "/").strip("/")
    # 清理每段路径中的非法文件名字符
    parts = template.split("/")
    safe = [sanitize_filename(p) for p in parts if p.strip()]
    return "/".join(safe) if safe else sanitize_filename(variables.get("avTitle", "video"))


# ========================================================
# BilibiliAPI —— 核心类
# ========================================================

class BilibiliAPI:
    """B站 API 封装类。每个实例可以带不同的 cookie（SESSDATA）。"""

    def __init__(self, sessdata="", proxy="", speed_limit=0):
        """
        sessdata: B站 SESSDATA cookie 值
        proxy: HTTP 代理地址，如 http://127.0.0.1:7890
        speed_limit: 下载限速（KB/s），0=不限速；限速时自动降为单线程
        """
        if sessdata:
            sessdata = urllib.parse.unquote(sessdata)
        self.sessdata = sessdata
        self.speed_limit = max(0, int(speed_limit or 0))  # KB/s
        # 用 curl_cffi 模拟 Chrome 浏览器 TLS 指纹
        self.session = requests.Session(impersonate="chrome", verify=VERIFY_SSL,
                                          curl_options={CurlOpt.HTTP_VERSION: 2})
        self.session.headers.update(DEFAULT_HEADERS)
        # 代理设置（如配置了 proxy 则生效）
        if proxy and proxy.strip():
            self.session.proxies = {"http": proxy.strip(), "https": proxy.strip()}
        if sessdata:
            self.session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
        self._init_fingerprint_cookies()
        if sessdata:
            self._init_login_cookies()

    def _init_fingerprint_cookies(self):
        """获取 buvid3/buvid4 等浏览器指纹 cookie，绕过B站风控"""
        try:
            resp = self.session.get(
                "https://api.bilibili.com/x/frontend/finger/spi", timeout=10, verify=VERIFY_SSL
            )
            data = resp.json()
            if data.get("code") == 0 and data.get("data"):
                self.session.cookies.set("buvid3", data["data"]["b_3"], domain=".bilibili.com")
                self.session.cookies.set("buvid4", data["data"]["b_4"], domain=".bilibili.com")
        except Exception:
            pass  # 获取失败不阻塞，后续请求可能仍能工作
        # 生成其他辅助 cookie
        self.session.cookies.set("b_lsid", uuid.uuid4().hex[:24].upper(), domain=".bilibili.com")
        self.session.cookies.set("_uuid", str(uuid.uuid4()), domain=".bilibili.com")

    def _init_login_cookies(self):
        """从 nav 接口提取完整登录 Cookie（DedeUserID 等），提高API成功率"""
        try:
            resp = self.session.get(
                "https://api.bilibili.com/x/web-interface/nav", timeout=10, verify=VERIFY_SSL
            )
            data = resp.json()
            if data.get("code") == 0 and data.get("data"):
                d = data["data"]
                mid = str(d.get("mid", ""))
                if mid:
                    self.session.cookies.set("DedeUserID", mid, domain=".bilibili.com")
                    self.session.cookies.set("DedeUserID__ckMd5", d.get("mid_md5", ""), domain=".bilibili.com")
                    self.session.cookies.set("sid", mid, domain=".bilibili.com")
                # 刷新WBI密钥缓存（持锁，与 _get_wbi_keys 互斥）
                global _wbi_keys, _wbi_keys_time
                if d.get("wbi_img"):
                    wbi = d["wbi_img"]
                    img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
                    sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
                    with _wbi_lock:
                        _wbi_keys = (img_key, sub_key)
                        _wbi_keys_time = time.time()
        except Exception:
            pass  # 失败不阻塞

    # -------------------- WBI 签名 --------------------

    def _get_wbi_keys(self):
        """从B站获取 WBI 签名密钥（缓存1小时，不用每次请求都拿）"""
        global _wbi_keys, _wbi_keys_time
        with _wbi_lock:
            now = time.time()
            if _wbi_keys is not None and now - _wbi_keys_time < 3600:
                return _wbi_keys
            try:
                resp = self.session.get(
                    "https://api.bilibili.com/x/web-interface/nav", timeout=10, verify=VERIFY_SSL
                )
                # 检查是否返回 JSON
                ct = resp.headers.get("content-type", "")
                if "json" not in ct or resp.status_code != 200:
                    raise Exception(f"Nav API 返回异常 (status={resp.status_code})")
                data = resp.json()
                wbi = data["data"]["wbi_img"]
                # img_url 形如 https://.../abc123.png，取最后一段的文件名部分
                img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
                sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
                _wbi_keys = (img_key, sub_key)
                _wbi_keys_time = now
                return _wbi_keys
            except Exception:
                # Nav API 失败，尝试不用 WBI 的方式
                # 返回空键，_sign_wbi 会跳过签名
                _wbi_keys = ("", "")
                _wbi_keys_time = now
                return _wbi_keys

    def _sign_wbi(self, params):
        """对请求参数做 WBI 签名（很多B站API必须签名才能用）"""
        img_key, sub_key = self._get_wbi_keys()
        if not img_key or not sub_key:
            # WBI 密钥获取失败，返回带时间戳但不签名的参数
            params = dict(params)
            params["wts"] = round(time.time())
            return params
        # 把两个 key 拼起来，用混淆表重排，取前32位
        raw = img_key + sub_key
        mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        # 加上当前时间戳
        params = dict(params)
        params["wts"] = round(time.time())
        # 按参数名排序
        params = dict(sorted(params.items()))
        # 过滤特殊字符（B站要求）
        filtered = {}
        for k, v in params.items():
            filtered[k] = re.sub(r"[ !'()*]", "", str(v))
        # 计算 w_rid（签名值）
        query = urllib.parse.urlencode(filtered)
        w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
        filtered["w_rid"] = w_rid
        return filtered

    @staticmethod
    def _friendly_error(data, action):
        """把B站错误码翻译成用户能看懂的提示"""
        code = data.get("code", -1)
        msg = data.get("message", "未知错误")
        hints = {
            -352: "（B站风控拦截，请设置Cookie或在浏览器中操作）",
            -403: "（权限不足，请设置Cookie后重试）",
            -412: "（请求被拦截，请稍后再试或设置Cookie）",
            -799: "（请求过于频繁，请稍后再试）",
            -101: "（账号未登录，请在右上角设置Cookie）",
        }
        hint = hints.get(code, "")
        return f"{action}失败: {msg} {hint}"

    def _api_get(self, url, params=None, extra_headers=None):
        """安全地发起 GET 请求并解析 JSON"""
        # 合并默认头和额外头（curl_cffi 不会自动合并 session headers 和传入的 headers）
        headers = dict(DEFAULT_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        resp = self.session.get(url, params=params, headers=headers, timeout=15, verify=VERIFY_SSL)
        ct = resp.headers.get("content-type", "")
        if "json" not in ct or resp.status_code != 200:
            raise Exception(f"服务器返回异常 (status={resp.status_code})，可能是请求过于频繁")
        return resp.json()

    # -------------------- 解析用户输入 --------------------

    @staticmethod
    def parse_uid(query):
        """从用户输入中提取 UID。支持纯数字和主页链接两种格式。"""
        query = str(query).strip()
        if query.isdigit():
            return query
        # 从各种 URL 格式中提取 UID
        for pattern in [
            r"space\.bilibili\.com/(\d+)",
            r"bilibili\.com/space/(\d+)",
            r"uid[=:](\d+)",
        ]:
            m = re.search(pattern, query)
            if m:
                return m.group(1)
        return None

    # -------------------- API 调用 --------------------

    def get_user_info(self, uid):
        """获取UP主信息：名字、头像、签名、等级、投稿数量等
        B站近期对 /x/space/wbi/acc/info 加了登录限制(-403)，
        该接口一旦受限就取不到 archive_count（投稿数变 0）。
        因此投稿数改为用 /x/space/navnum 的 data.video 作为权威来源
        （navnum 不受 acc/info 那种 -403 封锁，且 code 正常返回）。
        依次尝试：wbi接口 -> 旧接口 -> 主页HTML抓取 -> 最小化返回，
        最后统一用 navnum 补全/校正 archive_count。
        """
        user = None

        # 方案1: wbi 接口（需要 SESSDATA 才能用）
        try:
            params = self._sign_wbi({"mid": uid})
            data = self._api_get(
                "https://api.bilibili.com/x/space/wbi/acc/info",
                params=params,
                extra_headers={"Referer": f"https://space.bilibili.com/{uid}"},
            )
            if data["code"] == 0:
                d = data["data"]
                user = {
                    "uid": d["mid"],
                    "name": d["name"],
                    "face": d["face"].replace("http://", "https://"),
                    "sign": d.get("sign", "这个人很懒什么都没写"),
                    "level": d.get("level", 0),
                    "archive_count": d.get("archive_count", 0),
                    "album_count": 0,
                }
        except Exception:
            pass  # 失败就试下一个方案

        # 方案2: 旧接口（不带 wbi，有些时候还能用）
        if user is None:
            try:
                data = self._api_get(
                    "https://api.bilibili.com/x/space/acc/info",
                    params={"mid": uid, "jsonp": "jsonp"},
                    extra_headers={"Referer": f"https://space.bilibili.com/{uid}"},
                )
                if data["code"] == 0:
                    d = data["data"]
                    user = {
                        "uid": d["mid"],
                        "name": d["name"],
                        "face": d["face"].replace("http://", "https://"),
                        "sign": d.get("sign", "这个人很懒什么都没写"),
                        "level": d.get("level", 0),
                        "archive_count": d.get("archive_count", 0),
                        "album_count": 0,
                    }
            except Exception:
                pass

        # 方案3: 从用户主页 HTML 抓取基本信息
        if user is None:
            try:
                name = self._scrape_space_name(uid)
                if name:
                    user = {
                        "uid": int(uid),
                        "name": name,
                        "face": "",
                        "sign": "",
                        "level": 0,
                        "archive_count": 0,
                        "album_count": 0,
                    }
            except Exception:
                pass

        # 方案4: 全部失败，返回最小化信息
        if user is None:
            user = {
                "uid": int(uid) if str(uid).isdigit() else 0,
                "name": f"UID:{uid}",
                "face": "",
                "sign": "",
                "level": 0,
                "archive_count": 0,
                "album_count": 0,
            }

        # 投稿数量：用 navnum 校正（最权威，且不受 acc/info 的 -403 影响）
        # data.video=视频投稿数，data.album=图文/图集投稿数
        # 取 max(acc_info 的 archive_count, navnum 的 video)，保证任意单源受限都有兜底
        try:
            nav = self._api_get(
                "https://api.bilibili.com/x/space/navnum",
                params={"mid": uid, "jsonp": "jsonp"},
                extra_headers={"Referer": f"https://space.bilibili.com/{uid}"},
            )
            if nav.get("code") == 0:
                nav_data = nav.get("data", {}) or {}
                nav_video = nav_data.get("video", 0) or 0
                if nav_video and nav_video > user.get("archive_count", 0):
                    user["archive_count"] = nav_video
                nav_album = nav_data.get("album", 0) or 0
                if nav_album and nav_album > user.get("album_count", 0):
                    user["album_count"] = nav_album
        except Exception:
            pass

        return user

    def _scrape_space_name(self, uid):
        """从B站用户主页 HTML 中提取用户名"""
        resp = self.session.get(
            f"https://space.bilibili.com/{uid}",
            timeout=10, verify=VERIFY_SSL,
        )
        if resp.status_code != 200:
            return None
        # 主页 <title> 格式: "XXX的个人空间-XXX个人主页-哔哩哔哩视频"
        # 或旧格式: "XXX的个人空间_哔哩哔哩_bilibili"
        m = re.search(r"<title>(.+?)的个人空间", resp.text)
        if m:
            return m.group(1)
        # 备用: 从 __INITIAL_STATE__ 中提取
        m = re.search(r'"name"\s*:\s*"(.+?)"', resp.text[:5000])
        if m:
            return m.group(1)
        return None

    def _is_charge_only(self, item):
        """判断视频是否为「充电专属」。

        B站 字段说明（来源 bilibili-API-collect）：
        - 空间/投稿列表(arc/search)的 vlist 里带 `is_charging_arc` 字段（True=充电专属）；
        - 播放器接口(/x/player/wbi/v2)用 `is_upower_exclusive`（True=充电专属）兜底；
        - 动态 feed(web-dynamic/v1/feed/space)的 archive 通常【不带】上面两个字段，
          而是用角标 `archive.badge.text`，精确等于「充电专属」来标识；
          部分情况下 archive 也可能直接带 `is_charging_arc`，所以一并检测。
        老的 `attribute & 16` 是「搜索禁止」而非充电专属，勿用此位判断。
        """
        if not isinstance(item, dict):
            return False
        # 1) 已知布尔字段（投稿列表 / 播放器接口）
        for key in ("is_charging_arc", "is_upower_exclusive"):
            val = item.get(key)
            if val in (True, 1, "1", "true", "True"):
                return True
        # 2) 动态 feed 的角标文案（最常见标记方式）：精确匹配「充电专属」
        badge = item.get("badge")
        if isinstance(badge, dict):
            txt = badge.get("text") or ""
            if txt == "充电专属":
                return True
        return False

    def _has_charge_badge(self, obj, _depth=0):
        if isinstance(obj, dict):
            for k, v in obj.items():
                # 字符串值精确匹配「充电专属」（收紧，避免标题/正文含"充电宝""专属福利"等误判）
                if isinstance(v, str) and v == "充电专属":
                    return True
                if self._has_charge_badge(v, _depth + 1):
                    return True
            for k in obj:
                # 字段名(key)精确匹配「专属动态」
                if k == "专属动态":
                    return True
        elif isinstance(obj, list):
            for v in obj:
                if self._has_charge_badge(v, _depth + 1):
                    return True
        return False

    def get_user_videos(self, uid, page=1, page_size=10):
        """获取UP主的视频列表（仅用官方视频列表接口，不与动态混用）
        B站近期对 /x/space/wbi/arc/search 加了严格限制(-403)，
        接口受限时直接返回空列表并标记 limited=True，由前端提示用户
        去【动态】标签下载（视频动态同样可下载），保持"视频是视频、
        动态是动态"互不干扰。

        错误分类（避免旧版 except: pass 把瞬时网络抖动永久当成受限）：
        - 风控码(-403/-352/-412 等) / 非0业务码 → 直接 limited（不可重试）；
        - 连接/超时类异常 → 有 SESSDATA 时轻量重试最多3次，仍失败再 limited；
        - 游客(无 SESSDATA) → 不重试，直接 limited。
        """
        # 游客直接返回 limited，避免无谓重试空等
        if not self.sessdata:
            return {"videos": [], "total": 0, "page": page, "source": "limited", "limited": True}

        # 参考 BilibiliDown(INEedBiliAV) 的已知可用请求格式补充参数:
        # tid(分区,0=全部) / keyword(搜索词,空) / platform=web
        # 这几个参数对齐后, 签名与B站服务端预期一致, 可提高请求通过率
        params = self._sign_wbi({
            "mid": uid,
            "tid": 0,
            "pn": page,
            "ps": page_size,
            "keyword": "",
            "order": "pubdate",
            "platform": "web",
        })
        url = "https://api.bilibili.com/x/space/wbi/arc/search"
        referer = f"https://space.bilibili.com/{uid}/video"

        # 风控/业务错误码：返回 limited（不可重试）
        LIMITED_CODES = frozenset({-403, -352, -412, -509, -799, -101})
        last_reason = ""
        for attempt in range(3):
            try:
                resp_data = self._api_get(url, params=params, extra_headers={"Referer": referer})
                code = resp_data.get("code", -1)
                if code == 0:
                    vlist = resp_data["data"]["list"]["vlist"]
                    videos = []
                    for v in vlist:
                        videos.append({
                            "bvid": v["bvid"],
                            "title": v["title"],
                            "cover": v["pic"],
                            "duration": v.get("length", "??:??"),
                            "play": v.get("play", 0),
                            "created": v.get("created", 0),
                            "description": v.get("description", ""),
                            "charge_only": self._is_charge_only(v),
                        })
                    total = resp_data["data"]["page"]["count"]
                    return {"videos": videos, "total": total, "page": page, "source": "api", "limited": False}
                # 非0业务码：风控类直接放弃；其它也视为受限（不重试，重试也不会变）
                last_reason = f"BILI {code}: {resp_data.get('message', '')}"
                _logger.warning("get_user_videos uid=%s page=%s 接口返回非0: %s", uid, page, last_reason)
                return {"videos": [], "total": 0, "page": page, "source": "limited", "limited": True,
                        "reason": last_reason}
            except Exception as e:
                last_reason = str(e)
                # 连接/超时类错误才重试；其余（签名失败等）不重试
                if _is_conn_error(e) and attempt < 2:
                    _logger.info("get_user_videos uid=%s page=%s 第%d次失败(可重试): %s", uid, page, attempt + 1, last_reason)
                    time.sleep(3)
                    continue
                _logger.warning("get_user_videos uid=%s page=%s 失败: %s", uid, page, last_reason)
                break
        # 重试耗尽或不可重试异常 → limited（带原因，便于前端/日志区分）
        return {"videos": [], "total": 0, "page": page, "source": "limited", "limited": True,
                "reason": last_reason}


    def fetch_dynamics_batch(self, uid, offset=""):
        """按游标(offset)拉取【一批】动态（不一次拉完，避免触发 B站 风控）。
        返回 (parsed_list, next_offset, has_more)。
        parsed_list 中每条已解析；图文动态 type=="image"（用于统计"图文数量"）。
        """
        data = self._api_get(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            # features=itemOpusStyle: 图文动态以新版 opus 结构返回（含标题）
            params={"host_mid": uid, "offset": offset, "features": "itemOpusStyle"},
            extra_headers={"Referer": f"https://space.bilibili.com/{uid}/dynamic"},
        )
        if data.get("code") != 0:
            return [], "", False
        items = data.get("data", {}).get("items", [])
        next_offset = data.get("data", {}).get("offset", "")
        has_more = data.get("data", {}).get("has_more", False)

        # 预先收集转发 orig 中的 bvid —— 用于去重：
        # B站 feed API 在 UP 转发视频时，除了转发卡片，还会返回一条不带 orig 的
        # DYNAMIC_TYPE_AV 独立卡片（同一 BV 号）。若不跳过，同视频会出现两张卡片
        # （一张来自 orig 合成、一张来自 feed），且 feed 那张无法识别为转发。
        synth_bvids = set()
        for item in items:
            orig = item.get("orig")
            if orig and isinstance(orig, dict):
                try:
                    # 提取 orig 的 bvid（两种路径都兼容）
                    om = (orig.get("modules") or {}).get("module_dynamic") or {}
                    oarch = (om.get("major") or {}).get("archive") or {}
                    obvid = oarch.get("bvid")
                    if not obvid:
                        # 兜底：递归搜整棵 orig 树
                        obvid = (self._find_archive_in_tree(orig) or {}).get("bvid")
                    if obvid:
                        synth_bvids.add(obvid)
                except Exception:
                    pass

        parsed = []
        for item in items:
            p = self._parse_dynamic(item, host_mid=str(uid))
            if not p:
                continue

            # 跳过 feed 中已由 orig 合成覆盖的 DYNAMIC_TYPE_AV（防重复）
            if synth_bvids and p.get("type") == "video" and not (item.get("orig") and isinstance(item.get("orig"), dict)):
                if p.get("bvid") and p["bvid"] in synth_bvids:
                    continue

            parsed.append(p)
            # 转发动态的 orig 里包含原视频（投稿视频/动态视频），但 feed API 不一定单独返回它，
            # 额外解析出一条独立条目，让前端同时显示转发卡片和原视频卡片。
            # 仅对视频 origin 做此处理：图文/文字的 orig 已经作为独立动态存在 feed 里，不需要合成。
            # 条件放宽为"凡带 orig 子动态"：兼容 B站 偶发把转发视频的 type 标成 DYNAMIC_TYPE_AV。
            if item.get("orig") and isinstance(item.get("orig"), dict):
                try:
                    # 传 host_mid：orig 可能是 DYNAMIC_TYPE_AV 视频卡，作者若非宿主（被转发），
                    # 必须标"转发"而非"投稿视频"（与 feed 独立卡走同一判定，避免合成卡漏判）。
                    orig_parsed = self._parse_dynamic(item["orig"], host_mid=str(uid))
                    if orig_parsed and orig_parsed.get("id") and orig_parsed.get("bvid") \
                       and orig_parsed["id"] != p.get("id"):
                        parsed.append(orig_parsed)
                except Exception:
                    pass  # orig 解析失败不影响转发本身
        return parsed, next_offset, has_more


    def _classify_dyn_video(self, pub_action):
        """根据 module_author.pub_action 区分动态视频/投稿视频。
        对应B站动态时间模块文本：'发布了动态视频' / '投稿了视频'。
        返回 '动态视频' / '投稿视频'（无法判定时默认归为投稿视频，避免退化为裸'视频'标签）。
        """
        pa = pub_action or ""
        if "动态视频" in pa:
            return "动态视频"
        if "投稿" in pa:  # 含 "投稿了视频"
            return "投稿视频"
        return "投稿视频"



    def _extract_text_from_desc(self, desc):
        """从 module_dynamic.desc 提取文字。

        兼容两种结构：
        - 常规：desc.text 直接是纯文字；
        - 纯表情/特殊动态：desc.text 可能为空，真正的"文字+表情"在
          desc.rich_text_nodes 里（text 节点给文字，emoji 节点给 [表情名]）。
        这样即使动态只有表情、没有普通文字，也能拼出可见内容。
        """
        if not isinstance(desc, dict):
            return ""
        # 1) 直接文字
        t = desc.get("text")
        if isinstance(t, str) and t.strip():
            return t.strip()
        # 2) 富文本节点拼装（表情动态常只有 rich_text_nodes 而缺 text）
        nodes = desc.get("rich_text_nodes") or []
        if isinstance(nodes, list) and nodes:
            parts = []
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                if n.get("type") == "RICH_TEXT_NODE_TYPE_EMOJI":
                    emoji = n.get("emoji") or {}
                    nm = emoji.get("name") or n.get("text") or ""
                    if nm:
                        parts.append("[%s]" % nm)
                else:
                    nt = n.get("text") or ""
                    if nt:
                        parts.append(nt)
            joined = "".join(parts).strip()
            if joined:
                return joined
        return ""

    def _extract_emoji_map(self, desc_like):
        """从 desc / opus.summary 的 rich_text_nodes 里提取表情映射。

        返回 {"[表情名]": icon_url}，供前端把文字里的 [表情名] 占位符
        替换成真正的表情图片 <img>。desc.text 本身就用 [表情名] 占位，
        所以无论 text 来自哪一路，都能用这个映射还原表情。
        """
        emap = {}
        if not isinstance(desc_like, dict):
            return emap
        nodes = desc_like.get("rich_text_nodes") or []
        if not isinstance(nodes, list):
            return emap
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if n.get("type") != "RICH_TEXT_NODE_TYPE_EMOJI":
                continue
            emoji = n.get("emoji") or {}
            nm = emoji.get("name") or n.get("text") or ""
            url = emoji.get("icon_url") or ""
            if nm and url:
                # name 本身通常已带方括号（如 "[妙]"）；统一补齐成 [名] 形式
                key = nm if nm.startswith("[") else "[%s]" % nm
                emap[key] = url
        return emap

    def _extract_duration(self, archive):
        """从动态/视频 archive 提取时长。B站 不同接口字段名不一致：
        - 动态 feed 的 major.archive 常用 `duration`（秒，数字或字符串）
        - 视频列表接口（arc/search）常用 `length`
        - 个别接口直接给格式化好的 `duration_text`（如 "3:21"）
        统一返回原始值（交给前端 formatDuration 处理）。"""
        if not isinstance(archive, dict):
            return 0
        for key in ("duration", "length", "duration_text"):
            val = archive.get(key)
            if val not in (None, "", 0, "0"):
                return val
        return 0

    def _find_archive_in_tree(self, node):
        """在动态 JSON 树里递归查找第一个含有效 bvid 的 archive 字典。"""
        if isinstance(node, dict):
            if node.get("bvid"):
                return node
            for v in node.values():
                r = self._find_archive_in_tree(v)
                if r:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = self._find_archive_in_tree(v)
                if r:
                    return r
        return None

    def _parse_dynamic(self, item, host_mid=None):
        """解析单条动态，提取类型、文字、图片、BV号等。

        host_mid: 可选，宿主UP的UID。非空时触发转发检测：若 DYNAMIC_TYPE_AV 的
                  module_author.mid ≠ host_mid，说明该视频由其他UP投稿、被宿主转发，
                  应标记为"转发"而非"投稿视频"。
        """
        try:
            dtype = item.get("type") or ""
            did = item.get("id_str") or ""
            modules = item.get("modules") or {}
            # 转发判定已移到下方 if/elif 链首位（is_forward）：凡带 orig 子动态一律按转发解析，
            # 兼容 B站 偶发把"转发视频"的 type 标成 DYNAMIC_TYPE_AV 的异常（仍带 orig）。
            mod_author = modules.get("module_author") or {}
            mod_dynamic = modules.get("module_dynamic") or {}

            # 提取文字（兼容纯表情动态：text 为空时从 rich_text_nodes 拼装）
            desc = mod_dynamic.get("desc")
            desc_text = self._extract_text_from_desc(desc)
            text_parts = [desc_text] if desc_text else []

            major = mod_dynamic.get("major") or {}
            major_type = major.get("type", "")

            result = {
                "id": did,
                "type": "",
                "type_label": "未知",
                "text": "\n".join(text_parts).strip(),
                "images": [],
                "bvid": "",
                "title": "",
                "cover": "",
                "timestamp": mod_author.get("pub_ts", 0),
                "time_str": "",
                "dyn_video_type": "",
                "charge_only": False,
                # [表情名] -> icon_url 映射，前端据此把占位符渲染成表情图片
                "emoji_map": self._extract_emoji_map(desc),
            }

            # ---- 转发动态（优先判定：B站 偶发把"转发视频"的 type 标成 DYNAMIC_TYPE_AV，但仍带 orig）----
            is_forward = dtype == "DYNAMIC_TYPE_FORWARD" or (
                bool(item.get("orig")) and isinstance(item.get("orig"), dict)
            )
            if is_forward:
                self._parse_forward_body(item, result)
            # ---- 视频动态 ----
            elif dtype == "DYNAMIC_TYPE_AV":
                # 作者UID ≠ 宿主UP的UID → 这是一条被转发的视频
                # （B站 feed API 在 UP 转发时会额外生成一条不带 orig 的 DYNAMIC_TYPE_AV
                #   卡片，module_author.mid 仍是原视频作者而非宿主UP，据此识别转发）
                author_mid = str(mod_author.get("mid", ""))
                if host_mid and author_mid and author_mid != str(host_mid):
                    result["type_label"] = "转发"
                else:
                    result["type_label"] = "视频"
                result["type"] = "video"
                archive = major.get("archive", {})
                result["bvid"] = archive.get("bvid", "")
                result["title"] = archive.get("title", "")
                result["cover"] = archive.get("cover", "")
                result["duration"] = self._extract_duration(archive)
                result["charge_only"] = self._has_charge_badge(archive) or self._has_charge_badge(item)
                # 区分 动态视频 / 投稿视频（封面右上角角标用）
                result["dyn_video_type"] = self._classify_dyn_video(mod_author.get("pub_action"))
                if archive.get("desc"):
                    ad = archive["desc"].strip()
                    # 避免视频简介与动态配文/标题重复拼入 dy-text
                    if ad and ad not in text_parts:
                        text_parts.append(ad)
                        result["text"] = "\n".join(text_parts).strip()

            # ---- 图文动态（新版 opus 格式）----
            elif dtype in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"):
                result["type"] = "image" if dtype == "DYNAMIC_TYPE_DRAW" else "text"
                result["type_label"] = "图文" if dtype == "DYNAMIC_TYPE_DRAW" else "文字"
                # 充电专属检测：递归搜索整个动态 JSON 树里任意 badge.text 含"充电"/"专属"
                charge = self._has_charge_badge(item)
                result["charge_only"] = charge
                if major_type == "MAJOR_TYPE_OPUS":
                    opus = major.get("opus", {})
                    for pic in opus.get("pics", []):
                        url = pic.get("url") or pic.get("src", "")
                        if url:
                            result["images"].append(url)
                    # 新版 opus 图文自带独立标题（前端 opus-module-title__text 元素），
                    # 有的动态文字/emoji 只写在标题里而 summary/desc 全空，必须单独提取。
                    # title 可能是字符串，也可能是 {"text": "..."} 结构，两种都兼容。
                    opus_title = opus.get("title")
                    if isinstance(opus_title, dict):
                        opus_title = opus_title.get("text", "")
                    if opus_title and str(opus_title).strip():
                        result["title"] = str(opus_title).strip()
                    summary = opus.get("summary") or {}
                    # opus 的表情节点在 summary.rich_text_nodes，合并进映射
                    result["emoji_map"].update(self._extract_emoji_map(summary))
                    opus_text = summary.get("text", "") if isinstance(summary, dict) else ""
                    if opus_text and opus_text.strip():
                        # opus 以 summary 为准；但若与 desc 重复/更短则保留较长的，避免丢字
                        if not desc_text or len(opus_text.strip()) > len(desc_text):
                            result["text"] = opus_text.strip()
                elif major_type == "MAJOR_TYPE_DRAW":
                    draw = major.get("draw", {})
                    for pic in draw.get("items", []):
                        url = pic.get("src") or pic.get("url", "")
                        if url:
                            result["images"].append(url)
                elif major_type == "MAJOR_TYPE_BLOCKED":
                    # 充电专属图文/文字动态：未充电用户看不到实际内容，
                    # B站 返回屏蔽占位结构（blocked.hint_message 提示文案）。
                    # 提取提示文案作为 text，避免前端渲染出空白卡片。
                    blocked = major.get("blocked", {})
                    hint = blocked.get("hint_message") or ""
                    if isinstance(hint, str) and hint.strip():
                        if not desc_text or len(hint.strip()) > len(desc_text):
                            result["text"] = hint.strip()
                else:
                    # feed/space 对未充电的专属动态：module_dynamic 全空（desc/major 皆 null），
                    # 唯一标记是 basic.is_only_fans=true。此时给通用提示文案，避免空白卡片。
                    if (item.get("basic") or {}).get("is_only_fans"):
                        hint = "专属动态：加入当前UP主的包月充电即可解锁观看"
                        if not desc_text or len(hint) > len(desc_text):
                            result["text"] = hint

            # 转发动态的处理已抽到 _parse_forward_body（见上方 if is_forward 分支调用）

            # 格式化时间（B站API返回的pub_ts可能是字符串，需转int）
            ts = result.get("timestamp", 0)
            if ts:
                try:
                    result["time_str"] = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(int(ts))
                    )
                except (ValueError, TypeError, OSError):
                    result["time_str"] = ""
            return result
        except Exception:
            # 动态解析失败不阻塞其他动态的处理，静默跳过
            return None

    def _parse_forward_body(self, item, result):
        """填充转发动态内容（被转发的内容在 item.orig 里）。

        兼容 orig 位于 mod_dynamic.orig 或 item.orig 顶层两种结构；
        orig 解析异常时不影响转发外壳（至少保留转发者评论/标题）。
        """
        result["type"] = "forward"
        result["type_label"] = "转发"
        modules = item.get("modules") or {}
        mod_dynamic = modules.get("module_dynamic") or {}
        # B站 转发的 orig 位置不固定：标准在 mod_dynamic.orig，部分动态(如该 UP 的
        # 转发视频)会把完整 orig 放到 item.orig 顶层。两者兼容，否则取到 None 整段
        # 视频提取/原文拼接全部落空，转发视频只剩转发者评论、无封面。
        orig = mod_dynamic.get("orig") or item.get("orig") or {}
        if not orig:
            return
        try:
            orig_modules = orig.get("modules") or {}
            orig_mod_dynamic = orig_modules.get("module_dynamic") or {}
            orig_desc = orig_mod_dynamic.get("desc") or {}
            # 转发的原动态里的表情也合并进映射
            result["emoji_map"].update(self._extract_emoji_map(orig_desc))
            orig_text = orig_desc.get("text", "")
            if orig_text:
                result["text"] += f"\n\n[转发内容]\n{orig_text}"
            # 提取转发的视频：标准路径取 orig.modules.module_dynamic.major.archive
            orig_major = orig_mod_dynamic.get("major") or {}
            orig_archive = orig_major.get("archive") or {}
            if not orig_archive.get("bvid"):
                # 兜底：整棵 orig 树递归找第一个含 bvid 的 archive
                # （兼容结构异常 / 嵌套转发 / major.type 非标准）
                orig_archive = self._find_archive_in_tree(orig) or {}
            if orig_archive.get("bvid"):
                result["bvid"] = orig_archive["bvid"]
                result["title"] = orig_archive.get("title", "")
                result["cover"] = orig_archive.get("cover", "")
                result["duration"] = self._extract_duration(orig_archive)
                result["type"] = "video"
                # 转发视频保留 type_label="转发"，与转发(文字/图文)统一归类，
                # 避免 TAB 栏出现多余的「视频(转发)」胶囊、且「转发」计数恒为 0。
                result["type_label"] = "转发"
                # 转发的是视频时，也从原动态判定 动态视频/投稿视频（用于命名模板 dynType 分类）
                orig_author = orig_modules.get("module_author") or {}
                result["dyn_video_type"] = self._classify_dyn_video(
                    orig_author.get("pub_action")
                )
            else:
                # 转发的是 OPUS（图文/文字）：提取标题、正文、图片
                orig_major = orig_mod_dynamic.get("major") or {}
                if orig_major.get("type") == "MAJOR_TYPE_OPUS":
                    opus = orig_major.get("opus", {})
                    for pic in opus.get("pics", []):
                        url = pic.get("url") or pic.get("src", "")
                        if url:
                            result.setdefault("images", []).append(url)
                    opus_title = opus.get("title")
                    if isinstance(opus_title, dict):
                        opus_title = opus_title.get("text", "")
                    if opus_title and str(opus_title).strip():
                        result["title"] = str(opus_title).strip()
                    summary = opus.get("summary") or {}
                    result["emoji_map"].update(self._extract_emoji_map(summary))
                    opus_text = summary.get("text", "") if isinstance(summary, dict) else ""
                    if opus_text and opus_text.strip():
                        result["text"] += f"\n\n[转发内容]\n{opus_text.strip()}"
        except Exception:
            # orig 解析异常不影响转发外壳（至少保留转发者评论/标题）
            pass

    # -------------------- 视频下载 --------------------

    def get_video_info(self, bvid):
        """获取视频信息（cid 是下载必需的参数）"""
        data = self._api_get(
            "https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}
        )
        if data["code"] != 0:
            raise Exception(self._friendly_error(data, "获取视频信息"))
        return data["data"]

    def get_video_pages(self, bvid):
        """获取视频的分P列表（供前端展示集数、逐集下载）。
        返回 {bvid, title, count, pages:[{page,cid,part,duration}]}"""
        info = self.get_video_info(bvid)
        pages = info.get("pages") or []
        result = []
        for p in pages:
            result.append({
                "page": p.get("page", 0),
                "cid": p.get("cid"),
                "part": p.get("part", "") or "",
                "duration": int(p.get("duration", 0) or 0),
            })
        return {"bvid": bvid, "title": info.get("title", bvid), "count": len(result), "pages": result}

    def get_video_pages_batch(self, bvids):
        """批量获取多个视频的分P列表（一次 HTTP 往返判断哪些是多P）。

        返回 {bvid: {bvid,title,count,pages}}，单个视频失败不影响其它（不出现在结果里）。
        前端据此决定是否显示「📂 分P」按钮——只有 count>1 才显示。
        带 60s 内存缓存（_PAGES_CACHE），减少重复请求与被风控的风险。
        """
        result = {}
        now = time.time()
        to_fetch = []
        for bvid in (bvids or []):
            bvid = (bvid or "").strip()
            if not bvid:
                continue
            cached = _PAGES_CACHE.get(bvid)
            if cached and (now - cached[0]) < 60:
                result[bvid] = cached[1]
            else:
                to_fetch.append(bvid)
        for bvid in to_fetch:
            try:
                data = self.get_video_pages(bvid)
                result[bvid] = data
                _PAGES_CACHE[bvid] = (now, data)
            except Exception:
                # 单个失败不影响整体；前端查不到该 bvid 信息时按"单P"处理（不显示按钮）
                continue
        return result

    def check_login(self):
        """检查当前 SESSDATA 是否有效（调用 nav 接口）。

        返回 dict: {"login": bool, "uname": str, "code": int, "msg": str, "reason": str}
        reason 取值（供前端精确区分提示，避免把网络/风控错误误报成"已过期"）：
          - "ok"      : 已登录
          - "empty"   : 根本没设置 SESSDATA
          - "expired" : 真的登录态失效（cookie 存在但 nav 返回未登录 / isLogin=false）
          - "blocked" : 风控/网络受限（-352/-412/-509 等），并非 cookie 本身过期
          - "error"   : 请求异常（超时/TLS/解析失败等），无法判定
        """
        if not self.sessdata:
            return {"login": False, "uname": "", "code": -101, "msg": "未设置 SESSDATA", "reason": "empty"}
        try:
            data = self._api_get(
                "https://api.bilibili.com/x/web-interface/nav",
                extra_headers={"Referer": "https://www.bilibili.com/"},
            )
            code = data.get("code")
            # 风控/网络受限类错误码：这些不是 cookie 过期，不应提示"已过期"
            risk_codes = {-352, -412, -509, -799}
            if code == 0 and data.get("data"):
                d = data["data"]
                if bool(d.get("isLogin")):
                    return {
                        "login": True,
                        "uname": d.get("uname", ""),
                        "code": 0,
                        "msg": d.get("uname", ""),
                        "reason": "ok",
                    }
                # cookie 存在但 isLogin=false —— 真正失效（过期/被踢）
                return {
                    "login": False,
                    "uname": "",
                    "code": -101,
                    "msg": "登录态已失效",
                    "reason": "expired",
                }
            if code in risk_codes:
                return {
                    "login": False,
                    "uname": "",
                    "code": code,
                    "msg": data.get("message", "风控拦截，暂时无法验证"),
                    "reason": "blocked",
                }
            # 其它非 0 码（如 -101 未登录、-400 请求错误等）一律视为登录态失效
            return {
                "login": False,
                "uname": "",
                "code": code if code is not None else -1,
                "msg": data.get("message", "登录态校验失败"),
                "reason": "expired" if code == -101 else "blocked",
            }
        except Exception as e:
            return {"login": False, "uname": "", "code": -1, "msg": str(e), "reason": "error"}

    def get_self_info(self):
        """获取当前登录用户的完整信息（nav 接口）。
        返回 dict: {login, uid, name, face, level}
        未登录或请求失败时 login=False。
        """
        if not self.sessdata:
            return {"login": False, "uid": 0, "name": "", "face": "", "level": 0}
        try:
            data = self._api_get(
                "https://api.bilibili.com/x/web-interface/nav",
                extra_headers={"Referer": "https://www.bilibili.com/"},
            )
            if data.get("code") == 0 and data.get("data"):
                d = data["data"]
                return {
                    "login": bool(d.get("isLogin")),
                    "uid": d.get("mid", 0),
                    "name": d.get("uname", ""),
                    "face": (d.get("face", "") or "").replace("http://", "https://"),
                    "level": (d.get("level_info") or {}).get("current_level", 0),
                }
        except Exception:
            pass
        return {"login": False, "uid": 0, "name": "", "face": "", "level": 0}

    # -------------------- 扫码登录（二维码）--------------------

    def qr_generate(self):
        """生成 B站 二维码登录票据。

        调用 passport 的 qrcode/generate 接口拿到：
          - qrcode_key：32位密钥（轮询时用，180秒超时）
          - url：二维码内容（需编码成图片给用户扫）
        返回 dict: {qrcode_key, url}
        失败抛出 Exception（含友好原因）。
        """
        try:
            resp = self.session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                timeout=10, verify=VERIFY_SSL,
            )
            data = resp.json()
        except Exception as e:
            raise Exception(f"获取登录二维码失败（网络错误）: {e}")
        if data.get("code") != 0:
            raise Exception(f"获取登录二维码失败: {data.get('message', '未知错误')}")
        d = data.get("data") or {}
        key = d.get("qrcode_key")
        url = d.get("url")
        if not key or not url:
            raise Exception("获取登录二维码失败：返回数据缺少 qrcode_key / url")
        return {"qrcode_key": key, "url": url}

    def qr_poll(self, qrcode_key):
        """轮询扫码状态。

        code 含义：
          0    成功（data.url 里含登录态 cookie，从响应头 set-cookie 提取完整 cookie 串）
          86038 二维码已失效（需重新生成）
          86090 已扫码，待确认
          86101 未扫码
        返回 dict:
          {code(int), status(str: 'scanning'|'confirming'|'expired'|'success'|'unknown'),
           message(str), cookie(str|None)}
        cookie 仅在成功时返回完整 cookie 串（SESSDATA;bili_jct;DedeUserID...）。
        """
        try:
            resp = self.session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qrcode_key},
                timeout=10, verify=VERIFY_SSL,
            )
            data = resp.json()
        except Exception as e:
            raise Exception(f"轮询扫码状态失败（网络错误）: {e}")
        code = data.get("code")
        msg = data.get("message", "")
        # 从响应头 set-cookie 提取完整 cookie 串（仅成功时服务端会下发）
        cookie = None
        if code == 0:
            cookie = _extract_cookie_from_headers(resp.headers)
            # 关键收紧：B站 在部分情况下（如已登录态复用、仅下发 buvid 指纹）
            # 即便未真正扫码确认也会返回 code=0，但 cookie 里没有 SESSDATA。
            # 必须以“拿到 SESSDATA 登录态”作为成功判据，否则视为未扫码继续轮询。
            if not cookie or "SESSDATA=" not in cookie:
                code = 86101  # 回退为“未扫码”，继续轮询
                cookie = None
        # 状态码 → 友好分类（与官方定义一致）
        if code == 0:
            status = "success"
        elif code == 86090:
            status = "confirming"
        elif code == 86038:
            status = "expired"
        elif code == 86101:
            status = "scanning"
        else:
            status = "unknown"
        return {"code": code, "status": status, "message": msg, "cookie": cookie}

    def get_watch_later(self, page_size=40):
        """获取登录用户的『稍后再看』列表（需登录）。
        返回标准化视频列表：[{aid,bvid,title,cover,duration,owner_name,owner_mid,pubdate,view,progress}]
        """
        try:
            data = self._api_get(
                "https://api.bilibili.com/x/v2/history/toview",
                extra_headers={"Referer": "https://www.bilibili.com/"},
            )
        except Exception as e:
            raise Exception(f"获取稍后再看失败: {e}")
        if data.get("code") != 0:
            raise Exception(self._friendly_error(data, "获取稍后再看"))
        items = (data.get("data") or {}).get("list", []) or []
        result = []
        for it in items[:page_size]:
            if not isinstance(it, dict):
                continue
            owner = it.get("owner") or {}
            result.append({
                "aid": it.get("aid"),
                "bvid": it.get("bvid", ""),
                "title": it.get("title", ""),
                "cover": it.get("pic", ""),
                "duration": it.get("duration", 0),
                "owner_name": owner.get("name", ""),
                "owner_mid": owner.get("mid", ""),
                "pubdate": it.get("pubdate", 0),
                "view": (it.get("stat") or {}).get("view", 0),
                "progress": it.get("progress", 0),
            })
        return result

    def get_favorites(self):
        """获取登录用户创建的收藏夹列表，每个收藏夹附带其全部资源（需登录）。
        逐个收藏夹翻页拉取（B站 fav/resource/list 的 ps 上限为 20），直到拉完全部或达安全上限。
        返回 [{id,title,media_count,cover,items:[...]}]，items 结构与稍后再看一致（含 bvid/title/cover/duration 等）。
        """
        self_info = self.get_self_info()
        up_mid = self_info.get("uid")
        if not up_mid:
            raise Exception("未获取到登录用户UID，请确认 SESSDATA 有效")
        # 1) 列出该用户创建的收藏夹
        try:
            fparams = self._sign_wbi({"up_mid": up_mid, "type": 0})
            fdata = self._api_get(
                "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                params=fparams,
                extra_headers={"Referer": "https://www.bilibili.com/"},
            )
        except Exception as e:
            raise Exception(f"获取收藏夹列表失败: {e}")
        if fdata.get("code") != 0:
            raise Exception(self._friendly_error(fdata, "获取收藏夹列表"))
        folders = (fdata.get("data") or {}).get("list", []) or []
        result = []
        for f in folders:
            fid = f.get("id")
            folder_items = []
            # 2) 逐个收藏夹翻页拉取全部资源
            #    B站 fav/resource/list 的 ps 上限为 20，故以 ps=20 分页，pn 递增直到拉完或达安全上限。
            media_count = f.get("media_count", 0)
            pn = 1
            while True:
                try:
                    rparams = self._sign_wbi({
                        "media_id": fid,
                        "pn": pn,
                        "ps": 20,
                        "platform": "web",
                    })
                    rdata = self._api_get(
                        "https://api.bilibili.com/x/v3/fav/resource/list",
                        params=rparams,
                        extra_headers={"Referer": f"https://space.bilibili.com/{up_mid}/favlist"},
                    )
                    if rdata.get("code") != 0:
                        break
                    medias = (rdata.get("data") or {}).get("medias", []) or []
                    if not medias:
                        break
                    for m in medias:
                        if not isinstance(m, dict):
                            continue
                        upper = m.get("upper") or {}
                        folder_items.append({
                            "aid": m.get("id"),
                            "bvid": m.get("bvid", ""),
                            "title": m.get("title", ""),
                            "cover": m.get("cover", ""),
                            "duration": m.get("duration", 0),
                            "owner_name": upper.get("name", ""),
                            "owner_mid": upper.get("mid", ""),
                            "pubtime": m.get("pubtime", 0),
                            "collect": (m.get("cnt_info") or {}).get("collect", 0),
                        })
                    # 拉完条件：已达声明的 media_count，或本页不足 20 条（说明已到末页）
                    if (media_count and len(folder_items) >= media_count) or len(medias) < 20:
                        break
                    pn += 1
                    if pn > 50:  # 安全上限，避免极端情况下死循环
                        break
                except Exception:
                    break  # 单个收藏夹某页失败不阻断其余
            result.append({
                "id": fid,
                "title": f.get("title", ""),
                "media_count": f.get("media_count", 0),
                "cover": f.get("cover", ""),
                "items": folder_items,
            })
        return result

    def get_play_url(self, bvid, cid, qn=80):
        """获取视频播放地址。带多级降级，尽量保证能拿到可用流：
           DASH 1080P → DASH 720P → DASH 480P → FLV 480P(单文件兜底)。
           每次请求都检测返回的数据是否真的含可用视频流，全部失败才报错。
        """
        # (fnval, qn, 说明) —— DASH(fnval=16)画质最好但需合并；
        # 最后的 FLV(fnval=1) 单文件无需合并，且风控相对宽松，作为兜底。
        # qn 为用户选择的"期望最高画质"，实际返回受账号等级（大会员等）限制；
        # 按从高到低逐级降级请求，保证拿到账号可达的最高画质且不超过所选上限。
        # fnval=4048 = DASH(16)|4K(64)|HDR(128)|Dolby(256)|DolbyAudio(512)|臻彩(1024)|8K(2048)
        DASH_QNS = [127, 120, 116, 112, 80, 64, 32, 16]
        chosen = qn if qn in DASH_QNS else 80
        dash_qns = [q for q in DASH_QNS if q <= chosen] or [16]
        attempts = [(4048, q, f"DASH {QN_LABEL.get(q, q)}") for q in dash_qns]
        # FLV 单文件兜底（无需合并、风控宽松）；画质不超过所选上限（最低 480P）
        flv_qn = min(chosen, 32)
        flv_label = "FLV 480P" if flv_qn >= 32 else f"FLV {QN_LABEL.get(flv_qn, flv_qn)}"
        attempts.append((1, flv_qn, flv_label))
        last_err = None
        for fnval, q, label in attempts:
            try:
                data = self._api_get(
                    "https://api.bilibili.com/x/player/playurl",
                    params={"bvid": bvid, "cid": cid, "qn": q,
                            "fnval": fnval, "fourk": 1},
                    extra_headers={"Referer": "https://www.bilibili.com/"},
                )
                if data.get("code") != 0:
                    last_err = self._friendly_error(data, f"获取播放地址[{label}]")
                    continue
                d = data.get("data") or {}
                # 必须真的含可用流才算成功，否则继续降级
                if d.get("dash") or d.get("durl"):
                    return d
                last_err = f"接口返回空数据[{label}]（可能是未登录或视频受限）"
            except Exception as e:
                last_err = str(e)
                continue
        raise Exception(
            f"无法获取下载地址：{last_err}。"
            f"请确认：①已设置【有效且未过期】的 SESSDATA；"
            f"②该视频不是大会员/付费专享；③账号未被风控限制。"
        )

    def download_video(self, bvid, base_dir, task_id=None, qn=None,
                       folder_template=None, file_template=None, extra_vars=None,
                       max_duration=0, num_threads=3, target_page=None,
                       use_stage=True, cache_root=None, max_pages=0):
        """下载视频完整流程，支持多P（分P）视频。

        - target_page=None：下载全部分P，每P一个独立文件（修复"只下第一P"）。
        - target_page=N（1-based）：只下载第 N 集。
        - max_duration：单P超长则跳过；多P时跳过该P，单P时整体跳过；0=不限。
        - max_pages：仅下载前 N 集（1-based 截断）；0=不限。用于自动监控防爆盘
          （如监控的 UP 发长番剧，避免一次性下几十 G）。与 target_page 互斥，
          target_page 优先级更高。
        """
        if is_cancelled(task_id):
            raise DownloadCancelled()
        update_task(task_id, "downloading", 0, "正在获取视频信息...")
        os.makedirs(base_dir, exist_ok=True)

        info = self.get_video_info(bvid)
        title_raw = info.get("title", bvid)
        pic = info.get("pic", "")

        # 分P列表（view 接口返回 pages；没有则退化为单P）
        pages = info.get("pages") or []
        if not pages:
            pages = [{"page": 1, "cid": info.get("cid"), "part": title_raw,
                      "duration": info.get("duration", 0)}]
        # 原始是否多P：决定单集下载时是否追加 _P{n} 文件名后缀
        is_multi_source = len(pages) > 1
        # 指定只下某一P（优先级高于 max_pages）
        if target_page:
            pages = [p for p in pages if p.get("page") == int(target_page)]
            if not pages:
                raise Exception(f"未找到第 {target_page} 集")
        elif max_pages and max_pages > 0:
            # 仅下前 N 集（防爆盘，常用于自动监控）
            pages = pages[:int(max_pages)]
            if not pages:
                raise Exception(f"未找到前 {max_pages} 集内容")

        total_pages = len(pages)

        # 时长检查：单P超长->整体异常；多P超长->跳过该P
        if max_duration:
            if total_pages == 1:
                dur_sec = int(pages[0].get("duration", 0) or 0)
                if dur_sec / 60 > max_duration:
                    raise Exception(
                        f"视频时长 {int(dur_sec / 60)} 分 {int(dur_sec % 60)} 秒，"
                        f"超过设置上限 {max_duration} 分钟，已自动跳过"
                    )
            else:
                keep = []
                for p in pages:
                    pdur = int(p.get("duration", 0) or 0)
                    if pdur / 60 > max_duration:
                        update_task(task_id, "downloading", 0, f"P{p.get('page')} 时长超上限，跳过")
                    else:
                        keep.append(p)
                pages = keep
                total_pages = len(pages)
                if total_pages == 0:
                    raise Exception(f"所有分P均超过 {max_duration} 分钟上限，已跳过")

        if total_pages == 0:
            raise Exception("没有可下载的分P")

        # ===== 命名模板渲染（文件夹与基础文件名，对所有P共用）=====
        try:
            owner = info.get("owner", {}) or {}
            pubdate_ts = info.get("pubdate", 0)
            template_vars = {
                "avTitle": title_raw,
                "UpName": owner.get("name", ""),
                "bvid": bvid,
                "qn": str(qn) if qn else "",
                "uid": str(owner.get("mid", "")),
                "dynType": "普通视频",
            }
            if pubdate_ts:
                t = time.localtime(int(pubdate_ts))
                template_vars["pubdate"] = time.strftime("%Y-%m-%d", t)
                template_vars["pubtime"] = time.strftime("%H-%M-%S", t)
            if extra_vars:
                template_vars.update(extra_vars)
            if folder_template == "__FLAT__":
                # 特殊标记：不套子文件夹（用于「我的」来源：稍后再看/收藏夹，目录由调用方组织）
                folder_path = ""
            else:
                folder_path = render_template(folder_template or "视频", template_vars)
            folder_path = folder_path.replace("\\", "/").strip("/")
            file_name = render_template(file_template or "avTitle", template_vars)
            file_name = os.path.basename(file_name.replace("\\", "/").rstrip("/"))
            if not file_name.strip():
                file_name = sanitize_filename(title_raw)
            rel_path = folder_path + "/" + file_name if folder_path else file_name

            out_dir = os.path.join(base_dir, os.path.dirname(rel_path)) if "/" in rel_path else base_dir
            # 统一路径分隔符：render_template 内部用 "/" 拼子目录，os.path.join 用系统分隔符，
            # 不规整会出现 "来源\模板/文件" 混用 \ 与 / 的情况（Windows 能跑但不干净）。
            out_dir = os.path.normpath(out_dir)
            base_name = os.path.basename(rel_path) or sanitize_filename(title_raw)
        except Exception as e:
            raise Exception(f"命名模板渲染失败：{e}")
        os.makedirs(out_dir, exist_ok=True)

        # ===== 缓存中转：分段下载与合并都在临时缓存目录完成，合并成功后才移入最终保存位置 =====
        # 这样保存目录（可能在同步盘上）绝不会出现半成品 .m4s / 不完整 .mp4。
        final_mp4 = os.path.join(out_dir, base_name + ".mp4")
        if os.path.exists(final_mp4):
            update_task(task_id, "done", 100, f"文件已存在，跳过: {base_name}.mp4")
            return final_mp4
        stage = None
        if use_stage:
            stage = os.path.join(cache_root or tempfile.gettempdir(),
                                 "bili_cache", str(task_id or "single"))
            os.makedirs(stage, exist_ok=True)

        # 下载主体（封面 + 逐P）统一包在 try/finally 中：成功则把合并好的文件从缓存移入保存位置，
        # 失败/取消则清理缓存目录，保证保存目录始终零残留。
        try:
            # 下载封面图（仅一次，第一P）
            if pic:
                try:
                    cover_path = os.path.join(out_dir, f"{base_name}_封面.jpg")
                    if stage:
                        cover_stage = os.path.join(stage, f"{base_name}_封面.jpg")
                        self._download_file_simple(pic, cover_stage)
                        shutil.move(cover_stage, cover_path)
                    else:
                        self._download_file_simple(pic, cover_path)
                except Exception:
                    pass

            # 逐P下载
            downloaded_files = []
            for idx, p in enumerate(pages):
                if is_cancelled(task_id):
                    raise DownloadCancelled()
                page_no = p.get("page", idx + 1)
                cid = p.get("cid")
                if not cid:
                    continue
                part = p.get("part", "") or ""
                # 本P文件名（多P源追加 _P{n} 与分集标题；单集下载多P视频时也加后缀）
                page_base = base_name
                if is_multi_source:
                    suffix = f"_P{page_no}"
                    if part and part != title_raw:
                        suffix += f"_{sanitize_filename(part)}"
                    page_base = base_name + suffix
                # 已下载则跳过（按最终保存路径判断，缓存模式同样适用）
                final_out_path = os.path.join(out_dir, page_base + ".mp4")
                if os.path.exists(final_out_path):
                    downloaded_files.append(final_out_path)
                    continue
                # 进度区间（按页码均分 0-100）
                p_base = int(100 * idx / total_pages)
                p_span = int(100 / total_pages)

                update_task(task_id, "downloading", p_base, f"正在获取播放地址… [P{page_no}/{total_pages}]")
                play_data = self.get_play_url(bvid, cid, qn=qn)
                qn_label = _qn_label_from_play(play_data)

                update_task(task_id, "downloading", p_base,
                            f"正在下载 P{page_no}/{total_pages}" + (f" {part}" if part else ""),
                            quality=qn_label or None)
                stage_dir = stage if stage else out_dir
                if "dash" in play_data:
                    out = self._download_dash(play_data["dash"], stage_dir, page_base, task_id,
                                              num_threads, progress_base=p_base, progress_span=p_span)
                elif "durl" in play_data:
                    out = self._download_durl(play_data["durl"], stage_dir, page_base, task_id,
                                              num_threads, progress_base=p_base, progress_span=p_span)
                else:
                    raise Exception(f"P{page_no} 无法获取下载地址，可能需要登录B站")
                if stage:
                    final_out = os.path.join(out_dir, os.path.basename(out))
                    shutil.move(out, final_out)
                    downloaded_files.append(final_out)
                else:
                    downloaded_files.append(out)

            update_task(task_id, "done", 100,
                        f"下载完成：共 {len(downloaded_files)} 个分P" if total_pages > 1 else "下载完成")
            return downloaded_files[0] if len(downloaded_files) == 1 else downloaded_files
        finally:
            if stage:
                shutil.rmtree(stage, ignore_errors=True)

    def _candidate_urls(self, stream):
        """从单个流里收集所有候选下载地址（baseUrl + backupUrl），
        并按可达性排序：upos 节点优先，mcdn 节点垫后。"""
        if not stream:
            return []
        urls = []
        u = stream.get("baseUrl") or stream.get("base_url")
        if u:
            urls.append(u)
        backup = stream.get("backupUrl") or stream.get("backup_url")
        if backup and isinstance(backup, str):
            backup = [backup]
        if isinstance(backup, list):
            for b in backup:
                if b and b not in urls:
                    urls.append(b)
        if len(urls) > 1:
            urls = _sort_cdn_urls(urls)
        return urls

    def _download_dash(self, dash, save_dir, title, task_id, num_threads=3, progress_base=0, progress_span=100):
        """下载 DASH 格式：视频流+音频流分别下载，再用FFmpeg合并。
        progress_base/progress_span：多P时把本P进度映射到整体 0-100 区间。"""
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise Exception("未找到FFmpeg，无法合并音视频流。请确认FFmpeg已安装")

        # 选画质最高的视频流
        videos = dash.get("video", [])
        if not videos:
            raise Exception("没有可用的视频流")
        video_stream = max(videos, key=lambda x: x.get("id", 0))

        # 选第一条音频流
        audios = dash.get("audio", [])
        audio_stream = audios[0] if audios else None

        video_url = self._candidate_urls(video_stream)
        audio_url = self._candidate_urls(audio_stream) if audio_stream else []

        video_path = os.path.join(save_dir, f"{title}_video.m4s")
        audio_path = os.path.join(save_dir, f"{title}_audio.m4s")
        output_path = os.path.join(save_dir, f"{title}.mp4")

        # 如果已下载过，跳过
        if os.path.exists(output_path):
            update_task(task_id, "downloading", progress_base + progress_span, f"文件已存在: {title}.mp4")
            return output_path

        # 下载视频流（进度映射进本P区间）
        vb = progress_base + progress_span * 0.03
        ve = progress_base + progress_span * 0.50
        update_task(task_id, "downloading", int(vb), f"正在下载视频流: {title}")
        self._download_file(video_url, video_path, task_id, "下载视频流", int(vb), int(ve), num_threads)

        if audio_url:
            ab = progress_base + progress_span * 0.50
            ae = progress_base + progress_span * 0.80
            update_task(task_id, "downloading", int(ab), f"正在下载音频流: {title}")
            self._download_file(audio_url, audio_path, task_id, "下载音频流", int(ab), int(ae), num_threads)

            update_task(task_id, "merging", int(progress_base + progress_span * 0.80), f"正在合并音视频: {title}")
            self._merge_av(ffmpeg, video_path, audio_path, output_path)

            # 清理临时文件
            for tmp in (video_path, audio_path):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        else:
            # 没有独立音频流，直接用视频文件
            try:
                os.rename(video_path, output_path)
            except OSError:
                pass

        update_task(task_id, "downloading", progress_base + progress_span, f"完成: {title}.mp4")
        return output_path

    def _merge_av(self, ffmpeg, video_path, audio_path, output_path):
        """用FFmpeg合并视频和音频"""
        # 检查文件是否存在
        if not os.path.isfile(ffmpeg):
            raise Exception("FFmpeg可执行文件不存在，请检查安装路径")
        if not os.path.isfile(video_path):
            raise Exception("视频流文件不存在")
        if not os.path.isfile(audio_path):
            raise Exception("音频流文件不存在")

        # 删除可能存在的旧输出文件
        if os.path.isfile(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

        # FFmpeg 日志文件（用文件重定向代替管道，避免 Windows/沙箱编码问题）
        log_file = os.path.join(os.path.dirname(output_path), "_ffmpeg.log")

        # 先尝试直接复制流（最快，不重新编码）
        cmd = [ffmpeg, "-y", "-i", video_path, "-i", audio_path,
               "-c:v", "copy", "-c:a", "copy", output_path]
        try:
            with open(log_file, "w") as errf:
                result = subprocess.run(cmd, stdout=errf, stderr=subprocess.STDOUT, timeout=600)
        except Exception as e:
            raise Exception(f"FFmpeg启动失败: {e}")

        if result.returncode == 0:
            # 清理日志文件
            try:
                os.remove(log_file)
            except OSError:
                pass
            return

        # 读取第一次失败的日志
        err1 = ""
        try:
            with open(log_file, "r", errors="replace") as f:
                err1 = f.read()[-300:]
        except OSError:
            err1 = "(无法读取日志)"

        # 删除第一次可能产生的部分输出文件
        if os.path.isfile(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

        # 直接复制失败，改用 aac 编码音频
        cmd2 = [ffmpeg, "-y", "-i", video_path, "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", output_path]
        try:
            with open(log_file, "w") as errf:
                result2 = subprocess.run(cmd2, stdout=errf, stderr=subprocess.STDOUT, timeout=600)
        except Exception as e:
            raise Exception(f"FFmpeg启动失败(第二次): {e}")

        if result2.returncode == 0:
            # 清理日志文件
            try:
                os.remove(log_file)
            except OSError:
                pass
            return

        # 两次都失败，读取错误信息
        err2 = ""
        try:
            with open(log_file, "r", errors="replace") as f:
                err2 = f.read()[-500:]
        except OSError:
            err2 = "(无法读取日志)"
        raise Exception(f"FFmpeg合并失败: {err2}（首次尝试 copy 流也失败: {err1}）")

    def _download_durl(self, durl_list, save_dir, title, task_id, num_threads=3, progress_base=0, progress_span=100):
        """下载旧格式（单文件，不分音视频）。progress_base/progress_span 同 _download_dash。"""
        output_path = os.path.join(save_dir, f"{title}.mp4")
        if os.path.exists(output_path):
            update_task(task_id, "downloading", progress_base + progress_span, f"文件已存在: {title}.mp4")
            return output_path
        if len(durl_list) == 1:
            urls = self._candidate_urls(durl_list[0])
            self._download_file(urls, output_path, task_id, "下载视频",
                                int(progress_base + progress_span * 0.03),
                                int(progress_base + progress_span * 0.95), num_threads)
        else:
            # 多分段：逐个下载后合并
            seg_paths = []
            for i, seg in enumerate(durl_list):
                seg_path = os.path.join(save_dir, f"{title}_part{i}.mp4")
                urls = self._candidate_urls(seg)
                self._download_file(
                    urls, seg_path, task_id, f"下载第{i+1}段",
                    int(progress_base + progress_span * 0.03),
                    int(progress_base + progress_span * 0.80), num_threads
                )
                seg_paths.append(seg_path)
            # 用FFmpeg合并分段
            ffmpeg = find_ffmpeg()
            if ffmpeg and len(seg_paths) > 1:
                update_task(task_id, "merging", int(progress_base + progress_span * 0.80), "正在合并分段视频")
                cmd = [ffmpeg, "-y", "-i", "concat:" + "|".join(seg_paths), "-c", "copy", output_path]
                subprocess.run(cmd, capture_output=True, timeout=600)
                for sp in seg_paths:
                    try:
                        os.remove(sp)
                    except OSError:
                        pass
            elif seg_paths:
                os.rename(seg_paths[0], output_path)
        update_task(task_id, "downloading", progress_base + progress_span, f"完成: {title}.mp4", quality="480P")
        return output_path

    def _download_file(self, url, filepath, task_id, message, start_pct, end_pct, num_threads=3):
        """下载文件，带进度跟踪与多源回退。
        url 可为单个地址或候选地址列表；列表时优先 upos 节点，
        遇到网络层连接/超时错误（curl 7/28）自动换下一个候选源再试。
        """
        candidates = url if isinstance(url, (list, tuple)) else [url]
        if len(candidates) > 1:
            candidates = _sort_cdn_urls(candidates)
        RETRY = 3  # 每个候选源最多重试次数（应对间歇性 HTTP/2 流错误 curl (92) 等）
        last_err = None
        for idx, cur_url in enumerate(candidates, 1):
            for attempt in range(RETRY):
                try:
                    self._download_file_impl(
                        cur_url, filepath, task_id, message,
                        start_pct, end_pct, num_threads, idx, len(candidates),
                    )
                    return
                except Exception as e:
                    last_err = e
                    # 仅网络层连接/超时/传输错误才重试；鉴权/风控/文件类错误直接抛出
                    if not _is_conn_error(e):
                        raise
                    # 连接/传输错误：先同地址重试，用完重试次数再换下一个候选源
                    if task_id:
                        if attempt + 1 < RETRY:
                            update_task(task_id, "downloading", start_pct,
                                        f"{message}（源{idx}/{len(candidates)} 传输中断，重试 {attempt+1}/{RETRY}...）")
                        elif idx < len(candidates):
                            update_task(task_id, "downloading", start_pct,
                                        f"{message}（源{idx}失败，换备用源...）")
        raise Exception(f"{message}失败：下载源连接/传输不稳定，已重试仍失败（{last_err}）")

    def _download_file_impl(self, url, filepath, task_id, message, start_pct, end_pct, num_threads, src_idx, src_total):
        """单地址实际下载（供 _download_file 多源回退调用）。"""
        src_tag = f" [源{src_idx}/{src_total}]" if src_total > 1 else ""
        headers = {**DEFAULT_HEADERS, "Referer": "https://www.bilibili.com/"}
        resp = self.session.get(url, stream=True, headers=headers, timeout=30, verify=VERIFY_SSL)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))

        # 确保输出目录存在（保险，避免并发/子文件夹场景下的 [Errno 2]）
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

        # 小于 5MB 或单线程或限速 → 直接下载（限速时强制单线程）
        if total < 5 * 1024 * 1024 or num_threads <= 1 or (self.speed_limit > 0):
            if self.speed_limit > 0 and num_threads > 1:
                update_task(task_id, "downloading", 0, "限速模式：已自动切换单线程")
                num_threads = 1
            downloaded = 0
            last_update = 0
            # 速度计量
            speed_samples = []
            speed_bytes_done = 0
            speed_sec_start = time.time()
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 512):
                    f.write(chunk)
                    downloaded += len(chunk)
                    # 限速：确保每秒不超过 speed_limit KB
                    if self.speed_limit > 0:
                        speed_bytes_done += len(chunk)
                        elapsed = time.time() - speed_sec_start
                        if elapsed >= 1.0:
                            speed_bytes_done = 0
                            speed_sec_start = time.time()
                        else:
                            target_bytes = self.speed_limit * 1024 * (elapsed + 0.001)
                            if speed_bytes_done > target_bytes:
                                sleep_time = speed_bytes_done / (self.speed_limit * 1024) - elapsed
                                if sleep_time > 0.001:
                                    time.sleep(sleep_time)
                    if is_cancelled(task_id):
                        raise DownloadCancelled()
                    if task_id and total > 0:
                        now = time.time()
                        if now - last_update > 0.5:
                            ratio = downloaded / total
                            progress = round(start_pct + ratio * (end_pct - start_pct))
                            speed_samples.append((now, downloaded))
                            speed_samples = [(ts, db) for ts, db in speed_samples if now - ts <= 2]
                            speed = 0
                            eta = 0
                            if len(speed_samples) >= 2:
                                first_ts, first_db = speed_samples[0]
                                last_ts, last_db = speed_samples[-1]
                                elapsed_sp = last_ts - first_ts
                                if elapsed_sp > 0:
                                    speed = (last_db - first_db) / elapsed_sp
                                    remaining = total - downloaded
                                    eta = remaining / speed if speed > 0 else 0
                            if speed >= 1048576:
                                speed_str = f"{speed/1048576:.1f} MB/s"
                            elif speed >= 1024:
                                speed_str = f"{speed/1024:.0f} KB/s"
                            elif speed > 0:
                                speed_str = f"{speed:.0f} B/s"
                            else:
                                speed_str = ""
                            msg = f"{message}{src_tag} ({progress}%)"
                            if speed_str:
                                msg += f" {speed_str}"
                                if eta > 0:
                                    eta_str = f"{int(eta//60)}分{int(eta%60)}秒" if eta >= 60 else f"{int(eta)}秒"
                                    msg += f" 剩余{eta_str}"
                            update_task(task_id, "downloading", progress, msg)
                            last_update = now
            update_task(task_id, "downloading", end_pct, f"{message}{src_tag} 完成")
            return

        # === 多线程分段下载 ===
        n = min(num_threads, 8)
        chunk_size = total // n
        part_files = []
        threads_list = []
        progress_lock = threading.Lock()
        finished = [0] * n
        total_done = [0]
        err_box = []  # 收集分段线程异常（子线程异常不会自动冒泡到主线程）
        mt_start = time.time()  # 多线程开始时间，用于速度估算

        def _dl_part(i, start, end):
            part_path = filepath + f".part{i}"
            part_files.append(part_path)
            hdrs = {**headers, "Range": f"bytes={start}-{end}"}
            try:
                # 确保分段文件所在目录存在（保险）
                os.makedirs(os.path.dirname(part_path) or ".", exist_ok=True)
                r = self.session.get(url, headers=hdrs, timeout=60, verify=VERIFY_SSL, stream=True)
                r.raise_for_status()
                with open(part_path, "wb") as pf:
                    for chunk in r.iter_content(chunk_size=1024 * 512):
                        pf.write(chunk)
                        if is_cancelled(task_id):
                            raise DownloadCancelled()
                with progress_lock:
                    finished[i] = 1
                    total_done[0] += 1
                    if task_id:
                        pct = round(start_pct + total_done[0] / n * (end_pct - start_pct))
                        done_segs = total_done[0]
                        down_bytes = done_segs * (total // n)
                        elapsed = time.time() - mt_start
                        speed = down_bytes / elapsed if elapsed > 0 else 0
                        remaining = total - down_bytes
                        eta = remaining / speed if speed > 0 else 0
                        msg = f"{message}{src_tag} ({total_done[0]}/{n})"
                        if speed >= 1048576:
                            msg += f" {speed/1048576:.1f} MB/s"
                        elif speed >= 1024:
                            msg += f" {speed/1024:.0f} KB/s"
                        if eta > 0:
                            eta_str = f"{int(eta//60)}分{int(eta%60)}秒" if eta >= 60 else f"{int(eta)}秒"
                            msg += f" 剩余{eta_str}"
                        update_task(task_id, "downloading", pct, msg)
            except Exception as e:
                err_box.append(Exception(f"分段{i}下载失败: {e}"))
                return

        # 启动分段下载线程
        for i in range(n):
            seg_start = i * chunk_size
            seg_end = seg_start + chunk_size - 1 if i < n - 1 else total - 1
            t = threading.Thread(target=_dl_part, args=(i, seg_start, seg_end), daemon=True)
            t.start()
            threads_list.append(t)
        for t in threads_list:
            t.join()
        # 任意分段下载失败 -> 主线程抛出，避免合并出损坏文件（子线程异常不会自动冒泡）
        if err_box:
            raise err_box[0]
        # 任意分段被取消 -> 终止合并，让 DownloadCancelled 冒泡到 server.run
        if task_id and is_cancelled(task_id):
            raise DownloadCancelled()

        # 合并分段
        with open(filepath, "wb") as out:
            for i in range(n):
                part_path = filepath + f".part{i}"
                with open(part_path, "rb") as pf:
                    out.write(pf.read())
                try:
                    os.remove(part_path)
                except OSError:
                    pass
        update_task(task_id, "downloading", end_pct, f"{message}{src_tag} 完成")

    def _download_file_simple(self, url, filepath):
        """简单下载文件（不跟踪进度，用于小文件如封面图）"""
        headers = {**DEFAULT_HEADERS, "Referer": "https://www.bilibili.com/"}
        resp = self.session.get(url, headers=headers, timeout=30, verify=VERIFY_SSL)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)

    # -------------------- 图文动态下载 --------------------

    def download_dynamic(self, dynamic_data, save_dir, task_id=None, qn=None,
                          folder_template=None, file_template=None, uid="", up_name="",
                          use_stage=True, cache_root=None):
        """下载动态：图片动态下载图片+文字，视频动态走视频下载流程"""
        os.makedirs(save_dir, exist_ok=True)
        dtype = dynamic_data.get("type", "")
        text = dynamic_data.get("text", "")
        images = dynamic_data.get("images", [])
        title = dynamic_data.get("title", "")
        bvid = dynamic_data.get("bvid", "")

        if dtype == "video":
            # 视频动态 -> 走视频下载
            if bvid:
                update_task(task_id, "downloading", 0, f"正在下载视频动态: {title or bvid}")
                extra = {"dynType": dyn_folder_type_from_dynamic(dynamic_data), "dynamicId": str(dynamic_data.get("id", ""))}
                out = self.download_video(bvid, save_dir, task_id, qn=qn,
                                          folder_template=folder_template,
                                          file_template=file_template, extra_vars=extra,
                                          use_stage=use_stage, cache_root=cache_root)
                # 动态文字保存到视频同一目录下
                if text and out:
                    text_path = os.path.join(os.path.dirname(out), "动态文字.txt")
                    with open(text_path, "w", encoding="utf-8") as f:
                        f.write(text)
                return out
            raise Exception("视频动态缺少BV号")

        if dtype in ("image", "text"):
            # ===== 命名模板：图文/文字动态也按模板建子目录 =====
            ts = int(dynamic_data.get("timestamp", 0) or 0)
            dyn_vars = {
                "avTitle": sanitize_filename((title or text or str(dynamic_data.get("id", "")))[:50]),
                "UpName": up_name or "",
                "bvid": dynamic_data.get("bvid", ""),
                "qn": "",  # 图文/文字无画质，条件语法 (:qn 默认) 会走默认
                "dynamicId": str(dynamic_data.get("id", "")),
                "dynType": "图文" if dtype == "image" else "文字",
                "uid": str(uid or ""),
            }
            if ts:
                t = time.localtime(ts)
                dyn_vars["pubdate"] = time.strftime("%Y-%m-%d", t)
                dyn_vars["pubtime"] = time.strftime("%H-%M-%S", t)
            try:
                folder_path = render_template(folder_template or "动态", dyn_vars)
                folder_path = folder_path.replace("\\", "/").strip("/")
                file_name = render_template(file_template or "avTitle", dyn_vars)
                file_name = os.path.basename(file_name.replace("\\", "/").rstrip("/"))
                if not file_name.strip():
                    file_name = sanitize_filename(title or text or str(dynamic_data.get("id", "")))
                out_dir = os.path.join(save_dir, folder_path) if folder_path else save_dir
            except Exception as e:
                raise Exception(f"命名模板渲染失败：{e}")
            os.makedirs(out_dir, exist_ok=True)
            # 图文/文字动态 -> 模板命名图片和文字文件
            if text:
                text_path = os.path.join(out_dir, f"{file_name}_文字.txt")
                with open(text_path, "w", encoding="utf-8") as f:
                    f.write(text)

            if not images:
                update_task(task_id, "done", 100, "文字动态已保存")
                return out_dir

            total = len(images)
            for i, img_url in enumerate(images):
                if is_cancelled(task_id):
                    raise DownloadCancelled()
                ext = "jpg"
                m = re.search(r"\.(jpg|jpeg|png|gif|webp)", img_url, re.I)
                if m:
                    ext = m.group(1).lower()
                # 多图加序号，单图不加
                suffix = f"_{i+1:02d}" if total > 1 else ""
                img_path = os.path.join(out_dir, f"{file_name}{suffix}.{ext}")
                update_task(
                    task_id, "downloading",
                    round(i / total * 95),
                    f"正在下载图片 {i+1}/{total}",
                )
                self._download_file_simple(img_url, img_path)

            update_task(task_id, "done", 100, f"图文已保存（{total}张图片）")
            return out_dir

        raise Exception(f"暂不支持下载此类型动态: {dtype}")


def _extract_cookie_from_headers(headers):
    """从响应头的 set-cookie 列表里拼出完整 cookie 字符串。

    解析规则：每条 set-cookie 形如 `NAME=VALUE; Expires=...; Path=/; ...`。
    取「第一个分号 ; 之前」的 NAME=VALUE 作为真实 cookie（Expires 里的
    逗号/ GMT 日期不会被误当成 cookie 名），并丢弃 HttpOnly/Secure/SameSite/
    Path/Domain/Max-Age/Expires 等属性。
    注意：不能用 http.cookies.SimpleCookie——它对含 % * 等特殊字符的
    SESSDATA 值会校验失败整条丢弃。故采用宽松的首段解析。
    优先保留 SESSDATA / bili_jct / DedeUserID 等关键登录态字段。
    """
    try:
        raw = headers.get_all("set-cookie") if hasattr(headers, "get_all") else None
        if not raw:
            # http.client / 兼容：headers 可能是 dict-like，逐行取 set-cookie
            single = headers.get("set-cookie") if hasattr(headers, "get") else None
            raw = [single] if single else []
        cookies = {}
        skip = {"httponly", "secure", "samesite", "path", "domain", "max-age", "expires"}
        for item in raw:
            # 按分号切分属性，第一个片段即 NAME=VALUE
            first = str(item).split(";", 1)[0].strip()
            if not first or "=" not in first:
                continue
            name, _, val = first.partition("=")
            name = name.strip()
            val = val.strip()
            if not name or not val:
                continue
            # 排掉纯属性键（大小写不敏感）
            if name.lower() in skip:
                continue
            cookies[name] = val
        if not cookies:
            return None
        # 关键字段排序靠前（纯美观，便于排查）
        priority = ["SESSDATA", "bili_jct", "DedeUserID", "sid"]
        def _rank(n):
            return priority.index(n) if n in priority else len(priority)
        items = sorted(cookies.items(), key=lambda kv: _rank(kv[0]))
        return "; ".join(f"{k}={v}" for k, v in items)
    except Exception:
        return None


def dyn_folder_type(dyn_video_type):
    """视频动态 -> 命名模板 dynType 取值。

    - '投稿视频'（投稿了视频的动态）归为 '普通视频'，与【投稿视频】Tab 下载保持一致；
    - '动态视频' / 其它（含无法判定）归为 '动态视频'。
    这样「普通视频」动态就不会被误放进「动态视频」文件夹。
    """
    return "普通视频" if dyn_video_type == "投稿视频" else "动态视频"


def dyn_folder_type_from_dynamic(dyn):
    """动态视频下载时 folder 模板 dynType 取值。
    - 充电专属 → 充电专属
    - 转发视频（type_label=='转发'）→ 转发
    - 投稿视频 → 普通视频
    - 动态视频 / 其它 → 动态视频"""
    dyn = dyn or {}
    if dyn.get("charge_only"):
        return "充电专属"
    if dyn.get("type_label") == "转发":
        return "转发"
    return dyn_folder_type(dyn.get("dyn_video_type", ""))
