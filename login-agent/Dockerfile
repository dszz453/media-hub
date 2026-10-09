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
#  关于基础镜像：python:3.11-slim 现在指向 Debian 13 (trixie)。
#  下面依赖用的是不带 t64 后缀的老包名，trixie 上由同名虚拟包提供，
#  apt 能正常解析（已实测通过）。这样写 12 / 13 两代都通用。
#
#  为什么不用 `playwright install --with-deps`？
#    它会顺带拖进 xvfb 等一整套 X server（112 个包 / 约 362MB），
#    headless 场景根本用不到，纯属浪费构建时间与镜像体积。
# =============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright

# apt 换腾讯云源：实测这台机器腾讯云约 540KB/s，阿里云只有 87KB/s。
# 直接重写 deb822 格式的 sources，比 sed 更稳（不同基础镜像里
# 可能是 /etc/apt/sources.list，也可能是 sources.list.d/debian.sources）。
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

# Chromium headless 运行时依赖。
# 刻意不含 fonts-noto-cjk（约 57MB）——我们只从页面里取二维码图片，
# 不截图，中文字体渲染成豆腐块也不影响功能。
# 也刻意不用 libu2f-udev，它会连带装进整个 systemd/udev（约 8MB），
# 这里只需要 libudev1 这个动态库。
RUN apt-get update -o Acquire::Retries=3 \
 && apt-get install -y --no-install-recommends -o Acquire::Retries=3 \
        libnss3 libnspr4 \
        libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
        libcups2 libdrm2 libdbus-1-3 libgbm1 libglib2.0-0 \
        libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
        libxkbcommon0 libxrandr2 \
        libpango-1.0-0 libcairo2 libasound2 \
        libudev1 libvulkan1 \
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
