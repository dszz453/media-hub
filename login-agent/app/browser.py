"""Chromium 池：常驻一个浏览器实例，每个扫码会话各开一个 context。

常驻而不是每次新建，是因为 Chromium 冷启动要 5~15 秒，
在路由器这种小机器上尤其明显；context 之间互相隔离，
关掉 context 就等于「退出登录」，互不影响。

跑的是**有头模式**（headless=False），靠容器启动时 xvfb-run 提供的
虚拟屏显示。原因见 Dockerfile：抖音的风控 SDK 在 headless 下会拒绝
下发二维码，有头模式才能过。
"""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

log = logging.getLogger("login-agent.browser")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 容器里以非 root 运行，必须 --no-sandbox；
# --disable-dev-shm-usage 是因为 Docker 默认 /dev/shm 只有 64MB，Chromium 会崩。
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--window-size=1366,900",
]


class BrowserPool:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        try:
            return self._browser is not None and self._browser.is_connected()
        except Exception:  # noqa: BLE001
            return False

    async def start(self) -> None:
        async with self._lock:
            if self.ready:
                return
            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=False, args=LAUNCH_ARGS
            )
            log.info("chromium 已就绪（有头模式）: %s", self._browser.version)

    async def stop(self) -> None:
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._browser = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None

    async def new_context(self) -> BrowserContext:
        if not self.ready:
            await self.start()
        assert self._browser is not None
        context = await self._browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            device_scale_factor=1,
        )
        # 抹掉最明显的自动化特征，降低被风控直接拒的概率
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        return context


pool = BrowserPool()
