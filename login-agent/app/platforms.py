"""平台适配：登录页地址、二维码选择器、登录成功的判定条件。

选择器与操作步骤都是在这台机器的容器里实测出来的：
  * 小红书 —— 登录页 https://www.xiaohongshu.com/login 会自动渲染二维码，
    Vue scoped 样式里定义了 .qrcode-container(200x200) / .qrcode-img。
  * 抖音 —— https://www.douyin.com/ 打开后**不会**自动弹登录框，
    必须点右上角「登录」。而且实测要点 2~3 次面板才出来，
    所以 prepare 里做循环重试。
    面板出现后二维码在 #animate_qrcode_container 里，
    是一张 178x178 的 <img src="data:image/png;base64,...">，
    通常还要再等十几秒才渲染出来。
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
    # 「精确」的二维码选择器：命中的一定是二维码本体，优先一直等这些
    qrcode_selectors: tuple[str, ...]
    # 「兜底」选择器：范围更大，可能命中还没渲染出二维码的空容器，
    # 所以只在最后关头才试（详见 sessions._grab_qrcode）
    qrcode_fallbacks: tuple[str, ...] = ()
    # 页面上出现这些字样说明二维码过期了
    expired_markers: tuple[str, ...] = ("已过期", "点击刷新", "重新获取", "已失效")
    # 进入页面后的准备动作（比如点开登录弹窗）
    prepare: Callable[[Page], Awaitable[None]] | None = None
    goto_timeout: int = 60_000
    qrcode_timeout: int = 40_000
    # 会话最长存活时间（秒），超时视为二维码过期
    ttl: int = 300


async def _xhs_prepare(page: Page) -> None:
    """小红书登录页默认就是「扫码登录」tab，等 Vue 挂载完即可。"""
    await page.wait_for_timeout(2500)


# 找出右上角「登录」元素的屏幕中心坐标。
# 取宽度最大的那个 —— 页面上有若干处「登录」文字，最大的那个才是按钮。
_FIND_LOGIN_BOX = """() => {
  const els = [...document.querySelectorAll('button,div,span')]
    .filter(e => e.innerText && e.innerText.trim() === '登录');
  if (!els.length) return null;
  els.sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width);
  const r = els[0].getBoundingClientRect();
  if (r.width < 20 || r.height < 15) return null;
  return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
}"""


async def _douyin_prepare(page: Page) -> None:
    """点开抖音的登录面板。

    两个坑：
      1. 不能用 locator.click()。首页是无限滚动视频流，元素永远处于
         "不稳定"状态，Playwright 的可交互性检查必然超时（实测 force=True
         也照样超时）。所以用 page.mouse.click 按坐标点，绕开这套检查。
      2. 点一次往往不够，实测要点 2~3 次面板才出来，所以循环重试。
    """
    if await page.evaluate("() => !!document.querySelector('#login-panel-new')"):
        log.info("抖音登录面板已自动弹出")
        return

    for attempt in range(1, 6):
        box = await page.evaluate(_FIND_LOGIN_BOX)
        if not box:
            log.info("第 %d 次：页面上还找不到「登录」按钮，稍等", attempt)
            await page.wait_for_timeout(2000)
            continue

        await page.mouse.move(box["x"], box["y"])
        await page.wait_for_timeout(300)
        await page.mouse.click(box["x"], box["y"])
        await page.wait_for_timeout(3500)

        if await page.evaluate("() => !!document.querySelector('#login-panel-new')"):
            log.info("抖音登录面板已打开（第 %d 次点击）", attempt)
            return

    log.warning("点了 5 次仍未出现抖音登录面板，交给后面的选择器兜底")


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
    hint="打开「抖音」App → 右上角「+」→ 扫一扫",
    login_url="https://www.douyin.com/",
    session_cookie_keys=("sessionid", "sid_tt", "sessionid_ss"),
    qrcode_selectors=(
        # 二维码本体：178x178 的 base64 PNG
        "#animate_qrcode_container img[src^='data:image']",
        "#animate_qrcode_container canvas",
        "#douyin_login_comp_scan_code img[src^='data:image']",
    ),
    qrcode_fallbacks=(
        # 兜底：整个登录面板。二维码在里面只有 178px，扫起来费点劲，
        # 但比截到一个空白容器强（#animate_qrcode_container 在二维码
        # 渲染出来之前就是个纯白框，截它只会得到一张白图）。
        "#login-panel-new",
    ),
    prepare=_douyin_prepare,
    # 抖音从打开首页到二维码渲染出来要 40 秒上下，比小红书慢不少
    qrcode_timeout=60_000,
)

PLATFORMS: dict[str, PlatformSpec] = {p.key: p for p in (XHS, DOUYIN)}
