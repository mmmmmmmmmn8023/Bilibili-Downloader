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
# 镜像由 GitHub Actions (build.yml) 云端构建并推送到 GHCR，本脚本只负责拉取并重启。
#
set -euo pipefail

# ===== 按你服务器实际情况修改 =====
REPO="https://github.com/mmmmmmmmmn8023/Bilibili-Downloader.git"
DIR="/var/www/Bilibili-Downloader"      # 克隆目标目录（和本脚本路径分开）
BRANCH="develop/feature"
GHCR_IMAGE="ghcr.io/mmmmmmmmmn8023/bilibili-downloader:latest"
# 私有仓库需 GHCR 拉取凭证（公开仓库可留空）；在服务器执行 `docker login ghcr.io` 后写入
# 也可把账号密码放到下方，或用 GitHub PAT（含 read:packages）做 docker login
GHCR_USER="${GHCR_USER:-}"
GHCR_PASS="${GHCR_PASS:-}"
# ==================================

echo "==> [$(date '+%F %T')] 开始部署 $REPO @ $BRANCH"

# 首次克隆，之后拉取（纯 pull，绝不 push）
if [ ! -d "$DIR/.git" ]; then
  echo "==> 首次克隆仓库到 $DIR"
  git clone -b "$BRANCH" "$REPO" "$DIR"
else
  echo "==> 拉取最新代码"
  cd "$DIR"
  # 仅快进合并，避免本地改动导致冲突；若有未跟踪的运行期文件（.gitignore 已排除）不受影响
  git pull --ff-only origin "$BRANCH"
fi

cd "$DIR"

# 确保挂载所需的运行时文件/目录存在（否则 Docker 会把 ./config.json 建成目录导致写入失败）
touch config.json download_history.json
mkdir -p downloads logs

# 登录 GHCR 并拉取最新 app 镜像（公开仓库可跳过登录；私有仓库必须）
if [ -n "$GHCR_USER" ] && [ -n "$GHCR_PASS" ]; then
  echo "==> 登录 GHCR 并拉取镜像"
  echo "$GHCR_PASS" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
fi
docker pull "$GHCR_IMAGE" || echo "警告：拉取 $GHCR_IMAGE 失败（可能镜像尚未构建或仓库私有未登录）"

# 关键：调用仓库自带的 deploy.sh
#   deploy.sh 会：读 .env → 生成 Basic Auth 哈希 → 渲染 Caddyfile → up -d --pull always → 健康检查
#   ⚠️ 不要改成直接 `docker compose up -d --build`，否则 Caddyfile 不会生成，Caddy 启动失败
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
