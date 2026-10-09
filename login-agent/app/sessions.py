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
from .platforms import PlatformSpec

log = logging.getLogger("login-agent.sessions")

# 同时最多几个扫码会话（每个占一个 context，小机器上要克制）
MAX_SESSIONS = 3
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
        if any(k in jar for k in session.spec.session_cookie_keys):
            session.status = "success"
            session.cookie = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies if c.get("value")
            )
            session.nickname = await self._try_nickname(session)
            session.message = "登录成功"
            session.done_at = time.time()
            log.info("[%s] 登录成功，cookie %d 字节", sid, len(session.cookie))
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
        """截取二维码元素，返回 data URL。"""
        page = session.page
        last_err: Exception | None = None
        for sel in session.spec.qrcode_selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=session.spec.qrcode_timeout)
                png = await loc.screenshot(type="png")
                if png and len(png) > 200:
                    log.info("二维码选择器命中: %s", sel)
                    return "data:image/png;base64," + base64.b64encode(png).decode()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue

        # 兜底：整页截图（二维码会小一点，但至少能用）
        try:
            png = await page.screenshot(type="png", full_page=False)
            log.warning("二维码选择器全部未命中，回退整页截图（%s）", last_err)
            return "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"未能截取二维码：{exc}") from exc

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
