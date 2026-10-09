"""扫码会话管理。

生命周期：
    create()  -> 开 context、打开登录页、截二维码，返回给前端展示
    poll()    -> 前端每 1.5 秒轮询一次；检测到登录 cookie 即置为 success
    drop()    -> 关闭 context，等价于「退出这个临时会话」

会话只在内存里，进程重启就没了 —— 这是刻意的：
扫码登录是一次性动作，拿到 cookie 后由主服务落盘，这里不需要持久化。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass, field

from playwright.async_api import BrowserContext, Page

from .browser import pool
from .platforms import _QR_DATA_JS, PlatformSpec

log = logging.getLogger("login-agent.sessions")

# 同时最多几个扫码会话（每个占一个 context）。
# 有头模式比 headless 吃内存，这台机器只有 4GB，取 2 比较稳。
MAX_SESSIONS = 2
# success 之后再多留一会儿，方便前端把 cookie 取走
KEEP_AFTER_SUCCESS = 120


@dataclass
class Session:
    sid: str
    spec: PlatformSpec
    context: BrowserContext
    page: Page
    created_at: float
    last_seen: float
    status: str = "pending"  # pending | success | expired | error
    qrcode: str = ""
    cookie: str = ""
    # 建会话那一刻的 cookie 快照（只存有值的）。
    # 判定登录成功时必须拿它做对比 —— 详见 poll() 里的说明。
    baseline: dict[str, str] = field(default_factory=dict)
    nickname: str = ""
    message: str = ""
    done_at: float = 0.0
    closed: bool = field(default=False)

    @property
    def age(self) -> float:
        return time.time() - self.created_at


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._cleaner: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    async def create(self, spec: PlatformSpec) -> dict:
        async with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise RuntimeError(
                    f"同时进行的扫码会话已达上限（{MAX_SESSIONS}），请先完成或取消其他扫码"
                )

        await pool.start()
        context = await pool.new_context()
        page = await context.new_page()
        sid = uuid.uuid4().hex[:16]
        session = Session(
            sid=sid, spec=spec, context=context, page=page,
            created_at=time.time(), last_seen=time.time(),
        )
        try:
            await page.goto(spec.login_url, wait_until="domcontentloaded",
                            timeout=spec.goto_timeout)
            if spec.prepare is not None:
                await spec.prepare(page)
            session.qrcode = await self._grab_qrcode(session)
        except Exception as exc:  # noqa: BLE001
            await self._close(session)
            raise RuntimeError(f"打开 {spec.label} 登录页失败：{exc}") from exc

        # 抖音光是把二维码等出来就要 60~150 秒，TTL 必须从「二维码真正
        # 展示给用户」这一刻开始算，否则会话一创建就只剩几十秒寿命了。
        session.created_at = time.time()

        # 记录「还没扫码时」的 cookie 快照。小红书在建二维码那一步就会下发
        # 一个匿名 web_session，不存快照的话下一轮轮询立刻就会误判成登录成功。
        try:
            cookies = await context.cookies()
            session.baseline = {c["name"]: c["value"] for c in cookies if c.get("value")}
            log.info("[%s] 建会话时的 cookie 快照：%s",
                     sid, sorted(session.baseline) or "（空）")
        except Exception as exc:  # noqa: BLE001
            session.baseline = {}
            log.warning("[%s] 读取 cookie 快照失败：%s", sid, exc)

        async with self._lock:
            self._sessions[sid] = session
        self._ensure_cleaner()
        log.info("[%s] 扫码会话已创建 qrcode=%d bytes", sid, len(session.qrcode))
        return {
            "session_id": sid,
            "platform": spec.key,
            "status": session.status,
            "qrcode": session.qrcode,
            "expire_in": spec.ttl,
        }

    async def poll(self, platform: str, sid: str) -> dict | None:
        session = self._sessions.get(sid)
        if session is None or session.spec.key != platform:
            return None
        session.last_seen = time.time()

        if session.status in ("success", "expired", "error"):
            return self._snapshot(session)

        if session.age > session.spec.ttl:
            session.status = "expired"
            session.message = "二维码已过期，请重新获取"
            session.done_at = time.time()
            return self._snapshot(session)

        # 1) 看 cookie —— 最可靠的登录成功信号
        try:
            cookies = await session.context.cookies()
        except Exception as exc:  # noqa: BLE001
            session.status = "error"
            session.message = f"浏览器会话异常：{exc}"
            session.done_at = time.time()
            return self._snapshot(session)

        jar = {c["name"]: c["value"] for c in cookies if c.get("value")}

        # 登录成功的判据：会话 cookie 出现了，**而且跟建会话时的值不一样**。
        #
        # 为什么必须比对值而不能只看「存在」：
        #   小红书在 /login 页生成二维码那一步就会下发一个匿名 web_session。
        #   老代码只看 web_session 是否存在，于是二维码刚出来 0.7 秒就报
        #   「登录成功」，把匿名 cookie 当成登录凭据存进了 data/cookies.json。
        #   抖音同理 —— 先存快照、再比差异，两家都稳。
        hit = next(
            (k for k in session.spec.session_cookie_keys
             if jar.get(k) and jar.get(k) != session.baseline.get(k)),
            None,
        )
        if hit:
            session.status = "success"
            session.cookie = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies if c.get("value")
            )
            session.nickname = await self._try_nickname(session)
            session.message = "登录成功"
            session.done_at = time.time()
            log.info("[%s] 登录成功（%s 已更新），cookie %d 字节",
                     sid, hit, len(session.cookie))
            return self._snapshot(session)

        # 2) 看页面有没有提示二维码过期
        try:
            text = await session.page.inner_text("body", timeout=3000)
            if any(m in text for m in session.spec.expired_markers):
                session.status = "expired"
                session.message = "二维码已过期，请重新获取"
                session.done_at = time.time()
        except Exception:  # noqa: BLE001
            pass

        return self._snapshot(session)

    async def drop(self, platform: str, sid: str) -> bool:
        session = self._sessions.get(sid)
        if session is None or session.spec.key != platform:
            return False
        async with self._lock:
            self._sessions.pop(sid, None)
        await self._close(session)
        log.info("[%s] 会话已关闭", sid)
        return True

    async def shutdown(self) -> None:
        if self._cleaner is not None:
            self._cleaner.cancel()
            self._cleaner = None
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await self._close(session)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _grab_qrcode(self, session: Session) -> str:
        """拿到二维码，快路径优先。

        快路径：一次 page.evaluate 直接在 DOM 里把二维码读成 data:URI。
          抖音的二维码本身就是 512x512 的 base64 PNG，读 src 零损失；
          而且一次往返比「每个选择器各来一次 count/is_visible/screenshot」
          便宜一个数量级 —— 这台机器上每次 evaluate 要 2~3 秒，往返次数
          就是总耗时的决定因素。

        慢路径：快路径用掉 75% 预算还没拿到，才对选择器做元素截图兜底。
          （截图在这台机器上很慢，实测 6~25 秒，所以只当保险。）

        全程受 qrcode_timeout 约束，超时就抛错，避免把前端拖死。
        """
        page = session.page
        spec = session.spec
        started = time.time()
        total = spec.qrcode_timeout / 1000
        fast_until = started + total * 0.75
        last_err: Exception | None = None

        while time.time() < fast_until:
            try:
                uri = await page.evaluate(_QR_DATA_JS, list(spec.qrcode_selectors))
            except Exception as exc:  # noqa: BLE001
                uri = None
                last_err = exc
            if isinstance(uri, str) and uri.startswith("data:image") and len(uri) > 500:
                log.info("二维码命中 data:URI（%d 字节，耗时 %.1fs）",
                         len(uri), time.time() - started)
                return uri
            # 每次 evaluate 本身就要 2~7 秒，sleep 再长纯属浪费
            await asyncio.sleep(1.0)

        log.warning("读不到二维码 data:URI（上次异常：%s），改用截图兜底", last_err)
        for sel in spec.qrcode_selectors + spec.qrcode_fallbacks:
            try:
                loc = page.locator(sel).first
                if not await loc.count() or not await loc.is_visible():
                    continue
                png = await loc.screenshot(type="png", timeout=20000)
                if png and len(png) > 200:
                    log.info("二维码截图命中: %s", sel)
                    return "data:image/png;base64," + base64.b64encode(png).decode()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        raise RuntimeError(f"未能获取二维码：{last_err}")

    async def _try_nickname(self, session: Session) -> str:
        for sel in (".nickname", "[class*='nickname']", "[class*='user-name']"):
            try:
                loc = session.page.locator(sel).first
                if await loc.count():
                    text = (await loc.inner_text(timeout=2000)).strip()
                    if text:
                        return text[:40]
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def _close(self, session: Session) -> None:
        if session.closed:
            return
        session.closed = True
        try:
            await session.context.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _snapshot(session: Session) -> dict:
        out: dict = {
            "session_id": session.sid,
            "platform": session.spec.key,
            "status": session.status,
            "message": session.message,
            "qrcode": session.qrcode if session.status == "pending" else "",
        }
        if session.status == "success":
            out["cookie"] = session.cookie
            out["nickname"] = session.nickname
        return out

    def _ensure_cleaner(self) -> None:
        if self._cleaner is None or self._cleaner.done():
            self._cleaner = asyncio.create_task(self._clean_loop())

    async def _clean_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(20)
                now = time.time()
                async with self._lock:
                    stale = [
                        s for s in self._sessions.values()
                        if (s.status in ("success", "expired", "error")
                            and now - s.done_at > KEEP_AFTER_SUCCESS)
                        or (s.status == "pending" and now - s.created_at > s.spec.ttl + 90)
                    ]
                    for s in stale:
                        self._sessions.pop(s.sid, None)
                for s in stale:
                    await self._close(s)
                    log.info("[%s] 清理过期会话（%s）", s.sid, s.status)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("会话清理任务异常")


manager = SessionManager()
