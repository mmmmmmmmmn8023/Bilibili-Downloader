FROM python:3.13-slim

# 安装系统 FFmpeg（Linux 下由 bilibili.py 自动回退到 PATH 中的 ffmpeg）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "server.py"]
