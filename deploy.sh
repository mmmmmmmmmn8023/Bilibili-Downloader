#!/usr/bin/env bash
#
# Bilibili-Downloader 生产部署脚本（公网 + 域名）
# 拓扑：Caddy(443 HTTPS 反代) → app(内部 8000)
# 鉴权：应用内自建账号体系（/api/login），Caddy 不再做 Basic Auth
#
set -euo pipefail

# 健康检查轮询参数
HEALTH_RETRIES=30
HEALTH_INTERVAL=2

echo "==> [1/7] 检查 Docker / Docker Compose 插件"
if ! command -v docker >/dev/null 2>&1; then
  echo "错误：未检测到 docker，请先安装 Docker（https://docs.docker.com/get-docker/）"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "错误：未检测到 docker compose 插件，请安装 Docker Compose v2"
  exit 1
fi

echo "==> [2/7] 读取 .env 配置"
if [ ! -f .env ]; then
  echo "错误：未找到 .env，请先执行  cp .env.example .env  并填写"
  exit 1
fi
set -a
# shellcheck disable=SC1091
source ./.env
set +a

: "${DOMAIN:?请在 .env 中设置 DOMAIN}"
: "${ADMIN_EMAIL:?请在 .env 中设置 ADMIN_EMAIL}"

# 确保被挂载的运行时文件存在（否则 Docker 会建成目录，导致应用写入失败）
touch config.json 2>/dev/null || true

echo "==> [3/7] 预检 80 / 443 端口是否被占用"
for port in 80 443; do
  # 排除本项目自己的 caddy 容器（bilibili-caddy）：
  # 重复部署时它本就占着 80/443，属正常情况，不应判定为冲突
  conflict=$(docker ps --format '{{.Names}}\t{{.Ports}}' | grep ":${port}->" | grep -v '^bilibili-caddy' || true)
  if [ -n "$conflict" ]; then
    echo "警告：检测到已有容器占用了宿主机 ${port} 端口，可能与 Caddy 冲突。"
    echo "$conflict" | awk '{print "       冲突容器: " $1 "  端口: " $2}'
    echo "       请先停止冲突容器（docker ps 查看），否则 Caddy 启动会失败。"
    if [ "${CI:-}" = "true" ] || [ ! -t 0 ]; then
      echo "错误：当前为非交互环境（CI），无法等待确认，已终止部署。"
      echo "       请先在服务器上执行 docker ps 停掉占用端口的容器，再重新部署。"
      exit 1
    fi
    read -r -p "是否仍要继续部署？(y/N) " answer
    if [ "${answer:-N}" != "y" ] && [ "${answer:-N}" != "Y" ]; then
      echo "已取消部署。"
      exit 1
    fi
  fi
done

echo "==> [4/7] 拉取基础镜像（确保使用最新安全补丁）"
docker compose -f docker-compose.prod.yml pull caddy

echo "==> [5/7] 生成 Caddyfile（仅 HTTPS 反代，鉴权由应用内账号体系负责）"
sed -e "s|__DOMAIN__|${DOMAIN}|g" \
    -e "s|__ADMIN_EMAIL__|${ADMIN_EMAIL}|g" \
    Caddyfile.template > Caddyfile
echo "    Caddyfile 已生成（域名：${DOMAIN}）"

echo "==> [7/7] 构建镜像并启动服务"
# app 镜像由服务器本地 docker compose build 构建（不使用 GHCR）
docker compose -f docker-compose.prod.yml up -d --build

# ---- 部署后健康检查 ----
echo ""
echo "==> 等待 app 服务就绪（最多 $((HEALTH_RETRIES * HEALTH_INTERVAL)) 秒）..."
ready=0
for ((i=1; i<=HEALTH_RETRIES; i++)); do
  status=$(docker inspect -f '{{.State.Health.Status}}' bilibili-downloader 2>/dev/null || echo "no-healthcheck")
  if [ "$status" = "healthy" ]; then
    ready=1
    break
  fi
  printf "   [%s/%s] app 状态：%s，等待中...\n" "$i" "$HEALTH_RETRIES" "$status"
  sleep "$HEALTH_INTERVAL"
done

echo ""
if [ "$ready" -eq 1 ]; then
  echo "✅ 部署完成，app 已就绪！"
  echo "   请在浏览器访问： https://${DOMAIN}"
  echo "   使用自建账号登录（首次需注册）；登录后在「设置 → Cookie 设置」填入你的 B 站 SESSDATA 即可开始使用。"
else
  echo "⚠️  app 在规定时间内未通过健康检查，请排查："
  echo "     查看 app 日志： docker compose -f docker-compose.prod.yml logs -f app"
  echo "     查看 caddy 日志： docker compose -f docker-compose.prod.yml logs -f caddy"
fi
echo "   查看全部日志： docker compose -f docker-compose.prod.yml logs -f"
