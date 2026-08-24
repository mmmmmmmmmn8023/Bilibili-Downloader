# 部署指南（公网 + 域名 + 应用内账号鉴权）

本文档说明如何把 Bilibili-Downloader 部署到一台**带公网 IP 的 Linux 服务器**，并通过反向代理对外提供 HTTPS 访问。

---

## 一、为什么必须加 HTTPS 反代

本项目自带 Web 管理界面已内置**自建账号登录鉴权**（网页注册/登录，账号存于 `bili_history.db` 的 `users` 表），但 `server.py` 监听的 `8000` 端口**未加密**。如果直接把 `8000` 端口映射到公网，凭证会以明文传输、有被嗅探的风险。

因此公网部署时，必须在前面加一层**反向代理**：

- **HTTPS**：加密传输，避免 `SESSDATA` 等凭证在公网上被窃听
- **应用内账号鉴权**：由 `server.py` 自建账号体系负责登录校验，Caddy 不做额外鉴权
- **端口收敛**：应用本身只监听内部网络，公网只暴露 80/443

本方案使用 [Caddy](https://caddyserver.com/) 作为反向代理——它能**自动向 Let's Encrypt 申请与续期证书**，省去手动配置 certbot 的麻烦。

---

## 二、架构

```
浏览器
  │  HTTPS (443)  + 应用内账号鉴权
  ▼
[Caddy 反向代理]  ← 对外 80/443（80 仅用于证书验证），仅做 HTTPS 反代
  │  反向代理 http://app:8000
  ▼
[app 容器]  ← 仅内部暴露 8000（不映射公网），自建账号体系负责登录
  │
  ▼
持久化卷：downloads / config.json / bili_history.db / logs
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
vim .env              # 填 DOMAIN / ADMIN_EMAIL

# 4. 一键部署
chmod +x deploy.sh
./deploy.sh

# 5. 浏览器打开 https://你的域名
#    首次需注册账号 → 登录后在「设置」填入你的 B 站 SESSDATA
```

`deploy.sh` 会自动完成：检查 Docker → 读取 `.env` → 渲染并生成 `Caddyfile` → **本地 `docker compose build` 构建 app 镜像**并启动容器。

> **镜像来源**：本项目**不使用 GHCR**。`app` 镜像由服务器本地 `docker compose build` 基于仓库内 `Dockerfile` 构建（`docker-compose.prod.yml` 中 `build: .`），无需云端构建或拉取远程镜像。CI（`build.yml`）仅做代码质量门禁，不参与镜像构建。

---

## 五、运维命令

```bash
# 查看日志
docker compose -f docker-compose.prod.yml logs -f

# 停止
docker compose -f docker-compose.prod.yml down

# 更新（拉取最新代码后重新部署；镜像由服务器本地构建）
git pull
./deploy.sh

# 手动重新构建并重启（不依赖 CI）
docker compose -f docker-compose.prod.yml up -d --build

# 管理应用内账号
#   账号密码在网页内注册 / 登录管理（数据存于 bili_history.db 的 users 表）

# 查看证书状态
docker compose -f docker-compose.prod.yml exec caddy caddy list-certificates
```

> 切勿删除 `caddy_data` 卷，否则证书丢失需重新申请（可能触发 Let's Encrypt 频率限制）。

---

## 六、安全要点

- **SESSDATA 是账号凭证**：仅在你自己的浏览器会话中填写，不要泄露给他人
- **应用内账号密码务必强**：这是你服务的访问凭证；建议 16 位以上随机串
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
- 可以。用任意反向代理监听 443 并配置 HTTPS，反代到 `http://app:8000` 即可（鉴权由 app 内自建账号体系负责，反代层无需额外配置）。本仓库默认提供 Caddy 方案。

---

## 八、相关文件

| 文件 | 作用 |
| --- | --- |
| `docker-compose.prod.yml` | 生产编排：app（服务器本地 `build: .` 构建）内部 + caddy 反代 |
| `Caddyfile.template` | Caddy 配置模板（含占位符，由 deploy.sh 渲染） |
| `.env.example` | 配置模板（DOMAIN / ADMIN_EMAIL） |
| `deploy.sh` | 一键部署脚本（读 .env → 渲染 Caddyfile → 本地 build 镜像并启动 + 健康检查） |
| `.github/workflows/build.yml` | CI：推送时做代码质量门禁（语法/导入检查），不参与镜像构建 |
| `.github/workflows/deploy.yml` | CI：通过 SSH 私钥登录服务器，调用 `deploy-bilibili.sh` 执行部署 |
| `deploy/deploy-bilibili.sh` | 服务器侧部署脚本（SSH 调用：`git pull` + `./deploy.sh` 本地构建并重启） |
| `.gitignore` | 已排除真实 `.env` 与生成的 `Caddyfile` |

> **GitHub Actions 部署所需的仓库 Secrets**（`Settings → Secrets and variables → Actions`）：
> - `SSH_HOST` / `SSH_USER`：服务器地址与登录用户（必填）
> - `SSH_PRIVATE_KEY`：部署用的 **SSH 私钥**（必填，公钥须预先写入服务器 `~/.ssh/authorized_keys`）
> - `SSH_PORT`（选填，默认 22）、`DEPLOY_SCRIPT`（选填，默认 `/var/www/deploy-bilibili.sh`）
>
> 私钥生成：在服务器执行 `ssh-keygen -t ed25519`（passphrase 留空），
> 把 `id_ed25519.pub` 追加进服务器 `authorized_keys`，将 `id_ed25519` 全文存入 `SSH_PRIVATE_KEY`。
> 登录采用 ssh-agent 注入私钥，不使用密码，比明文密码更安全。
> 注意：本仓库为公共仓库，服务器 `git pull` 无需认证（见第九节 cron 方案）。

---

## 九、服务器自动拉取更新（cron 定时部署）

仓库为**公共仓库**，服务器无需任何密钥即可 `git pull`。用 cron 让服务器**自己定时拉取最新代码并重新部署**，不依赖 GitHub Actions / SSH 反向连接。

### 1. 服务器首次准备

```bash
# 1) 克隆仓库（公共仓库，无需认证）
git clone https://github.com/mmmmmmmmmn8023/Bilibili-Downloader.git /var/www/Bilibili-Downloader
cd /var/www/Bilibili-Downloader
git checkout develop/feature

# 2) 准备运行配置（deploy.sh 会读取 .env 并渲染 Caddyfile）
cp .env.example .env
vim .env                            # 填 DOMAIN / ADMIN_EMAIL

# 3) 首次部署
chmod +x deploy.sh deploy/deploy-bilibili.sh
./deploy.sh

# 4) 把部署脚本放到项目目录之外（避免 git pull 时冲突），并指向项目目录
cp deploy/deploy-bilibili.sh /var/www/deploy-bilibili.sh
chmod +x /var/www/deploy-bilibili.sh
```

### 2. 配置 cron 定时任务

```bash
crontab -e
```

加入（每 5 分钟拉取一次并重新部署）：

```cron
*/5 * * * * cd /var/www/Bilibili-Downloader && git pull --ff-only origin develop/feature && /var/www/deploy-bilibili.sh >> /var/log/bili-deploy.log 2>&1
```

> 说明：`git pull --ff-only` 只接受快进合并；拉到新代码后调用 `deploy-bilibili.sh`（内部执行 `./deploy.sh` 重新生成 Caddyfile 并 `docker compose up -d --build`）。如只想拉代码不重建容器，去掉 `&& /var/www/deploy-bilibili.sh` 即可。

### 3. 验证

```bash
crontab -l                                  # 确认任务已注册
tail -f /var/log/bili-deploy.log            # 观察下次触发输出
git push origin develop/feature             # 本地推送后，最多 5 分钟服务器自动更新
```

### 4. 注意事项

- **凭证安全**：`.env`、`config.json` 均已被 `.gitignore` 排除，不会随代码提交。
- **不要本地改动被跟踪文件**：`git pull --ff-only` 只接受快进；若服务器本地改了被跟踪文件，会导致 pull 失败，需先 `git checkout .` 或 `git stash` 处理。
- **频率权衡**：5 分钟一次足够及时；如资源敏感可改为每小时 `0 * * * *`。

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
- **本单元只负责开机拉起容器**；日常代码更新由第九节的 cron 定时 `git pull` + `deploy-bilibili.sh` 完成，两者互补。
- 若你的 Docker 本身已 `systemctl enable docker`（多数云镜像默认），且只靠 `restart: unless-stopped` 也能满足需求，本单元为可选增强。
