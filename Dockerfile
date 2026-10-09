FROM python:3.11-slim

# ---------------------------------------------------------------------------
# 基础环境
#   ffmpeg   —— 抽音轨 / 转码 / 写封面标签，核心依赖
#   tini     —— 让 PID 1 正确回收子进程（yt-dlp、ffmpeg 都是子进程）
# ---------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用 Docker 层缓存：改业务代码不用重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --upgrade yt-dlp

COPY app ./app

# 非 root 运行，数据卷目录提前建好
RUN useradd -m -u 1000 -s /usr/sbin/nologin appuser \
 && mkdir -p /data/cache /data/tmp \
 && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
