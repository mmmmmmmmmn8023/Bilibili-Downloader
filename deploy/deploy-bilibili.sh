#!/bin/bash
#
# 服务器侧自动部署脚本（由 GitHub Actions 的 deploy.yml 通过 SSH 调用）
#
# ⚠️ 重要：本脚本必须放在【项目目录之外】，例如 /var/www/deploy-bilibili.sh
#    （不要放进 Bilibili-Downloader 目录，否则首次 git clone 会因目标目录非空而失败）
#
# 与本仓库 .github/workflows/deploy.yml 配套：
#   push develop/feature → GitHub Actions 用 SSH 私钥登录服务器 → 执行本脚本
# 登录方式：deploy.yml 通过 webfactory/ssh-agent 注入私钥（secrets.SSH_PRIVATE_KEY），
#           服务器 ~/.ssh/authorized_keys 中需含有对应公钥，无需密码。
# 镜像由服务器本地 docker compose build 构建（不使用 GHCR），本脚本克隆代码后调用 deploy.sh 构建并重启。
#
set -euo pipefail

# ===== 按你服务器实际情况修改 =====
REPO="https://github.com/mmmmmmmmmn8023/Bilibili-Downloader.git"
DIR="/var/www/Bilibili-Downloader"      # 克隆目标目录（和本脚本路径分开）
BRANCH="develop/feature"
# ==================================

echo "==> [$(date '+%F %T')] 开始部署 $REPO @ $BRANCH"

# 首次克隆，之后拉取（纯拉取，绝不 push）
if [ ! -d "$DIR/.git" ]; then
  echo "==> 首次克隆仓库到 $DIR"
  git clone -b "$BRANCH" "$REPO" "$DIR"
else
  echo "==> 拉取最新代码"
  cd "$DIR"
  # 部署目录无需保留本地提交/改动，直接对齐远端（fetch + reset --hard）：
  # 若远端历史被 force push 改写，git pull --ff-only 会因分支分叉而失败，reset 可避免；
  # 运行期文件（config.json / download_history.json / downloads / logs）均未跟踪，不受影响
  git fetch origin "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi

cd "$DIR"

# 确保挂载所需的运行时文件/目录存在（否则 Docker 会把文件建成目录导致写入失败）
# ⚠️ bili_history.db 也必须 touch：它是 SQLite 单文件库，若主机不存在，Docker 会建成一个
#    目录，sqlite3 打开失败 → server 启动即崩溃（容器 unhealthy）。这是持久化卷新增后漏的一项。
for f in config.json download_history.json bili_history.db; do
  # 若上一轮部署已误建为目录，先删掉再建文件，避免 sqlite 打开失败
  if [ -d "$f" ]; then rm -rf "$f"; fi
  touch "$f"
done
mkdir -p downloads logs

# 关键：调用仓库自带的 deploy.sh
#   deploy.sh 会：读 .env → 生成 Basic Auth 哈希 → 渲染 Caddyfile → up -d --build（本地构建 app 镜像）→ 健康检查
#   ⚠️ 不要改成直接 `docker compose up -d`（不带 --build），否则 Caddyfile 不会生成，Caddy 启动失败
if [ -f "./deploy.sh" ]; then
  chmod +x deploy.sh
  ./deploy.sh
else
  echo "错误：未找到 deploy.sh，无法生成 Caddyfile，终止部署。"
  exit 1
fi

# 清理悬空旧镜像，避免磁盘被历史层占满
docker image prune -f

echo "==> 部署完成：$(date)"
