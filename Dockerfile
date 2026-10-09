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
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    TZ=Asia/Shanghai \
    DATA_DIR=/data

# apt 换腾讯云源。基础镜像 python:3.11-slim 现在是 Debian 13 (trixie)，
# 默认的 deb.debian.org 在国内下载 ffmpeg 这种大包会非常慢；
# 实测同一台机器上腾讯云约 540KB/s，比默认源快一个数量级。
# 直接重写 deb822 格式的 sources，兼容 12/13 两代。
RUN set -eux; \
    . /etc/os-release; \
    printf '%s\n' \
        'Types: deb' \
        'URIs: https://mirrors.cloud.tencent.com/debian' \
        "Suites: ${VERSION_CODENAME} ${VERSION_CODENAME}-updates" \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
        '' \
        'Types: deb' \
        'URIs: https://mirrors.cloud.tencent.com/debian-security' \
        "Suites: ${VERSION_CODENAME}-security" \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
        > /etc/apt/sources.list.d/debian.sources; \
    rm -f /etc/apt/sources.list

RUN apt-get update -o Acquire::Retries=3 \
 && apt-get install -y --no-install-recommends -o Acquire::Retries=3 \
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
