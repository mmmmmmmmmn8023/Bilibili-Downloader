# ---- 阶段 1：构建依赖 ----
FROM python:3.13-slim AS builder

WORKDIR /app

# 仅先拷贝依赖清单，利用层缓存；安装到独立前缀便于整体拷贝
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---- 阶段 2：运行镜像 ----
FROM python:3.13-slim AS runtime

# Linux 下由 bilibili.py 自动回退到 PATH 中的 ffmpeg（明确来源为系统 apt）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 把 builder 阶段装好的依赖合并进系统路径
COPY --from=builder /install /usr/local

WORKDIR /app

# 仅拷贝运行时必需的代码，其余由 .dockerignore 排除
COPY server.py bilibili.py db.py ./
COPY static/ ./static/

EXPOSE 8000
CMD ["python", "server.py"]
