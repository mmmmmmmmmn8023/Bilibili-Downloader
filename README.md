# B站下载器

本地 B站 视频 / 动态下载工具。搜索 UP主即可批量下载投稿视频与图文动态，支持自动化定时监控、下载历史记录与自定义命名。

![License](https://img.shields.io/badge/license-Apache%202.0-blue) ![Python](https://img.shields.io/badge/python-3.13%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Docker-lightgrey)

> **注意**：本项目为**本地单机工具**，默认仅监听 `127.0.0.1`（本机），不对外暴露。请勿在不可信网络中将其绑定到 `0.0.0.0`。

## 功能

- 按 UP主下载全部投稿视频（支持多 P、充电专属标记）
- 下载图文 / 文字动态（含图片）
- 转发动态自动解析原内容
- 稍后再看 / 收藏夹下载
- 自定义命名模板（文件夹 / 文件名）
- 自动化定时监控下载
- 下载历史记录（按 UP主 分组，可去重）
- 下载失败自动重试、速度 / 进度展示

## 快速开始

### Windows（推荐）

双击 `start.bat`：首次会自动创建虚拟环境并安装依赖，随后浏览器打开 `http://localhost:8000`。

### 手动运行

```bash
pip install -r requirements.txt
python server.py
# 浏览器访问 http://localhost:8000
```

FFmpeg 已内置在 `ffmpeg/` 目录（Windows）。其它平台会从系统 `PATH` 自动查找 `ffmpeg`，或自行安装：`apt install ffmpeg` / `brew install ffmpeg`。

## 配置

首次启动会生成 `config.json`（若不存在）。所有配置项均存于该文件。

| 配置项                                       | 说明                                                        |
| ----------------------------------------- | --------------------------------------------------------- |
| `sessdata`                                | B站 登录态 Cookie。**请勿提交到任何公开仓库**，泄露等同账号被盗                    |
| `download_dir`                            | 下载根目录，留空则用内置 `downloads/`                                 |
| `folder_template` / `file_template`       | 文件夹 / 文件名命名模板                                             |
| `qn`                                      | 画质档位（如 `127`=蓝光 1080P）                                    |
| `max_duration`                            | 单条视频最大时长（分钟，超出跳过）                                         |
| `download_threads`                        | 单文件下载线程数                                                  |
| `auto_uids`                               | 自动化监控的 UP主 列表                                             |
| `auto_schedule_enabled` / `auto_interval` | 定时检查开关与间隔（秒）                                              |
| `proxy` / `speed_limit`                   | 下载代理 / 限速（KB/s）                                           |
| `insecure_tls`                            | 默认 `true`（不校验 TLS 证书，兼容代理 / 抓包）；在不受信任网络建议设 `false` 开启严格校验 |
| `host`                                    | 监听地址，默认 `127.0.0.1`；Docker 用环境变量 `BIND_HOST=0.0.0.0` 覆盖   |

### 获取 SESSDATA

浏览器登录 B站 → F12 → Application → Cookies → 复制 `SESSDATA` 的值，在界面右上角设置中粘贴即可（无 Cookie 时画质限制 480P）。

## 目录结构

```
server.py             HTTP 服务 / 历史存储 / 前端 API
bilibili.py           B站 API 调用与下载逻辑
static/               前端页面（index.html / app.js）
ffmpeg/               内置 FFmpeg（Windows）
start.bat             Windows 一键启动
requirements.txt      依赖（curl_cffi）
Dockerfile            容器构建
docker-compose.yml    容器编排
config.json           运行配置（自动生成，勿提交）
download_history.json 下载历史（自动生成，勿提交）
logs/                 运行日志（自动生成）
```

## HTTP API 速查

前端通过以下接口与后端交互，完整实现见 `server.py`。

**GET**

| 端点                       | 作用                      |
| ------------------------ | ----------------------- |
| `/api/search`            | 搜索 UP主（用户 + 视频 + 动态首屏）  |
| `/api/videos`            | 视频分页列表                  |
| `/api/dynamics`          | 动态分页 + 类型筛选             |
| `/api/status`            | 所有下载任务状态（前端轮询）          |
| `/api/history`           | 下载历史                    |
| `/api/config`            | 当前配置（脱敏，不返回明文 SESSDATA） |
| `/api/check_cookie`      | 校验登录态                   |
| `/api/self`              | 当前登录用户信息                |
| `/api/watchlater`        | 稍后再看                    |
| `/api/favorites`         | 收藏夹                     |
| `/api/video_pages`       | 分 P 列表                  |
| `/api/video_page_counts` | 批量分 P 计数                |
| `/api/auto/log`          | 自动化监控日志                 |
| `/api/logs/download`     | 下载日志文件                  |

**POST**

| 端点                      | 作用           |
| ----------------------- | ------------ |
| `/api/download/video`   | 提交视频下载       |
| `/api/download/dynamic` | 提交动态下载       |
| `/api/config`           | 保存配置         |
| `/api/check_cookie`     | 校验登录态        |
| `/api/history/clear`    | 清空历史         |
| `/api/history/remove`   | 删除单条历史       |
| `/api/auto/check`       | 手动触发监控检查     |
| `/api/auto/status`      | 监控 UP主 新内容状态 |
| `/api/download/cancel`  | 取消任务         |
| `/api/download/retry`   | 重试任务         |
| `/api/tasks/clear`      | 清理任务列表       |

## 自动化监控

在设置中填入要监控的 UP主（`auto_uids`）并开启 `auto_schedule_enabled`，程序会按 `auto_interval` 秒间隔自动检查新投稿 / 动态并下载。监控通过内部 HTTP 自调用复用下载队列，因此服务必须监听在本机可访问的地址。

## 安全须知

- **切勿提交 `config.json` 与 `download_history.json`**：两者均含隐私（登录态 / 下载记录），已写入 `.gitignore`。
- 服务器**不向前端返回明文 SESSDATA**（`/api/config`、`/api/check_cookie` 仅告知是否已设置）。
- 默认仅本机访问；若需局域网共享，请知悉风险并自行配置防火墙。

## 常见问题

**Q：为什么需要 SESSDATA？**  
A：未登录时 B站 限制画质最高 480P；填入 SESSDATA 后可解锁更高画质。

**Q：下载很慢或频繁失败？**  
A：检查代理（`proxy`）与限速（`speed_limit`）设置；批量请求已内置限流（约 0.3s/请求）与重试退避，触发风控（错误码 -412 / -352）时会自动重试。

**Q：如何备份我的数据？**  
A：保留 `downloads/`、`config.json`、`download_history.json`、`logs/` 即可。`config.json` 与 `download_history.json` 已纳入 `.gitignore`，请勿误提交。

## 许可证

本项目以 [Apache License 2.0](LICENSE) 发布。

```
Copyright 2026 mmmmmmmmmn8023/Bilibili-Downloader

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
    http://www.apache.org/licenses/LICENSE-2.0
```
