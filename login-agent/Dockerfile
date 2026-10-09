# =============================================================================
#  MediaHub 登录代理（login-agent）
#
#  为什么需要单独一个容器跑浏览器？
#    1. 小红书 web 接口的 x-s 签名算法持续升级。实测 xhs==0.2.13 的纯 Python
#       签名在 /api/sns/web/v1/login/* 上稳定返回 406（它用自定义字符表编码，
#       而线上是 XYS_ 前缀的新算法）。逆向跟进成本高、且随时会失效。
#    2. 抖音 passport 接口有 JSVM(argus) 风控，get_qrcode 直接返回一段 JS
#       验证页而不是 JSON，必须由真实浏览器执行 JS 生成 __ac_signature 指纹。
#    两者都只能靠真实浏览器，于是把 Chromium 单独隔离在这个容器里，
#    主服务（media-hub）保持轻量。
#
#  两点刻意的选择：
#    * 基础镜像钉死 bookworm。python:3.11-slim 现在指向 Debian 13(trixie)，
#      包名带 t64 后缀，容易踩坑；bookworm 的依赖名长期稳定。
#    * 不用 `playwright install --with-deps`。它会在 trixie 上拖进来
#      xvfb 等 112 个包、约 362MB，而 headless 场景根本不需要 X server。
# =============================================================================
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright

# apt 走阿里云镜像加速
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list \
 && sed -i 's|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list

# Chromium headless 运行时依赖（Debian 12/bookworm 包名）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 \
        libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
        libcups2 libdrm2 libdbus-1-3 libgbm1 libglib2.0-0 \
        libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
        libxkbcommon0 libxrandr2 \
        libpango-1.0-0 libcairo2 libasound2 \
        libu2f-udev libvulkan1 \
        fonts-liberation \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install chromium

COPY app ./app

# 非 root 运行（Chromium 启动参数里带 --no-sandbox）
RUN useradd -m -u 1000 -s /usr/sbin/nologin pwuser \
 && chown -R pwuser:pwuser /app /ms-playwright
USER pwuser

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8100/health',timeout=5); \
sys.exit(0 if r.status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
