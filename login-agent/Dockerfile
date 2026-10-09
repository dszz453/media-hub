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
#  为什么必须跑「有头模式 + Xvfb」？
#    抖音的登录面板由风控 SDK（rc-verifycenter / rmc-nocaptcha）把守。
#    实测在 headless 下，rmc-nocaptcha 的 iframe 直接 ERR_ABORTED，
#    随后登录面板虽然能点出来，但 /passport/web/get_qrcode/ 永远不返回，
#    二维码区一直是个空白白框。换成 Xvfb 虚拟屏跑有头模式后，
#    风控 SDK 全部 200，二维码接口正常返回，二维码正常渲染。
#    小红书两种模式都能用，所以统一走有头模式。
#
#  为什么不用 `playwright install --with-deps`？
#    它会顺带拖进一整套 X server 依赖（112 个包 / 约 362MB），
#    而我们只需要 xvfb 这一个包。
# =============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright

# apt 换腾讯云源：实测在这台机器上，阿里云的所有服务（debian / pypi）
# 都被限速到 50~90 KB/s，腾讯云能跑到 15~25 MB/s，差两个数量级。
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

# Chromium 运行时依赖 + xvfb（有头模式需要虚拟屏）。
# fonts-noto-cjk 保留：登录页大量中文，缺字体会影响页面渲染与截图。
RUN apt-get update -o Acquire::Retries=3 \
 && apt-get install -y --no-install-recommends -o Acquire::Retries=3 \
        libnss3 libnspr4 \
        libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
        libcups2 libdrm2 libdbus-1-3 libgbm1 libglib2.0-0 \
        libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
        libxkbcommon0 libxrandr2 \
        libpango-1.0-0 libcairo2 libasound2 \
        libudev1 libvulkan1 \
        xvfb \
        fonts-liberation fonts-noto-cjk \
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

# 用 xvfb-run 起一块虚拟屏，Chromium 在里面跑有头模式。
#   -a  自动挑一个空闲 display
#   -s  指定 24 位色深（xvfb 默认 8 位，页面会渲染成灰的）
CMD ["xvfb-run", "-a", "-s", "-screen 0 1366x900x24 -nolisten tcp", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
