"""平台适配：登录页地址、二维码选择器、登录成功的判定条件。

选择器都是实测出来的：
  * 小红书 —— 登录页 https://www.xiaohongshu.com/login 会自动渲染二维码，
    Vue scoped 样式里定义了 .qrcode-container(200x200) / .qrcode-img。
  * 抖音 —— https://www.douyin.com/ 加载后会自动弹出登录面板 #login-panel-new，
    二维码是面板内的一个 <canvas>。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from playwright.async_api import Page

log = logging.getLogger("login-agent.platforms")


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    label: str
    hint: str
    login_url: str
    # 出现其中任意一个 cookie 且值非空，就认为登录成功
    session_cookie_keys: tuple[str, ...]
    # 二维码元素候选选择器，按优先级尝试
    qrcode_selectors: tuple[str, ...]
    # 页面上出现这些字样说明二维码过期了
    expired_markers: tuple[str, ...] = ("已过期", "点击刷新", "重新获取", "已失效")
    # 进入页面后的准备动作（比如点开登录弹窗）
    prepare: Callable[[Page], Awaitable[None]] | None = None
    goto_timeout: int = 60_000
    qrcode_timeout: int = 35_000
    # 会话最长存活时间（秒），超时视为二维码过期
    ttl: int = 300


async def _xhs_prepare(page: Page) -> None:
    """小红书登录页默认就是「扫码登录」tab，等 Vue 挂载完即可。"""
    await page.wait_for_timeout(2500)


async def _douyin_prepare(page: Page) -> None:
    """抖音首页一般会自动弹登录框；万一没弹，就点一下右上角「登录」。"""
    try:
        await page.locator("#login-panel-new").first.wait_for(state="visible", timeout=9000)
        log.info("抖音登录面板已自动弹出")
        return
    except Exception:  # noqa: BLE001
        log.info("抖音未自动弹出登录面板，尝试点击登录按钮")

    for sel in ("button:has-text('登录')", "text=登录", "[data-e2e='login-button']"):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=4000)
                log.info("已点击登录入口: %s", sel)
                break
        except Exception:  # noqa: BLE001
            continue
    await page.wait_for_timeout(2500)


XHS = PlatformSpec(
    key="xhs",
    label="小红书",
    hint="打开「小红书」App → 左上角菜单 → 扫一扫",
    login_url="https://www.xiaohongshu.com/login",
    session_cookie_keys=("web_session",),
    qrcode_selectors=(
        ".qrcode-container",
        ".qrcode-img",
        "img[src^='data:image']",
    ),
    prepare=_xhs_prepare,
)

DOUYIN = PlatformSpec(
    key="douyin",
    label="抖音",
    hint="打开「抖音」App → 左上角 → 扫一扫",
    login_url="https://www.douyin.com/",
    session_cookie_keys=("sessionid", "sid_tt", "sessionid_ss"),
    qrcode_selectors=(
        "#login-panel-new canvas",
        "#login-panel-new img[src^='data:image']",
        "[class*='login-panel'] canvas",
        "canvas",
    ),
    prepare=_douyin_prepare,
)

PLATFORMS: dict[str, PlatformSpec] = {p.key: p for p in (XHS, DOUYIN)}
