# 部署指南（公网 + 域名 + 鉴权）

本文档说明如何把 Bilibili-Downloader 部署到一台**带公网 IP 的 Linux 服务器**，并通过反向代理对外提供 HTTPS 访问。

---

## 一、为什么必须加鉴权

本项目自带的 Web 管理界面**没有内置登录功能**，且需要填入你的 B 站 `SESSDATA`（账号登录态）。如果直接把 `8000` 端口映射到公网，任何人都能打开界面、操作下载、并拿到你的账号凭证。

因此公网部署时，必须在前面加一层**反向代理 + 访问控制**：

- **HTTPS**：加密传输，避免凭证在公网上被窃听
- **Basic Auth（账号密码）**：公网访问的唯一防线，挡住未授权访问
- **端口收敛**：应用本身只监听内部网络，公网只暴露 80/443

本方案使用 [Caddy](https://caddyserver.com/) 作为反向代理——它能**自动向 Let's Encrypt 申请与续期证书**，省去手动配置 certbot 的麻烦。

---

## 二、架构

```
浏览器
  │  HTTPS (443) + Basic Auth
  ▼
[Caddy 反向代理]  ← 对外 80/443（80 仅用于证书验证）
  │  反向代理 http://app:8000
  ▼
[app 容器]  ← 仅内部暴露 8000（不映射公网）
  │
  ▼
持久化卷：downloads / config.json / download_history.json / logs
```

---

## 三、前置条件

- 一台带**公网 IP** 的 Linux 服务器（x86_64 / aarch64 均可），已安装 **Docker** 与 **Docker Compose v2**
- 一个域名，已将 **A 记录**解析到该服务器的公网 IP
- 安全组 / 防火墙已放行 **TCP 80 与 443** 入方向
- 若使用**中国大陆节点**，域名必须已完成 **ICP 备案**，否则 80/443 会被运营商阻断（临时可改用非标准端口，但不推荐）

---

## 四、部署步骤

```bash
# 1. 登录服务器
ssh root@你的服务器公网IP

# 2. 获取项目（部署文件位于 develop/feature 分支）
git clone https://github.com/mmmmmmmmmn8023/Bilibili-Downloader.git
cd Bilibili-Downloader
git checkout develop/feature

# 3. 准备配置
cp .env.example .env
vim .env              # 填 DOMAIN / AUTH_USER / AUTH_PASS / ADMIN_EMAIL

# 4. 一键部署
chmod +x deploy.sh
./deploy.sh

# 5. 浏览器打开 https://你的域名
#    输入 Basic Auth 账号密码 → 进入后在「设置」填入你的 B 站 SESSDATA
```

`deploy.sh` 会自动完成：检查 Docker → 读取 `.env` → 用 caddy 镜像生成密码哈希 → 生成 `Caddyfile` → 构建并启动容器。

---

## 五、运维命令

```bash
# 查看日志
docker compose -f docker-compose.prod.yml logs -f

# 停止
docker compose -f docker-compose.prod.yml down

# 更新（拉取最新代码后重新构建）
git pull
./deploy.sh

# 修改 Basic Auth 密码
vim .env              # 改 AUTH_PASS
./deploy.sh           # 重新生成哈希与 Caddyfile 并重建 caddy

# 查看证书状态
docker compose -f docker-compose.prod.yml exec caddy caddy list-certificates
```

> 切勿删除 `caddy_data` 卷，否则证书丢失需重新申请（可能触发 Let's Encrypt 频率限制）。

---

## 六、安全要点

- **SESSDATA 是账号凭证**：仅在你自己的浏览器会话中填写，不要泄露给他人
- **Basic Auth 密码务必强**：这是公网唯一防线；建议 16 位以上随机串
- **证书卷 `caddy_data` 不要删**：删了要重新申请证书
- **端口收敛**：应用容器只 `expose 8000`（内部），公网只经 Caddy 的 80/443

---

## 七、常见问题（FAQ）

**Q：浏览器打不开 / 连接超时？**
- 检查安全组是否放行 80/443；确认域名 A 记录已生效（`ping 你的域名`）
- 中国大陆未备案域名会被阻断，需先完成 ICP 备案或临时改用非标准端口

**Q：证书申请失败（ACME 报错）？**
- Caddy 首次申请需 **80 端口公网可达**；确认 80 未被其他程序占用、未被防火墙挡住
- 检查 `ADMIN_EMAIL` 是否填写正确
- 频繁重试可能触发 Let's Encrypt 频率限制，稍后再试

**Q：想用自己的证书（自有 / 通配符）？**
- 把证书与私钥放到服务器，修改 `Caddyfile` 使用 `tls /path/fullchain.pem /path/privkey.pem` 替换自动申请；再 `docker compose -f docker-compose.prod.yml up -d caddy` 重建 caddy

**Q：不想用 Caddy，可以用 Nginx 吗？**
- 可以。用任意反向代理监听 443 并配置 HTTPS + Basic Auth，反代到 `http://app:8000` 即可（app 仍只暴露内部 8000）。本仓库默认提供 Caddy 方案。

---

## 八、相关文件

| 文件 | 作用 |
| --- | --- |
| `docker-compose.prod.yml` | 生产编排：app 内部 + caddy 反代 |
| `Caddyfile.template` | Caddy 配置模板（含占位符，由 deploy.sh 渲染） |
| `.env.example` | 配置模板（DOMAIN / AUTH_USER / AUTH_PASS / ADMIN_EMAIL） |
| `deploy.sh` | 一键部署脚本（读 .env → 生成哈希 → 渲染 Caddyfile → 启动 + 健康检查） |
| `deploy/deploy-bilibili.sh` | 服务器侧部署脚本模板（由 GitHub Actions 经 SSH 调用） |
| `.gitignore` | 已排除真实 `.env` 与生成的 `Caddyfile` |

---

## 九、GitHub Actions 自动部署（push 即上线）

仓库已内置 `.github/workflows/deploy.yml`：当你 **push 到 `develop/feature` 分支**（或在 Actions 页面手动 Run）时，GitHub 会自动 SSH 登录你的服务器并执行部署脚本，**无需手动登服务器 `git pull`**。

### 1. 服务器准备

```bash
# 1) 安装并启用 Docker + compose 插件（略，参见第三节）

# 2) 把 deploy/deploy-bilibili.sh 放到项目目录【之外】，例如 /var/www/
cp deploy/deploy-bilibili.sh /var/www/deploy-bilibili.sh
chmod +x /var/www/deploy-bilibili.sh

# 3) 首次准备运行配置（deploy.sh 会读取 .env 并渲染 Caddyfile）
cd /var/www/Bilibili-Downloader   # 首次 push 后由脚本自动克隆生成
cp .env.example .env
vim .env                            # 填 DOMAIN / AUTH_USER / AUTH_PASS / ADMIN_EMAIL
```

> 脚本逻辑：目录不存在则 `git clone`，已存在则 `git pull --ff-only`，随后调用 `./deploy.sh`（后者负责生成 Caddyfile 并启动）。**不要**改成直接 `docker compose up`，否则 Caddyfile 不会生成、Caddy 启动失败。

### 2. 配置 GitHub Secrets

在仓库 **Settings → Secrets and variables → Actions** 中添加：

| Secret 名 | 值 | 说明 |
| --- | --- | --- |
| `SERVER_HOST` | 服务器公网 IP 或域名 | SSH 目标 |
| `SERVER_USER` | SSH 登录用户名 | 如 `root` 或普通用户 |
| `SSH_PRIVATE_KEY` | 私钥（对应服务器 `~/.ssh/authorized_keys` 里的公钥） | 用于免密登录 |

若 SSH 端口不是 22，请在 `.github/workflows/deploy.yml` 的 `port` 字段修改。

### 3. 验证

```bash
git push origin develop/feature
# 到 GitHub → Actions 标签页查看 Deploy to Server 是否绿勾
# 浏览器打开 https://你的域名 验证可访问
```

### 4. 注意事项

- **凭证安全**：`.env`、`config.json`、`download_history.json` 均已被 `.gitignore` 排除，不会随代码提交；SSH 私钥只存在 GitHub Secrets，不进仓库。
- **首次部署需手动准备 `.env`**：自动部署脚本依赖服务器上已存在的 `.env`，首次请按上面第 1 步手动创建。
- **不要本地改动被跟踪文件**：`git pull --ff-only` 只接受快进；若服务器本地改了被跟踪文件，会导致 pull 失败，需先处理再触发部署。

---

## 十、开机自启（systemd）

上面 `restart: unless-stopped` 只在 **Docker 守护进程已运行**时让容器自愈；若服务器断电重启、Docker 自身未设开机启动，则服务不会自动起来。用 systemd 单元兜住这一层：开机后自动 `docker compose up -d`。

### 1. 安装单元

仓库内置模板 `deploy/bilibili-downloader.service`，在服务器上：

```bash
sudo cp deploy/bilibili-downloader.service /etc/systemd/system/bilibili-downloader.service
# 确认 WorkingDirectory 与 deploy-bilibili.sh 的 DIR 一致（默认 /var/www/Bilibili-Downloader）
sudo systemctl daemon-reload
sudo systemctl enable bilibili-downloader     # 设为开机自启
sudo systemctl start  bilibili-downloader     # 立即启动（等同 docker compose up -d）
```

### 2. 常用命令

```bash
systemctl status bilibili-downloader          # 查看状态
systemctl stop  bilibili-downloader           # 停止（会 docker compose down）
systemctl disable bilibili-downloader         # 取消开机自启
```

### 3. 说明

- 单元 `After/Requires=docker.service`，确保 Docker 先就绪。
- 类型 `oneshot` + `RemainAfterExit`：拉起 compose 后即视为"运行中"，不常驻进程。
- **本单元只负责开机拉起**，日常代码更新仍由 GitHub Actions 自动部署（`deploy-bilibili.sh`）完成，两者互补。
- 若你的 Docker 本身已 `systemctl enable docker`（多数云镜像默认），且只靠 `restart: unless-stopped` 也能满足需求，本单元为可选增强。
