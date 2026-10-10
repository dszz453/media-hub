"""小红书：走 xhs SDK。

关键点（读源码确认过 xhs==0.2.13）：
  * XhsClient(cookie, user_agent, timeout, proxies, sign)
  * 所有请求都会调用 external_sign(uri, data, a1=..., web_session=...)，
    返回值直接 update 进请求头，所以必须给一个 sign 回调，否则必然 TypeError。
  * 好消息：xhs.help.sign 是一个**纯 Python** 的 x-s 实现，无需 playwright。
    因此本服务默认用本地签名，镜像保持轻量；如需更强的兼容性，可另配
    XHS_SIGN_SERVER 走浏览器级签名服务。
  * get_note_by_id(note_id) 返回的就是 note_card 本身（不是外层 items）。
  * get_note_by_keyword(keyword, page, page_size, sort, note_type)。
"""

from __future__ import annotations

import contextlib
import io
import os
import threading
from typing import Any, Callable

import httpx

from ..config import settings
from .base import MediaItem, Provider, ProviderError, Resolved, pick_first_url

_COVER_KEYS = ("url_default", "url_pre", "url")


@contextlib.contextmanager
def _quiet_stdout():
    """xhs SDK 在每个响应里都 print(data)，会把日志刷爆。"""
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        with contextlib.redirect_stdout(devnull):
            yield


class XhsProvider(Provider):
    name = "xhs"
    label = "小红书"

    def __init__(self) -> None:
        self._client: Any = None
        self._lock = threading.Lock()
        self._init_error: str = ""
        self._cookie_used: str = ""

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _cookie(self) -> str:
        """扫码登录保存的凭据优先，其次才是 .env 里的 XHS_COOKIE。"""
        from ..services import credentials
        return credentials.get_cookie("xhs") or settings.xhs_cookie

    def available(self) -> tuple[bool, str]:
        if not settings.xhs_enabled:
            return False, "已通过 XHS_ENABLED 关闭"
        if not self._cookie():
            return False, "未登录：可在「账号」页扫码登录，或配置 XHS_COOKIE"
        try:
            self._ensure_client()
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"小红书客户端初始化失败：{exc}"
        return True, "ok"

    def _ensure_client(self) -> Any:
        cookie = self._cookie()
        if self._client is not None and self._cookie_used == cookie:
            return self._client
        with self._lock:
            if self._client is not None and self._cookie_used == cookie:
                return self._client
            # cookie 变了（例如刚扫码登录完），清掉旧的失败状态重建客户端
            self._init_error = ""
            try:
                from xhs import XhsClient  # type: ignore
            except ImportError as exc:
                self._init_error = "未安装 xhs 库，请检查镜像依赖"
                raise ProviderError(self._init_error) from exc

            self._client = XhsClient(
                cookie=cookie,
                user_agent=settings.request_ua,
                timeout=settings.search_timeout,
                sign=self._build_sign(),
            )
            self._cookie_used = cookie
            return self._client

    def _build_sign(self) -> Callable[..., dict]:
        """返回 (uri, data, a1=..., web_session=...) -> {header: value}。

        优先级：显式配置的签名服务 > xhs 自带的纯 Python 签名。
        """
        if settings.xhs_sign_server:
            endpoint = f"{settings.xhs_sign_server}/sign"

            def remote_sign(uri: str, data: Any = None, a1: str = "",
                            web_session: str = "", **_: Any) -> dict:
                payload = {"uri": uri, "data": data, "a1": a1, "web_session": web_session}
                try:
                    resp = httpx.post(endpoint, json=payload, timeout=15)
                    resp.raise_for_status()
                    raw = resp.json()
                except Exception as exc:  # noqa: BLE001
                    raise ProviderError(f"小红书签名服务不可用（{endpoint}）：{exc}") from exc
                return _normalize_sign(raw)

            return remote_sign

        try:
            from xhs.help import sign as local_sign  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("xhs 未提供本地签名实现，请配置 XHS_SIGN_SERVER") from exc

        def sign(uri: str, data: Any = None, a1: str = "",
                 web_session: str = "", **_: Any) -> dict:
            # 注意：xhs 内部是用 web_session= 这个关键字调用 sign 的，
            # 而 xhs.help.sign 的形参叫 b1，所以这里要吃掉多余的 kwargs。
            return _normalize_sign(local_sign(uri, data, a1=a1))

        return sign

    # ------------------------------------------------------------------
    # 校验候选 cookie
    # ------------------------------------------------------------------
    def check_cookie(self, cookie: str) -> tuple[bool, str]:
        """拿这段 cookie 真去搜一次，用结果判断它是不是登录态。

        为什么用「搜索」而不是「查我是谁」：xhs SDK 的 GET 类接口签名有问题
        （xhs 0.2.13 的本地签名对 GET 不覆盖 query string，服务端一律回 406
        {"code":-1}），所以 get_self_info 拿不到有效信息。搜索是 POST，
        签名正常，而且它正好就是我们真正要用的能力 —— 搜得动就是好 cookie。
        """
        if not cookie.strip():
            return False, "Cookie 是空的"

        try:
            from xhs import XhsClient  # type: ignore
        except ImportError:
            return False, "容器里没装 xhs 库"

        try:
            client = XhsClient(
                cookie=cookie,
                user_agent=settings.request_ua,
                timeout=settings.search_timeout,
                sign=self._build_sign(),
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"小红书客户端初始化失败：{exc}"

        try:
            with _quiet_stdout():
                client.get_note_by_keyword(keyword="美食", page=1, page_size=20)
        except Exception as exc:  # noqa: BLE001
            return False, _xhs_error_hint(exc)
        return True, "ok"

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def search(self, keyword: str, limit: int = 20) -> list[MediaItem]:
        client = self._ensure_client()
        try:
            from xhs import SearchNoteType  # type: ignore
            video_only: Any = SearchNoteType.VIDEO
        except ImportError:  # pragma: no cover
            video_only = 1

        results: list[MediaItem] = []
        seen: set[str] = set()
        page = 1
        page_size = min(20, max(1, limit))

        while len(results) < limit and page <= 5:
            try:
                with _quiet_stdout():
                    payload = client.get_note_by_keyword(
                        keyword=keyword, page=page, page_size=page_size,
                        note_type=video_only,
                    )
            except Exception as exc:  # noqa: BLE001
                if results:
                    break
                raise ProviderError(f"小红书搜索失败：{self._friendly(exc)}") from exc

            items = (payload or {}).get("items") or []
            if not items:
                break

            for raw in items:
                if not isinstance(raw, dict):
                    continue
                note_id = str(raw.get("id") or "")
                card = raw.get("note_card") or {}
                if not note_id or note_id in seen or not isinstance(card, dict):
                    continue
                note_type = (card.get("type") or "").lower()
                if note_type and note_type != "video":
                    continue

                seen.add(note_id)
                user = card.get("user") or {}
                results.append(
                    MediaItem(
                        platform=self.name,
                        source_id=note_id,
                        title=(card.get("display_title") or card.get("displayTitle") or "").strip(),
                        author=(user.get("nickname") or user.get("nick_name") or "").strip(),
                        cover=self._pick_cover(card),
                        duration=self._pick_duration(card),
                        source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
                        extra={
                            "xsec_token": raw.get("xsec_token") or "",
                            "liked": str((card.get("interact_info") or {}).get("liked_count") or ""),
                        },
                    )
                )
                if len(results) >= limit:
                    break

            if not (payload or {}).get("has_more"):
                break
            page += 1

        return results

    # ------------------------------------------------------------------
    # 解析直链
    # ------------------------------------------------------------------
    def resolve(self, item: MediaItem) -> Resolved:
        client = self._ensure_client()
        try:
            with _quiet_stdout():
                note = client.get_note_by_id(item.source_id)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"小红书笔记解析失败：{self._friendly(exc)}") from exc
        if not note:
            raise ProviderError("小红书未返回笔记内容（可能已被删除或需要登录）")

        note_type = (note.get("type") or "").lower()
        if note_type and note_type != "video":
            raise ProviderError("这是一条图文笔记，没有音轨可以提取")

        url = self._pick_video_url(note)
        if not url:
            raise ProviderError("这条笔记没有可下载的视频流")

        user = note.get("user") or {}
        title = (note.get("title") or note.get("desc") or item.title or "").strip()
        return Resolved(
            mode="direct",
            url=url,
            headers={
                "Referer": "https://www.xiaohongshu.com/",
                "User-Agent": settings.request_ua,
            },
            title=title[:120] or item.title,
            author=(user.get("nickname") or item.author or "").strip(),
            cover=self._pick_cover(note) or item.cover,
            duration=self._pick_duration(note) or item.duration,
        )

    # ------------------------------------------------------------------
    # 解析工具
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_video_url(note: dict) -> str:
        video = note.get("video") or {}

        # 1) 官方 SDK 的取法：origin_video_key 拼 CDN（无水印原片）
        try:
            from xhs.help import get_video_url_from_note  # type: ignore
            url = get_video_url_from_note(note)
            if url:
                return url
        except Exception:  # noqa: BLE001
            pass

        # 2) 退回 media.stream 里的 master_url
        stream = ((video.get("media") or {}).get("stream") or {})
        for codec in ("h264", "h265", "av1"):
            entries = stream.get(codec)
            if not isinstance(entries, list):
                continue
            for entry in sorted([e for e in entries if isinstance(e, dict)],
                                key=lambda e: (e.get("size") or 0), reverse=True):
                url = pick_first_url(entry)
                if url:
                    return url
        return ""

    @staticmethod
    def _pick_cover(note: dict) -> str:
        cover = note.get("cover") or note.get("image_cover") or {}
        if isinstance(cover, dict):
            for key in _COVER_KEYS:
                url = cover.get(key)
                if isinstance(url, str) and url:
                    return url
        url = pick_first_url(cover)
        if url:
            return url
        # 图文笔记兜底
        images = note.get("image_list") or []
        if images and isinstance(images[0], dict):
            return pick_first_url(images[0].get("url_default") or images[0])
        return ""

    @staticmethod
    def _pick_duration(note: dict) -> int:
        video = note.get("video") or {}
        capa = video.get("capa") or {}
        for key in ("duration", "duration_ms"):
            val = capa.get(key) or video.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val / 1000) if key.endswith("_ms") else int(val)
        return 0

    @staticmethod
    def _friendly(exc: Exception) -> str:
        text = str(exc)
        lowered = text.lower()
        if "461" in text or "471" in text or "验证码" in text or "verify" in lowered:
            return "触发了小红书风控（需要验证码），请稍后重试或更换出口 IP"
        if "sign" in lowered or "300015" in text:
            return "x-s 签名校验未通过，可尝试配置 XHS_SIGN_SERVER 走浏览器级签名"
        if "-100" in text or "登录" in text or "expired" in lowered:
            return "cookie 已失效，请重新从浏览器复制 XHS_COOKIE"
        if "300012" in text:
            return "小红书判定当前 IP 请求过快，请稍后重试"
        return text[:300]


def _normalize_sign(raw: Any) -> dict:
    """签名服务/SDK 可能返回多余字段，只保留真正的请求头。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ("x-s", "x-t", "x-s-common"):
        if raw.get(key):
            out[key] = raw[key]
    return out or raw


_XHS_ERROR_HINTS = {
    "-100": "服务端判定「登录已过期」—— 这段 Cookie 不是登录态。"
            "小红书会给每个访客都发一个匿名 web_session，看起来像登录但其实不是。"
            "复制前请确认页面右上角显示的是你的头像，而不是「登录」按钮。",
    "-101": "服务端说「无登录信息」—— Cookie 里没有携带登录会话，"
            "多半复制时漏掉了 web_session。",
    "300011": "账号被小红书风控了（「当前账号存在异常，请切换账号后重试」）。"
              "建议先在手机 App 上正常用几天，或换一个账号。",
    "461": "触发了验证码校验，稍后再试。",
    "406": "签名校验没通过，通常是 Cookie 里缺 a1。",
}


def _xhs_error_hint(exc: Exception) -> str:
    """把 xhs SDK 抛出的 dict 型异常翻译成人话。"""
    text = str(exc)
    for code, hint in _XHS_ERROR_HINTS.items():
        if f"'code': {code}" in text or f'"code": {code}' in text:
            return hint
    return f"小红书接口报错：{text[:180]}"

