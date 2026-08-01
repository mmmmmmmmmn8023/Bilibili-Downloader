#!/usr/bin/env bash
#
# Bilibili-Downloader 生产部署脚本（公网 + 域名 + 鉴权）
# 拓扑：Caddy(443 + Basic Auth) → app(内部 8000)
#
set -euo pipefail

echo "==> [1/5] 检查 Docker / Docker Compose 插件"
if ! command -v docker >/dev/null 2>&1; then
  echo "错误：未检测到 docker，请先安装 Docker（https://docs.docker.com/get-docker/）"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "错误：未检测到 docker compose 插件，请安装 Docker Compose v2"
  exit 1
fi

echo "==> [2/5] 读取 .env 配置"
if [ ! -f .env ]; then
  echo "错误：未找到 .env，请先执行  cp .env.example .env  并填写"
  exit 1
fi
set -a
# shellcheck disable=SC1091
source ./.env
set +a

: "${DOMAIN:?请在 .env 中设置 DOMAIN}"
: "${AUTH_USER:?请在 .env 中设置 AUTH_USER}"
: "${AUTH_PASS:?请在 .env 中设置 AUTH_PASS}"
: "${ADMIN_EMAIL:?请在 .env 中设置 ADMIN_EMAIL}"

# 确保被挂载的运行时文件存在（否则 Docker 会建成目录，导致应用写入失败）
touch config.json download_history.json 2>/dev/null || true

echo "==> [3/5] 生成 Basic Auth 密码哈希"
# 使用官方 caddy 镜像临时计算哈希，不污染宿主机
HASH=$(docker run --rm caddy:2-alpine caddy hash-password "$AUTH_PASS" 2>/dev/null)
if [ -z "$HASH" ]; then
  echo "错误：生成密码哈希失败（可能拉取 caddy 镜像失败，请检查网络）"
  exit 1
fi

echo "==> [4/5] 生成 Caddyfile"
sed -e "s|__DOMAIN__|${DOMAIN}|g" \
    -e "s|__AUTH_USER__|${AUTH_USER}|g" \
    -e "s|__HASH__|${HASH}|g" \
    -e "s|__ADMIN_EMAIL__|${ADMIN_EMAIL}|g" \
    Caddyfile.template > Caddyfile
echo "    Caddyfile 已生成（域名：${DOMAIN}）"

echo "==> [5/5] 启动服务"
docker compose -f docker-compose.prod.yml up -d --build

echo ""
echo "✅ 部署完成！"
echo "   请在浏览器访问： https://${DOMAIN}"
echo "   首次访问需输入 Basic Auth 账号密码（${AUTH_USER} / 你设置的密码）"
echo "   进入后在设置中填入你的 B 站 SESSDATA 即可开始使用。"
echo "   查看日志： docker compose -f docker-compose.prod.yml logs -f"
