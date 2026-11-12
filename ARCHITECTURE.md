# Bilibili-Downloader 架构与改造指南

> 生成时间：2026-11-12 · 基于 `main` 分支 `903b21a`（server.py 1869 行 / bilibili.py 2340 行 / db.py 163 行 / static/app.js ≈2900 行 / static/index.html 2077 行）
> 本文件为**本地未跟踪笔记**，不是上游仓库内容，可随时删除或提交到自己分支。

---

## 0. 先决条件（当前机器状态）

| 项 | 状态 |
|---|---|
| git | 便携版 MinGit 2.56.1，`D:\tool\git\cmd\git.exe`，**未加入 PATH** |
| Python | **未安装**（`python` 是 WindowsApps 商店占位符），项目暂时跑不起来 |
| config.json / downloads/ / logs/ | 均不存在，首次启动自动创建 |
| git 身份 | `user.name` / `user.email` 为空，**提交前必须 `git config --global`** |
| 网络 | 直连 GitHub 会 RST；可用镜像 `https://gh-proxy.com/`、`https://ghproxy.net/`（`git clone https://gh-proxy.com/https://github.com/...`） |

跑起来需要：Python 3.13+（本机已装 `D:\tool\python\python.exe`，3.13.17）→ 双击 `start.bat` 即自动建 `.venv` + 装依赖（`curl_cffi>=0.7.0`、`qrcode>=7.4.2`、`Pillow>=10.0.0`，走清华镜像）+ 起服务 → 浏览器 http://127.0.0.1:8000。ffmpeg 已内置在 `ffmpeg/ffmpeg.exe`，无需另装。

**别人 clone 后不用装 Python**：`start.bat` 探测顺序是 PATH → 常见目录 → 注册表（HKCU/HKLM 的 `PythonCore\3.x\InstallPath`，覆盖自定义目录）；全都找不到时，自动从华为云/阿里云/npmmirror/官方依次尝试下载官方安装包，静默装到 `%LOCALAPPDATA%\BiliDownloader\python`（仅当前用户，免管理员），再继续建 venv。探测时每个候选都会**实际执行一次版本检查**，因此 PATH 里 Windows 商店的 `python.exe` 占位符（退出码 9009）会被跳过。

---

## 1. 一句话架构

一个**单进程 Python 标准库 HTTP 服务**（`http.server.ThreadingHTTPServer`）+ **单页前端**（原生 JS）+ **SQLite 历史库** + **两个线程池/定时器**（下载页池、自动监控定时器），业务全部压在两个大文件 `server.py`（HTTP 层 + 编排）和 `bilibili.py`（B 站 API + 下载实现）里。没有框架、没有 ORM、没有构建工具、没有依赖注入。

```
浏览器 (static/index.html + app.js)
   │  fetch /api/*  (1.5s 轮询 /api/status)
   ▼
server.py  Handler(do_GET/do_POST 的 if-elif 路由链)
   ├── _api_*          参数校验 + 调 bilibili / db + 返回 JSON
   ├── _run_download   ThreadPoolExecutor(默认 2 worker) 里的下载编排
   ├── _do_auto_check  threading.Timer 链上的定时监控
   └── _auto_submit    → 内部 HTTP POST http://127.0.0.1:8000/api/download/*
                              ▼
                        bilibili.py  BilibiliAPI (curl_cffi 模拟 Chrome TLS 指纹)
                              ├── WBI 签名 / 风控错误翻译
                              ├── 取 playurl (DASH 逐级降级 → FLV 兜底)
                              ├── 分段多线程下载 → %TEMP%/bili_cache/<task_id>
                              └── ffmpeg 合并 → shutil.move 到下载目录
                              ▼
                        db.py  SQLite downloads 表（去重 + 历史）
```

进程内共享的三块可变全局状态（**改任何并发逻辑前必须知道**）：

1. `bilibili.download_tasks` + `bilibili.tasks_lock`（bilibili.py:37/41）——任务状态字典，server 通过 `from bilibili import download_tasks, tasks_lock` 直接共享（server.py:27-30）。
2. `bilibili._wbi_keys`（bilibili.py:44-46）——WBI 密钥模块级缓存，全实例共用，TTL 3600s。
3. `server.py` 的配置与定时器全局：`_auto_schedule_timer`(:338)、`_auto_busy`(:327)、`_auto_fail_count/_auto_fail_time`(:512-513)、`_DYN_STATE`(:173)、`_qr_state`(:102)、`_file_lock`(:79)。

---

## 2. 文件职责

| 文件 | 行数 | 职责 |
|---|---|---|
| `server.py` | 1869 | 启动、路由、API 处理器、下载编排、自动监控、配置读写、日志 |
| `bilibili.py` | 2340 | B 站全部 API（WBI 签名、动态/视频/收藏/稍后再看/扫码登录）、下载管线、错误分类、文件名清洗、模板渲染 |
| `db.py` | 163 | SQLite 单表 `downloads` 的增删查，去重 |
| `static/app.js` | ≈2900 | 全部前端逻辑（全局可变状态 + 分节函数） |
| `static/index.html` | 2077 | 结构 + **全部内联 CSS**（9-1680 行是 `<style>`，1680-2077 是 body 与 8 个弹窗） |
| `start.bat` | — | Windows 启动器：chcp 65001、探测 Python、起 server.py |
| `ffmpeg/ffmpeg.exe` | 3.6 MB | 内置 ffmpeg，`find_ffmpeg()` 优先用它 |
| `bili_history.db` | 运行时生成 | 历史库（WAL） |
| `config.json` | 运行时生成 | 唯一配置源 |

---

## 3. server.py

### 3.1 启动链路（`main()` server.py:1843）

```
main()
 ├─ os.makedirs(DOWNLOAD_DIR)         # :1844
 ├─ db.init_db()                      # :1846
 ├─ bilibili.VERIFY_SSL = not cfg["insecure_tls"]   # :1848
 ├─ ThreadingHTTPServer((host, 8000), Handler)      # :1851  host 来自 config，默认 127.0.0.1
 ├─ start_auto_scheduler()            # :1859
 └─ serve_forever()                   # :1861
```

- `PORT = 8000` 硬编码（server.py:43）。**改端口必须同步 `_auto_submit` 里的自调用 URL**（server.py:525），否则自动监控"检查到新内容但永远下不下来"且不报错。
- 每个 HTTP 请求一个线程，**无并发上限、无连接超时**；任何 `_api_*` 里做阻塞网络 IO 都会直接吃掉线程（`_api_search` 串行 3 组请求 + sleep(0.3) 就是这种）。

### 3.2 路由表（全在 if-elif 链里，无装饰器/注册表）

`do_GET` server.py:761-805，`do_POST` server.py:807-843。

**登录/账号**
| 方法 | 路径 | 处理器 | 入参 → 出参 |
|---|---|---|---|
| POST | `/api/qr/generate` | `_api_qr_generate` :1725 | 无 → `{ok,qrcode_key,image(base64),expires_in}` |
| POST | `/api/qr/poll` | `_api_qr_poll` :1755 | `{qrcode_key}` → `{ok,status,message,code}` / 成功 `{ok,cookie,sessdata,self}` |
| GET | `/api/qr/status` | :1804 | → `{ok,has_qr,expires_in,status}` |
| GET/POST | `/api/check_cookie` | :1660 | `cookie`/`sessdata` → nav 结果 + `{has_sessdata,sessdata}` |
| GET | `/api/self` | :1684 | → 自己的 UID/昵称/头像 |

**内容浏览**
| 方法 | 路径 | 处理器 | 说明 |
|---|---|---|---|
| GET | `/api/search` | :901 | query+cookie，聚合用户/视频/动态三路，返回 `warnings` |
| GET | `/api/videos` | :1037 | uid,page,cookie,last_total |
| GET | `/api/dynamics` | :1101 | uid,page,cookie,dtype |
| GET | `/api/video_pages` | :1224 | bvid → 分P列表 |
| GET | `/api/video_page_counts` | :1239 | `bvids` 逗号分隔 → 各 bvid 分P数 |
| GET | `/api/watchlater` | :1697 | 稍后再看 |
| GET | `/api/favorites` | :1710 | 收藏夹 |

**下载与任务**
| 方法 | 路径 | 处理器 | 说明 |
|---|---|---|---|
| POST | `/api/download/video` | :1181 | bvid,title,username,cookie,uid,qn,page,dynType,source,self_name,fav_name → `{task_id}` / `{already_downloaded}` / `{error}` |
| POST | `/api/download/dynamic` | :1257 | 动态（含图文/转发/视频） |
| GET | `/api/status` | :1477 | `{tasks:{tid:{status,progress,message,time,title,upname,type,params,quality,error_code,error_detail,file_path}}}` |
| POST | `/api/download/cancel` | :1484 | |
| POST | `/api/download/retry` | :1510 | 可能返回 `{ok:false,reason:"active_exists"}` |
| POST | `/api/tasks/clear` | :1538 | `scope:"finished"|"all"` |

**历史**
| GET | `/api/history` | :1552 | 裸 list |
| POST | `/api/history/clear` | :1555 | `{uid?}` |
| POST | `/api/history/remove` | :1561 | `{dyid|id,bvid}`（内部调私有 `db._norm_val` :1562） |

**自动监控**
| POST | `/api/auto/check` | :1591 | 起 daemon 线程即回 `{ok:true}` |
| POST | `/api/auto/status` | :1596 | `{uids?}` → 每个 UP 的 `{has_new,count,error?}`（**同步阻塞**，逐个拉动态） |
| GET | `/api/auto/log` | :1632 | `after` → `{logs,last_id,running}` |

**设置/杂项**
| GET | `/api/config` | :1570 | **全量明文**，含 `sessdata` |
| POST | `/api/config` | :1578 | 任意 JSON `config.update` 后落盘（无白名单） |
| GET | `/api/logs/download` | :1644 | 下载 server.log |

助手：`_json` :880（吞 BrokenPipe）、`_read_body` :893（**无大小上限**）、`_serve_static` :847（`basename` 防穿越，只服务 static 根目录，`Cache-Control: no-cache`）、`log_message` 被清空 :1835（**没有访问日志**，排障只能靠自建的 logs/server.log）。

### 3.3 下载队列

```
POST /api/download/video
  └─ _api_download_video :1181   校验登录态/参数 → _submit_download :1303
       ├─ task_id = uuid4().hex[:8]
       ├─ update_task(task_id, "queued", 0, "排队中", ...)
       └─ get_download_executor().submit(_run_download, task_id, kind, params)  :1303
            └─ _run_download :1316
                 ├─ db.is_downloaded(...) → 已存在则直接 done   :1331
                 ├─ cfg = load_config()；BilibiliAPI(cookie, proxy, speed_limit)  :1347-1357
                 ├─ kind=="video"    → api.download_video(bvid,...,qn=)   :1360
                 ├─ kind=="dynamic"  → api.download_dynamic(params["dynamic"])  :1380
                 ├─ 成功 → db.add_to_history(...)  :1395 / :1414 / :1422
                 └─ 异常 → _classify_error → 可重试则 threading.Timer(delay, self._run_download) :1447-1464
```

- 线程池：`get_download_executor()` server.py:114，`max_concurrent` 默认 2（:92），钳制 1..8。**配置一变就 `shutdown(wait=False)` 旧池建新池 → 正在跑的任务变孤儿**（继续跑但不受新池约束）。
- 自动重试：`MAX_AUTO_RETRIES=3`、`RETRY_DELAYS=[30,60,120]`（:1447-1455）。重试用 `threading.Timer` 调 `self._run_download`，**绕过线程池并发上限**，且 Timer 持有 Handler 引用。
- 可重试错误码（:1428-1440）：`CURL 7/28/92/18/35/56/6`、`TIMEOUT`、`CONN`、`UNKNOWN`、`HTTP 500/502/503`、`HTTP 412`、`BILI -352/-509/-799`。
- 任务清理：`_autoclean_tasks` :130，超过 150 条时删最旧的 finished，保留 50 条。

### 3.4 自动监控（定时任务）

不是 `sched`/`APScheduler`，而是 **`threading.Timer` 自我重排**：

```
start_auto_scheduler :461
  └─ _schedule_next_check :428   读 config 的 interval(默认1800s) → Timer(interval, _run_scheduled_check)
       └─ _run_scheduled_check :455   → _do_auto_check("定时检查")  → 再 _schedule_next_check()  :455-458
            └─ _do_auto_check :383   (_auto_check_lock + _auto_busy 防重入)
                 └─ 遍历 auto_uids → _auto_download_up :556
                      ├─ api.fetch_dynamics_batch 翻页（最多 50 批，批间 sleep 0.3）
                      ├─ 标签映射 :620-635（充电专属 > 联合投稿 > 动态视频/图文/文字/转发）
                      ├─ auto_cutoff_date 过滤 :574-580、max_pages
                      ├─ 整批都"已下载且通过过滤"则提前 break :723
                      └─ _auto_submit :516 → 内部 HTTP POST /api/download/{video|dynamic}
```

- 内部自调用（:525）是为了**复用同一套下载队列与去重**，代价是 HTTP 回路 + 端口耦合。
- `429` 时 sleep 2s 重试一次（:541）。
- 冷却：`_auto_fail_count` / `_auto_fail_time`（:512-513，纯内存、无锁），配合 `auto_cooldown_minutes`（默认 30）避免风控期反复撞墙。
- 日志：`_auto_log` 环形 1000 条（:330/:503）+ `_auto_log_seq` 自增 id（前端按 `after` 增量拉）。
- 调度器用 `_auto_schedule_timer = True` 作"已启动"哨兵（:473），`stop_auto_scheduler` 用 `if t is not True` 判断（:487）——**改这里极易写出"永不重启"或"双 Timer"**。

### 3.5 配置

`CONFIG_FILE = config.json`（:47，与 server.py 同目录）；`load_config :146` / `save_config :162` 由 `_file_lock`（:79，RLock）串行；**每次调用都重新读盘**（无内存缓存）。

键与默认值：

| 键 | 默认 | 说明 |
|---|---|---|
| `host` | `127.0.0.1` | 监听地址 |
| `sessdata` | `""` | B 站登录态（**明文存盘、明文下发**） |
| `download_dir` | `downloads/` | 下载根目录 |
| `folder_template` / `file_template` | `视频` / `avTitle` | 支持 `(:var 默认)` 语法，`folder_template="__FLAT__"` 表示不建子目录 |
| `qn` | `80` | 期望最高画质 |
| `max_duration` / `max_pages` | `0` | 0 = 不限 |
| `download_threads` | `3` | 单文件分段线程数 |
| `max_concurrent` | `2` | 同时下载任务数（1..8） |
| `proxy` / `speed_limit` | `""` / `0` | speed_limit>0 时 bilibili 内部自动降为单线程 |
| `insecure_tls` | `true` | ← 默认关闭 TLS 校验（`VERIFY_SSL=False`） |
| `notify_urls` | `[]` | Server酱 GET / 其它 POST JSON |
| `auto_uids` / `auto_interval` / `auto_schedule_enabled` / `auto_cooldown_minutes` / `auto_cutoff_date` / `download_types` | — / 1800 / false / 30 / "" / 6 类 | 自动监控 |

`POST /api/config` 的三个副作用（:1582-1588）：`insecure_tls` → 立即改 `bilibili.VERIFY_SSL`；`auto_interval`/`auto_schedule_enabled` → 重启调度器；其余键直接 `config.update` 落盘。

---

## 4. bilibili.py

### 4.1 模块级

- `VERIFY_SSL = False`（:31）——进程级全局，所有请求硬编码引用，**无法按请求切换**。
- `download_tasks`（:37）/ `tasks_lock`（:41）/ `_cancel_tasks`（:342，**无锁 set**）。
- `_wbi_keys` + `_wbi_keys_time` + `_wbi_lock`（:44-46），TTL 3600s（:506）。
- `_PAGES_CACHE = {}`（:49），分P缓存 60s TTL（:1272），**无锁、无上限**。
- `DEFAULT_HEADERS`（:52-62）Chrome/120 UA + Referer + Origin；`MIXIN_KEY_ENC_TAB`（:65-70）WBI 64 项混淆表。
- `QN_LABEL`（:270-271）：`127 最高画质 / 120 4K / 116 1080P60 / 112 1080P+ / 80 1080P / 64 720P / 32 480P / 16 360P`。
- `find_ffmpeg`（:78-92）：优先 `ffmpeg/ffmpeg.exe`，否则 `shutil.which("ffmpeg")`。

### 4.2 工具函数

- `_sort_cdn_urls`（:103-121）——upos=0 / mcdn=2 打分排序候选 CDN。
- `_is_conn_error`（:124-136）——只有 curl 7/28/92/18/56 等网络层错误才允许重试。
- `_classify_error`（:139-234）——`curl (N)` → `CURL N`；`[WinError N]` → `WIN N`；`HTTP N`（412 → "请求被拦截（风控）"）；`code: -N` → `BILI N`；再退关键字。返回 `(code, reason)`。
- `sanitize_filename`（:237-266）——控制符/全角/`#%&` → `_`，段尾点空格处理，100 字符截断，Windows 保留名（:96-100）。
- `update_task`（:305-334）——**保留式更新**（传 None 的字段不改），持 `tasks_lock`。所有状态写入口。
- `render_template`（:369-424）——`(:var 默认)` + 裸变量，单词边界单遍替换，变量值里的 emoji 转 `[emoji]`。
- `DownloadCancelled`（:345）+ `cancel_task/is_cancelled/clear_cancel/clear_all_cancels`（:350-366）。

### 4.3 BilibiliAPI（:431-2271）

`__init__`（:434-455）：`curl_cffi.requests.Session(impersonate="chrome", verify=VERIFY_SSL, ...)`，`CurlOpt.HTTP_VERSION:2`，注入 proxy 与 `SESSDATA`（`urllib.parse.unquote`）。`_init_fingerprint_cookies`（:457）/`_init_login_cookies`（:473，顺带刷新 WBI）。

**WBI 签名**：`_get_wbi_keys`（:501-529，失败返回 `("","")` 并缓存）、`_sign_wbi`（:531-555）：`mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]`，加 `wts`，按 key 排序，`re.sub(r"[ !'()*]", "", value)`，`w_rid = md5(urlencode(filtered) + mixin_key)`。

**业务方法索引**（改功能最常动的）

| 用途 | 方法 | 行 |
|---|---|---|
| 用户信息（4 级降级 + navnum 校正） | `get_user_info` | 605 |
| 用户投稿列表（无 SESSDATA 直接 limited） | `get_user_videos` | 774 |
| 动态批量（含转发合成卡去重） | `fetch_dynamics_batch` | 847 |
| 动态解析/转发正文/视频分类/时长/emoji | `_parse_dynamic` / `_parse_forward_body` / `_classify_dyn_video` / `_extract_duration` / `_extract_emoji_map` | 1021 / 1162 / 914 / 991 / 964 |
| 视频信息/分P（60s 缓存） | `get_video_info` / `get_video_pages` / `get_video_pages_batch` | 1233 / 1242 / 1257 |
| 登录态检查（ok/empty/expired/blocked/error） | `check_login` | 1286 |
| 扫码登录 | `qr_generate` / `qr_poll` | 1371 / 1397 |
| 稍后再看 / 收藏夹 | `get_watch_later` / `get_favorites` | 1444 / 1477 |
| **取播放地址（画质降级）** | `get_play_url` | 1557 |
| **下载入口：普通视频/动态** | `download_video` / `download_dynamic` | 1601 / 2185 |
| 候选 CDN | `_candidate_urls` | 1791 |
| DASH 下载+合并 | `_download_dash` / `_merge_av` | 1811 / 1871 |
| 旧格式（durl 分段） | `_download_durl` | 1949 |
| 文件下载（多线程分段 + 多源回退） | `_download_file` / `_download_file_impl` / `_download_file_simple` | 1988 / 2021 / 2175 |

### 4.4 画质协商（`get_play_url` :1557-1599）

```
DASH_QNS = [127,120,116,112,80,64,32,16]        # :1567
chosen  = qn if qn in DASH_QNS else 80
dash_qns= [q for q in DASH_QNS if q <= chosen]  # 从高到低
attempts= [(fnval=4048, q) for q in dash_qns]   # :1570  DASH|4K|HDR|Dolby|臻彩|8K，fourk=1
         + [(fnval=1, min(chosen,32))]          # :1574  FLV 单文件兜底（无需合并、风控宽松）
```

每到一级都校验 `data["dash"] or data["durl"]` 真的存在（:1589），否则 `continue`；全挂才抛"无法获取下载地址…"（:1595）。→ **实际画质由账号等级与风控决定，`qn` 只是上限**。

### 4.5 单视频下载管线（`download_video` :1601-1789）

1. 取消检查（:1614）→ `update_task("正在获取视频信息")`
2. `get_video_info` → 分P列表；没有 pages 就用 `info["cid"]` 造单P（:1624-1627）
3. 选P / `max_pages` / `max_duration` 过滤
4. 命名：`template_vars = {avTitle, UpName, bvid, qn, uid, dynType, pubdate, pubtime}`（:1672-1683），`folder_template or "视频"`、`file_template or "avTitle"`；`"__FLAT__"` = 不建子目录（:1686）
5. 缓存中转：`stage = %TEMP%/bili_cache/<task_id>`（:1715），下完 `shutil.move` 到目标目录，`finally` 里 `rmtree(stage)`
6. 进度：`p_base = int(100*idx/total_pages)`、`p_span = int(100/total_pages)`（:1758-1759）——**>100P 时 p_span=0，进度条会卡住**
7. `get_play_url` → `_qn_label_from_play` → `update_task(quality=...)`
8. 按格式分发（:1769-1776）：有 `dash` → `_download_dash`；有 `durl` → `_download_durl`
9. 封面 `{base_name}_封面.jpg`

**`_download_dash`（:1811-1869）**：`find_ffmpeg()` 缺失直接抛；`video_stream = max(videos, key=id)`、`audio_stream = audios[0]`；落 `{title}_video.m4s` / `{title}_audio.m4s`；进度切分 **3%→50% 视频、50%→80% 音频、80% 状态转 `merging`**；无音频则 `os.rename` 视频为 `.mp4`；完成后删临时文件。
若目标 `.mp4` 已存在则直接跳过（:1836）——**断点续传不支持，重复任务靠这个兜**。

**`_merge_av`（:1871-1947）**：`ffmpeg -y -i v -i a -c:v copy -c:a copy out` → 失败重试 `-c:a aac -b:a 192k` → 都失败抛错并带两次日志尾部。日志写 **`<输出目录>/_ffmpeg.log`**（:1889），成功即删，失败留下（注意别下到同步盘根目录）。

**`_download_durl`（:1949-1986）**：单段直下；多段逐个下 `{title}_part{i}.mp4` 再 `ffmpeg -f concat` 合并 —— **`subprocess.run` 的 returncode 没检查（:1977）**，合并失败也会当成功并删掉分段，产出坏/空文件；且 quality 硬编码 `"480P"`（:1985）。

**`_download_file_impl`（:2021-2173）**：单线程阈值 5MB（:2033）；分段线程上限 8（:2099）；每候选源 `RETRY=3`（:1996）且只有 `_is_conn_error` 才重试；分段请求 60s 超时（:2116）——`self.session` 被 N 个线程并发复用（curl_cffi Session 线程安全无保证，是潜在随机失败源）。

---

## 5. db.py

- 单文件 SQLite：`bili_history.db`（:22，与 db.py 同目录）；`_db_lock = RLock`（:24）；`_connect()`（:34）每次新建连接，`timeout=30`、`check_same_thread=False`、`PRAGMA journal_mode=WAL`、`busy_timeout=30000`。
- 唯一表（:48-57）：

```sql
downloads(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  data_dyid TEXT NOT NULL DEFAULT '',   -- 动态 id，普通视频为 "0"→存 ""
  bvid      TEXT NOT NULL DEFAULT '',   -- 单集记 bvid#P{n}
  title     TEXT NOT NULL DEFAULT '',
  up_uid    TEXT NOT NULL DEFAULT '',
  up_name   TEXT NOT NULL DEFAULT '',
  time      TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
)
```

- 索引（:59-61）：`idx_dyid` / `idx_bvid` / `idx_uid`。**没有 UNIQUE、没有版本号/迁移框架**（只有 `CREATE TABLE IF NOT EXISTS`）。
- 函数：`init_db` :42、`load_history` :68（返回 `data-dyid/bvid/title/time/up_name/up_uid`，`ORDER BY created_at,id ASC`）、`add_to_history(data_dyid,bvid,title,up_uid,up_name)` :89（**先 SELECT 再 INSERT**）、`is_downloaded` :111、`clear_history(uid=None)` :127、`remove_history(dyid,bvid)` :142、`_norm_val` :28（None/""/`"0"` 归一为空）。
- 去重语义：`bvid#P{n}` 精确匹配；动态用 `data_dyid`。并发下 SELECT-then-INSERT 仍可能双写（无 UNIQUE 兜底）。

---

## 6. 前端（static/）

零框架、零 CDN、零构建：唯一外部资源是 `index.html:2075` 的 `<script src="/static/app.js?v=20260804-58">`（**改完 JS 记得改这个 `?v=` 版本号**，否则浏览器缓存旧文件）。样式是**一个内联 `<style>`**（index.html:9-1674，约 1660 行），无 CSS 变量，色值字面量重复（底 `#0d1117` / 卡 `#161b22` / 边 `#30363d` / 品牌粉 `#fb7299`）。

### 6.1 index.html 结构
- 导航（:1679）：`#selfChip` 1685、`#autoFloat` 1690、`#navSettings` 1694 + 菜单 1696、`#cookieStatus` 1697
- 主列：搜索区 1710、常用 UP 1723、用户卡 1733、`#tabs` 1756（**只剩 dynamics 一个真 tab**，:1757）、`#content` 1775
- 覆盖层：`#selfView` 1783、`#downloadPanel` 1815、`#historyView` 1829、`#cookieModal` 1854、`#qrModal` 1881、`#dlModal` 1908、`#autoModal` 1975、`#pagesModal` 2048、`#imgPreview` 2058
- 事件几乎全是内联 `onclick` / `onchange` 调全局函数；全文件只有 1 个 `addEventListener`（app.js:1867）。

### 6.2 app.js（2877 行）
无模块、无 IIFE、**顶层即全局**：约 60 个可变 `let`（`currentData` 8、`currentTab` 9、`cookie` 10、`downloadTasks` 11、`pollTimer` 13、`downloadedItems` 14、`selectedVideos` 51、`selectedDynamics` 52、`taskItemMap` 55 …），结尾 app.js:2877 裸调 `initApp()`。

主要分节与函数：

| 分节 | 关键函数（行） |
|---|---|
| 初始化 | `initApp` 59（localStorage → `/api/config` 兜底 → `/api/history` 预填 → `verifyCookie` 2352 → `loadSelfChip` 2636 → `renderDownloadPanel` 1676 → 5 分钟心跳 → `startAutoLogPolling` → `startPolling`） |
| 搜索 | `doSearch` 118（500ms 防抖）、`searchByQuery` 136 |
| 常用 UP | `loadFavUps` 209、`renderFavUps` 218、`addUp` 240 |
| 渲染 | `renderUser` 297、`switchTab` 369、`renderVideos` 480、`renderDynamics` 637、`refresh*Badges` 419/437/464 |
| 分页 | `loadDynPage` 834、`loadVideoPage` 885、`renderPagination` 765 |
| 下载 | `downloadVideo` 983、`downloadDynamic` 1026、`openPagesModal` 1070、`revealMultiP` 1128 |
| 选择/批量 | `toggleSelectAll` 1191、`batchDownload` 1250、`downloadAllDynamics` 1329 |
| 任务/轮询 | `pollStatus` 1594、`renderDownloadPanel` 1676、`cancelTask` 1792、`retryTask` 1801 |
| 设置 | `openDlModal` 1891、`saveDlSettings` 1922、`getVideoQn` 1878（`QN_OPTIONS=[127,120,116,112,80,64,32,16]`） |
| 自动化 | `openAutoModal` 1976、`loadAutoData` 1981、`pollAutoLog` 2072、`saveAutoAdvanced` 2197 |
| 登录 | `verifyCookie` 2352、`saveCookie` 2297、`openQrModal` 2411、`pollQr` 2500 |
| 历史 | `openHistoryView` 1408、`renderHistory` 1445 |
| 我的 | `openSelfView` 2668、`loadWatchLater` 2723、`loadFavorites` 2767 |

**轮询**：`setTimeout` 自递归（app.js:1579-1587），`STATUS_FAST_MS=1500` / `IDLE_POLL_MS=10000`（:1559-1560），`startPolling` 只在 `pollTimer` 为空时启动；自动化日志另一条 2s/10s（:2046）；`tickRelativeTimes` 是顶层 `setInterval(...,1000)` 且**从不 clear**。

### 6.3 前后端契约（前端实际调用的）

`GET /api/config`（:69 / :1893 / :1983）· `POST /api/config`（:1954/:2154/:2203/:2318/:2539）· `GET /api/search`（:153/:2031）· `GET /api/dynamics`（:843/:1346，**唯一被使用的列表接口**）· `GET /api/videos`（:907，从 `loadVideoPage` 走，但 videos tab 已死）· `POST /api/download/video`（:997）· `POST /api/download/dynamic`（:1034）· `GET /api/video_pages`（:1086）· `GET /api/video_page_counts`（:1144）· `GET /api/status`（:1596）· 取消/重试/清任务（:1794/:1803/:1822）· `/api/history` + remove + clear（:82/:1427/:1504/:1529）· `/api/check_cookie`（:2275/:2358）· 扫码三兄弟（:2442/:2478/:2503）· `/api/auto/status`（:2004）· `/api/auto/check`（:2244）· `/api/auto/log?after=`（:2074）· `/api/logs/download`（:2118，直接 `<a href>`）· `/api/self`（:2638）/ `/api/watchlater`（:2727）/ `/api/favorites`（:2771）。

### 6.4 前端特有的坑

1. **`setupContentToggles` 未定义**却被调用两次（app.js:897、:944）→ `loadVideoPage` 一跑就 ReferenceError。因为 videos tab 是死代码（`currentTab === "videos"` 的分支全部不可达：363/389/407/1192/1226/1244/1636），所以现在没人踩到——**谁复活视频 tab 谁先炸**。
2. **轮询孤儿链**：`kickStatusPoll` 在 fetch 未落地时被再次调用，`finally` 会覆盖句柄，旧链变永久孤儿 → 反复开关下载面板/自动化弹窗，1.5s 轮询会成倍叠加。
3. **XSS/转义**：全用字符串拼 `innerHTML`（no diff/no key）。`escapeHtml` 2584 走 `textContent`；`escapeAttr` 2590 只处理 `'` 和 `"`，**不处理 `\` 与 `&`**；`jsAttr` 2600 依赖"外层属性必须用单引号"。未转义落点：`d.error` 直插（847/927）、`d.time_str` 裸拼（734）。
4. **localStorage 与 config 双写**：`bilibili_sessdata`(70/2307)、`bili_qn`(1879/1924)、`bili_dl_types`(962/1951)、`dynTypeFilter`(588)。新设置只写 config 不写 localStorage → 刷新后行为回滚（`checkDownloadType` 962 读的是 localStorage）。
5. **分类单一数据源**：`DYN_CATEGORIES`（app.js:619）派生 tab 胶囊（645）、下载类型复选框（627）、过滤（561）——删一项会连带三处消失。
6. **弹层无栈管理**：`openQrModal` 手写先 `closeCookieModal`；`historyView`/`selfView` 用 `display`，其余用 `.show`。
7. 十余处 `alert`/`confirm` 阻塞（1258/1366/1380/1409/1527/1960…）。
8. 两条批量路径语义不同：`downloadAllDynamics` 逐条 `sleep(1000)`（1392），`batchDownload` 每批 10 并发（1277-1281）；"已下载"标记口径必须同步改。

---

## 7. 常见改造 SOP

### 7.1 加一个 HTTP 接口
1. 在 `do_GET`（server.py:768-803）或 `do_POST`（:814-841）的 if-elif 链里加分支；注意 GET 的 `params` 是 `parse_qs` 的 `{k: [v]}`，POST 的 `data` 已 `json.loads`。
2. 写 `_api_xxx(self, params|data)` 私有方法，最后 `self._json({...})`。
3. **别在处理器里做长阻塞**（会占满 ThreadingHTTPServer 线程）；参考 `/api/auto/check`（:1591）起 daemon 线程即回。

### 7.2 加一个下载来源（如"合集"、"番剧"）
1. `BilibiliAPI` 加方法（照 `get_favorites` :1477 的模板）。
2. `server.py` 加 `_api_*` + `_submit_download(kind, params)`。
3. **坑**：`_run_download` 的 `else` 分支硬绑 `params["dynamic"]`（:1380）——新 kind 要么走 video 分支，要么改这里。

### 7.3 加设置项
1. 前端表单 → `POST /api/config`（无白名单，`config.update` 直接落盘，不用改后端也能存）。
2. 要"即时生效"就在 `_api_save_config`（:1578）加副作用分支（参照 `insecure_tls` :1583 / `auto_interval` :1586）。
3. 读取点通常在 `_run_download`（:1347-1357）或 `_auto_*`。

### 7.4 加数据库字段
1. `db.py:48` 的建表语句加列 + 老库手写 `ALTER TABLE`（无迁移框架）。
2. 同步改 `load_history` 的 SELECT（:74）与 `add_to_history` 的 INSERT（:95 附近）。
3. 想真正防重就加 `UNIQUE(bvid)` 或 `UNIQUE(data_dyid)` 并在写入处 `INSERT OR IGNORE`。

### 7.5 改画质/格式
- 只改可选档位：`QN_LABEL`（:270）与 `DASH_QNS`（:1567）**必须同步**。
- 加新封装/新格式：扩 `get_play_url` 的 `attempts`（:1570-1574）+ 新 `_download_xxx` + `download_video` 的格式分发（:1769-1776）。
- 后处理钩子：加在 `_download_dash` 合并成功后（:1852-1860）或 `download_video` 移入最终目录前（:1777-1782）。

### 7.6 改任务状态/进度
**必须三处同时改**：写入侧 `update_task`（bilibili.py:305）、重试侧（server.py:1439-1464）、快照侧 `_api_status`（:1477-1482）+ 前端渲染。`/api/status` 是 `dict(download_tasks)` 浅拷贝，序列化与写入线程可以并发读同一子字典。

---

### 7.7 改前端
1. 新按钮：`index.html` 里直接 `onclick="fn()"`，`app.js` 加全局 `async function fn()`，参数一律用 `jsAttr()`/`escapeAttr()` 包。
2. 新设置项：`#dlModal`（index.html:1908-1965）加 input → `openDlModal`（:1891）回填 → `saveDlSettings`（:1922）组 payload → **同时写 localStorage**（参照 `bili_qn` :1924）。
3. 任务列表加字段：`/api/status` 加键 → `renderDownloadPanel`（:1714-1760）模板加一行 → `.dl-*` 样式写进 index.html 的内联 `<style>`。
4. 新筛选项/下载类型：只改 `DYN_CATEGORIES`（:619），胶囊/复选框/过滤三处自动派生。
5. 新轮询：复用 `startPolling`/`kickStatusPoll` 范式，**不要新起独立 `setInterval`**。
6. **改完务必改 `index.html:2075` 的 `app.js?v=...` 版本号**，否则用户浏览器缓存旧 JS。

## 8. 风险清单（按危险度排序）

**会直接泄露/丢数据**
1. `/api/config` 明文下发 `sessdata`（server.py:1570-1576，注释说"本地部署有意为之"），`/api/status` 的 `params` 里还带 cookie 与完整动态 JSON（:1482）。**`host` 改成 `0.0.0.0` 前必须先加鉴权**，否则局域网内任何人可拿走登录态。README:134 声称"不返回明文 SESSDATA"与代码不符。
2. `_download_durl` 不检查 ffmpeg returncode（bilibili.py:1977）→ 静默产出坏文件并删除分段源。
3. `_api_clear_tasks(scope="all")`（:1543）会清掉 downloading 任务 → 线程继续跑但状态丢失、无法取消。

**并发/竞态**
4. `get_download_executor` 在 `max_concurrent` 变化时 `shutdown(wait=False)`（:120-126）→ 在跑任务成孤儿。
5. 重试用 `threading.Timer` 直接调 `_run_download`（:1463）→ 绕过并发上限，Timer 持有 Handler 引用。
6. `_DYN_STATE` 里的 item dict 在锁外被就地改（`d["downloaded"]=`，:1009-1010 / :1167-1168）；`_ensure_dyn_buffer` 的 `cached_at` 只在 `setdefault` 写，TTL 从首次创建算。
7. `_auto_busy = True` 在取 `_auto_check_lock` **之前**（:389）。
8. `self.session` 被分段线程并发复用（bilibili.py:2116 附近）→ curl_cffi 无线程安全保证。
9. `_PAGES_CACHE`（bilibili.py:49）无锁无上限；`_wbi_keys` 跨实例共享（多账号场景会互相影响）。

**健壮性**
10. `int(params.get("page", ["1"])[0])` 未 try（:1040、:1107）→ 传非数字直接 500。
11. `_read_body` 无大小上限（:894）；`log_message` 全关（:1835）→ 无访问日志，排障困难。
12. 裸 `except: pass` 多处：:143、:448、:199、:1829、:110、bilibili.py:445、467、496、636、658、674、707、1158、1229、1281、1546、1732、1859。
13. `download_dynamic` 的 `动态文字.txt` 固定名（bilibili.py:2207）→ 同目录多个动态互相覆盖。
14. Windows：`TimedRotatingFileHandler` 在文件被占用时轮转失败；`start.bat` 硬编码 `D:\RJ\Python` 等绝对路径。

**设计耦合**
15. `_run_download` 同时管去重/命名/历史/目录；`download_video` 里模板、进度、缓存、封面混在一起。
16. 进度用整数百分比 + 区间切分，>100P 时 `p_span=0`。
17. `PORT=8000` 与 `_auto_submit` 自调用强耦合（:43 / :525）。

---

## 9. 分支与演进

- **当前 `main` 落后 `origin/develop/feature` 31 个提交**。develop 是"服务器部署版"：自建多用户账号体系（`db.py` 的 `users` 表与 role、`hash_password`/`verify_password`/`create_user`/`ensure_admin_user`/`reset_user_password`/`list_users`/`delete_user`，按用户分键存 `config.json`）、管理面板、Docker + Caddy + `deploy.sh` + `DEPLOY.md` + GitHub Actions，并移除了 `start.bat`。
- **要改功能先决定基线**：
  - 只做本机个人使用、要 Windows 一键启动 → 留在 `main`。
  - 要做多用户/公网/容器化 → 切 `develop/feature`，别在 main 上重复造轮子。
- 无论哪条线：**先配 git 身份**（`user.email`/`user.name` 目前为空），并用 `git switch -c feat/xxx` 开分支，不要直接改 main。

---

## 10. 调试速查

```powershell
# 一键启动（自动探测 Python → 建 .venv → 装依赖 → 起服务）
.\start.bat

# 手动等价操作（本机 Python 在 D:\tool\python）
D:\tool\python\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.\.venv\Scripts\python.exe server.py

# 换端口（优先级：BILI_DL_PORT > config.json 的 port > 8000）
$env:BILI_DL_PORT = 8080

D:\tool\git\cmd\git.exe --version   # 便携 git

# 看后端日志
Get-Content .\logs\server.log -Tail 50 -Wait

# 看 ffmpeg 合并失败原因（失败时留在输出目录）
Get-Content .\<下载目录>\_ffmpeg.log -Tail 40

# 直接打接口
curl "http://127.0.0.1:8000/api/config"
curl "http://127.0.0.1:8000/api/status"
```

排障顺序：`/api/config` 看 `has_sessdata` / `sessdata_masked`（**明文 SESSDATA 已不再下发**）→ `/api/check_cookie` 看登录态（expired/blocked）→ `/api/logs/download` 看后端日志 → 下载目录 `_ffmpeg.log` 看合并 → 任务字典 `error_code`（`_classify_error` 的分类码）决定是否重试。

---

## 11. 改造记录（2026-11-12：8 点中的 2–8）

原风险清单里除「落后 develop 31 个提交」（属分支策略）外的 7 项已全部落地，行为语义不变：

| # | 问题 | 现在的做法 |
|---|------|-----------|
| 2 | 路由 if-elif 长链 | `server.py` 顶部 `GET_ROUTES` / `POST_ROUTES` 两张 `路径→Handler 方法名` 表；`do_GET`/`do_POST` 只做鉴权 + JSON 解析 + `_dispatch`。新增接口 = 加一行，测试会校验方法真实存在 |
| 3 | 任务字典三处耦合 | `bilibili.py` 增加任务访问层：`get_task` / `snapshot_tasks(public=True)` / `cleanup_finished_tasks` / `TASK_ACTIVE_STATUS` / `TASK_PUBLIC_FIELDS`；`_api_status`、`_api_cancel_task`、`_api_retry_task`、`_autoclean_tasks` 全部改走它，不再各自持锁拼字典 |
| 4 | PORT 硬编码 + 自调用耦合 | `DEFAULT_PORT` + `_resolve_port()`：环境变量 `BILI_DL_PORT` > `config["port"]` > 8000；`main()` 绑定后用真实端口回填 `PORT`，`_auto_submit` 统一用 `_server_base_url()`。端口被占用会打印明确提示并退出 |
| 5 | 画质表双写 | `bilibili.py` 唯一数据源 `QN_TABLE`，`QN_LABEL` 与 `DASH_QNS` 都由它派生；`get_play_url` 的局部 `DASH_QNS` 已删 |
| 6 | SQLite 无 UNIQUE / 无迁移 | `db.py`：`SCHEMA_VERSION=2` + `_migrate()`；部分唯一索引 `uq_dyid` / `uq_bvid`（空值不参与）；升级前先 `_dedupe()` 清历史重复；`add_to_history` 改为 `INSERT OR IGNORE` 并返回 bool |
| 7 | 明文 SESSDATA 泄漏 | `/api/config` 只返回 `has_sessdata` + `sessdata_masked` 并剔除 `access_token`；`/api/check_cookie`、`/api/qr/poll` 同样不再回传明文；前端不再同步明文（cookie 留空，后端从 config 兜底）。另加**非回环监听时的 token 鉴权**：`main()` 生成 `access_token` 存 config，打开首页时以 `HttpOnly; SameSite=Strict` cookie 下发，也接受 `X-Access-Token` 头或 `?token=` |
| 8 | 前端三类缺陷 | 补上缺失的 `setupContentToggles()`（否则命中缓存的翻页会 ReferenceError）；`pollGeneration` / `autoLogGeneration` 让 kick 作废旧轮询链，不再叠加 setTimeout；`escapeAttr()` 现在同时转义 `& \ ' "` 与换行 |

顺带修复：`_api_clear_tasks(scope="all")` 不再 `clear()` 掉正在下载的任务（线程还在跑却失去句柄），只清已结束的，并返回 `removed` / `active_kept`。

**测试**（不需要联网、不需要 curl_cffi/ffmpeg）：

```powershell
cd D:\tool\blbld
python -m unittest discover -s tests -v      # 18 个用例
```

`tests/test_core.py` 覆盖迁移/去重/画质表/任务快照/路由表/脱敏/端口解析；`tests/test_server_http.py` 真起一个 `ThreadingHTTPServer`，验证静态文件、404、400、401 鉴权、config 脱敏。

