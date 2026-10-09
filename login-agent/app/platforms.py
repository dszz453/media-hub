"""平台适配：登录页地址、二维码选择器、登录成功的判定条件。

选择器与操作步骤都是在这台机器的容器里实测出来的：

  * 小红书 —— 登录页 https://www.xiaohongshu.com/login 自动渲染二维码。
    二维码是 <img class="qrcode-img" src="data:image/png;base64,...">，
    CSS 上 128x128，页面加载后约 6 秒出现。
    ⚠ 老的 .qrcode-container 已经不存在了（小红书改过 DOM），别再依赖它。

  * 抖音 —— https://www.douyin.com/ 打开后**不会**自动弹登录框，
    必须点右上角「登录」。面板出现后二维码在 #animate_qrcode_container 里，
    同样是 <img src="data:image/png;base64,...">（512x512 无损 PNG，
    CSS 上显示成 178x178）。

── 关于「怎么把二维码拿出来」──
优先**直接读 data:URI**，而不是截图。原因：

  1. 两家的二维码本来都是 data:URI，读 src 是零损失的原图
     （抖音 512x512，小红书 128x128），截图只能拿到 CSS 尺寸的位图。
  2. 这台机器（J1900 + Xvfb 软件渲染）上截图极不稳：实测 page.screenshot
     连 clip 截图都能卡满 25 秒超时；locator.screenshot 也要 6~20 秒。
     而一次 page.evaluate 虽然也要 2~7 秒（页面太重），但稳定得多。

截图路径保留为兜底。

── 选图必须带尺寸/形状过滤 ──
踩过的坑：登录页上「小红书」logo 也是 data:image（68x32，PNG 本体 205x96）。
早期版本只按「容器内面积最大的 data:image」挑，结果把 logo 当成二维码
交给前端了。所以现在**必须**同时满足：
    * 屏幕尺寸 >= 110x110
    * 宽高比在 0.8 ~ 1.25 之间（二维码是正方形）
    * class/id/alt 里不含 logo / icon / avatar / emoji / badge
"""

from __future__ import annotations

import logging
import time
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
    # 二维码候选容器，按优先级排列。同一个列表两用：
    #   快路径 —— 在容器里找符合「正方形 + >=110px」的 data:image / canvas；
    #   兜底   —— 快路径整个超时后，对这些选择器做元素截图。
    qrcode_selectors: tuple[str, ...]
    # 截图兜底时的额外候选（范围更大，可能命中还没渲染出二维码的空容器）
    qrcode_fallbacks: tuple[str, ...] = ()
    # 页面上出现这些字样说明二维码过期了
    expired_markers: tuple[str, ...] = ("已过期", "点击刷新", "重新获取", "已失效")
    # 进入页面后的准备动作（比如点开登录弹窗）
    prepare: Callable[[Page], Awaitable[None]] | None = None
    goto_timeout: int = 60_000
    qrcode_timeout: int = 40_000
    # 会话最长存活时间（秒），超时视为二维码过期
    ttl: int = 600


async def _xhs_prepare(page: Page) -> None:
    """小红书登录页默认就是「扫码登录」tab，等 Vue 挂载完即可。

    二维码大约 6 秒后才渲染出来，这里只等 2.5 秒，
    剩下的交给 _grab_qrcode 轮询（它本来就带超时）。
    """
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


# 面板出来之后，把首页的无限视频流停掉。
#
# 为什么值得做：这台机器上每次 page.evaluate 要 2~7 秒，瓶颈就是渲染进程
# 主线程被视频流占满。把 <video> 暂停并卸掉 src 之后，后面几十次轮询
# 都会快很多。失败也无所谓，纯属优化。
_FREEZE_VIDEOS = """() => {
  let n = 0;
  for (const v of document.querySelectorAll('video')) {
    try {
      v.pause();
      v.removeAttribute('src');
      v.load();
      n++;
    } catch (_) {}
  }
  return n;
}"""


# 打开抖音登录面板的总预算（秒）。
# 实测面板不是点完立刻出来：点下去之后要 15~40 秒才渲染出来，
# 机器越忙越慢。所以不能用「等 15 秒没有就放弃」这种写法。
_DOUYIN_PREPARE_BUDGET = 100.0
# 每次点击之后愿意等多久
_DOUYIN_PANEL_WAIT = 15_000
# 最多点几次（点多了没意义，反而可能把面板点关掉）
_DOUYIN_MAX_CLICKS = 5


async def _douyin_prepare(page: Page) -> None:
    """点开抖音的登录面板。

    三个坑：
      1. 不能用 locator.click()。首页是无限滚动视频流，元素永远处于
         "不稳定"状态，Playwright 的可交互性检查必然超时（实测 force=True
         也照样超时）。所以用 page.mouse.click 按坐标点，绕开这套检查。
      2. 这台机器上**每次 page.evaluate 要 2~7 秒**（渲染进程被视频流占满），
         所以循环里绝不能塞固定 sleep —— 那是纯浪费。改用
         wait_for_selector：单次 CDP 调用、在浏览器侧等，比轮询便宜得多。
      3. **面板不是点完就出来**。实测点下去之后要 15~40 秒才渲染出来。
         早期版本「点 3 次、每次只等 15 秒」会全部落空，然后拿不到二维码。
         现在改成「在 100 秒总预算内反复 点一下 / 等 15 秒」，
         什么时候出来什么时候走。
    """
    deadline = time.time() + _DOUYIN_PREPARE_BUDGET
    clicks = 0
    opened = False

    while time.time() < deadline:
        if await page.evaluate("() => !!document.querySelector('#login-panel-new')"):
            opened = True
            break

        if clicks < _DOUYIN_MAX_CLICKS:
            box = await page.evaluate(_FIND_LOGIN_BOX)
            if box:
                await page.mouse.move(box["x"], box["y"])
                await page.mouse.click(box["x"], box["y"])
                clicks += 1
                log.info("已点击「登录」按钮（第 %d 次）", clicks)
            else:
                log.info("页面上还找不到「登录」按钮")

        try:
            await page.wait_for_selector(
                "#login-panel-new", timeout=_DOUYIN_PANEL_WAIT, state="attached")
            opened = True
            break
        except Exception:  # noqa: BLE001
            log.info("等了 %ds 面板仍未出现（已点击 %d 次）",
                     _DOUYIN_PANEL_WAIT // 1000, clicks)

    if not opened:
        log.warning("准备阶段用尽 %.0fs，抖音登录面板仍未出现", _DOUYIN_PREPARE_BUDGET)
        return

    log.info("抖音登录面板已就绪（共点击 %d 次）", clicks)

    try:
        frozen = await page.evaluate(_FREEZE_VIDEOS)
        if frozen:
            log.info("已暂停首页视频流（%d 个 <video>），给后续轮询减负", frozen)
    except Exception as exc:  # noqa: BLE001
        log.info("暂停视频流失败（不影响登录）：%s", exc)


# 一次 CDP 往返，把二维码直接读成 data:URI。
# 传进来的 sels 就是 PlatformSpec.qrcode_selectors。
_QR_DATA_JS = """(sels) => {
  const MIN = 110;                 // 屏幕上至少要这么大，才可能是二维码
  const MIN_RATIO = 0.8;           // 二维码是正方形，宽高比卡在 0.8~1.25
  const MAX_RATIO = 1.25;
  const BAD = /logo|icon|avatar|emoji|badge/i;

  const okSrc = (s) => typeof s === 'string' && s.startsWith('data:image') && s.length > 500;

  const looksLikeQr = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width < MIN || r.height < MIN) return false;
    const ratio = r.width / r.height;
    if (ratio < MIN_RATIO || ratio > MAX_RATIO) return false;
    const cls = (typeof e.className === 'string' ? e.className : '') + ' ' +
                (e.id || '') + ' ' + (e.getAttribute('alt') || '');
    return !BAD.test(cls);
  };

  const pick = (root) => {
    let best = null, area = 0;
    for (const im of root.querySelectorAll('img[src^="data:image"]')) {
      if (!looksLikeQr(im)) continue;
      const s = im.getAttribute('src');
      if (!okSrc(s)) continue;
      const r = im.getBoundingClientRect();
      const a = r.width * r.height;
      if (a > area) { area = a; best = s; }
    }
    if (best) return best;
    for (const c of root.querySelectorAll('canvas')) {
      if (!looksLikeQr(c)) continue;
      try { return c.toDataURL('image/png'); } catch (_) {}
    }
    return null;
  };

  for (const sel of sels) {
    let root = null;
    try { root = document.querySelector(sel); } catch (_) { continue; }
    if (!root) continue;
    if (root.tagName === 'IMG') {
      const s = root.getAttribute('src');
      if (okSrc(s) && looksLikeQr(root)) return s;
      continue;
    }
    const hit = pick(root);
    if (hit) return hit;
  }

  // 兜底：全页找。选择器是会漂的（小红书的 .qrcode-container 就没了），
  // 有这层兜底，哪怕容器选择器全过期也还能救回来。
  return pick(document);
}"""


XHS = PlatformSpec(
    key="xhs",
    label="小红书",
    hint="打开「小红书」App → 左上角菜单 → 扫一扫",
    login_url="https://www.xiaohongshu.com/login",
    session_cookie_keys=("web_session",),
    qrcode_selectors=(
        ".qrcode-img",
        ".qrcode",
    ),
    qrcode_fallbacks=(
        ".login-container",
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
        "#animate_qrcode_container",
        "#douyin_login_comp_scan_code",
    ),
    qrcode_fallbacks=(
        # 兜底：整个登录面板。二维码在里面只有 178px，扫起来费点劲，
        # 但比截到一个空白容器强（#animate_qrcode_container 在二维码
        # 渲染出来之前就是个纯白框，截它只会得到一张白图）。
        "#login-panel-new",
    ),
    prepare=_douyin_prepare,
    # 抖音首页是无限视频流，这台机器上从打开到二维码渲染出来实测要
    # 60~140 秒（跟机器负载关系很大），所以预算给得很宽。
    qrcode_timeout=150_000,
)

PLATFORMS: dict[str, PlatformSpec] = {p.key: p for p in (XHS, DOUYIN)}
