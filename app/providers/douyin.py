"""抖音：自己实现 Web 接口调用，签名用 vendor 里的 a_bogus。

为什么不用 f2 的 DouyinHandler？
  * f2 的 handler 里根本没有视频搜索接口（只定义了 POST_SEARCH 常量没用上）；
  * f2 的运行时依赖极重（protobuf / cryptography / pillow / pyexecjs / black …），
    还会把 httpx、pydantic 的版本锁死。

所以这里只 vendor 了它自包含的签名算法，HTTP 部分自己写。

实测结论（2026-10）：
  * 无 cookie：搜索返回 status_code=2483「请先登录，再继续搜索吧」，
    详情返回 403「Blocked by ArgusSecurityPlugin Uifid Not Found」。
  * 也就是说抖音的搜索与详情**都必须带登录态 Cookie**，
    Cookie 里至少要有 ttwid / msToken / UIFID（直接从浏览器整串复制即可）。
"""

from __future__ import annotations

import threading
from typing import Any

import httpx

from ..config import settings
from ..vendor.abogus import ABogus, BrowserFingerprintGenerator
from .base import MediaItem, Provider, ProviderError, Resolved, pick_first_url

# 必须与 a_bogus 算法内部使用的 UA 保持一致，否则签名对不上
DOUYIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)

SEARCH_ENDPOINT = "https://www.douyin.com/aweme/v1/web/general/search/single/"
DETAIL_ENDPOINT = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

# 与 f2 BaseRequestModel 对齐的公共参数
_BASE_PARAMS: dict[str, Any] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": 1,
    "publish_video_strategy_type": 2,
    "pc_libra_divert": "Windows",
    "version_code": "290100",
    "version_name": "29.1.0",
    "cookie_enabled": "true",
    "screen_width": 1920,
    "screen_height": 1080,
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Edge",
    "browser_version": "130.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "130.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": 12,
    "device_memory": 8,
    "platform": "PC",
    "downlink": 10,
    "effective_type": "4g",
    "round_trip_time": 100,
}


def _douyin_error_hint(text: str) -> str:
    """把抖音接口错误翻译成用户能照做的提示。"""
    if "2483" in text or "请先登录" in text:
        return ("这段 Cookie 不是登录态（抖音回「请先登录，再继续搜索吧」）。"
                "请在**已登录**的浏览器里复制，且整串一起复制，"
                "至少要包含 sessionid / ttwid / msToken / UIFID。")
    if "403" in text or "Uifid" in text:
        return ("抖音拦截了请求（403 Uifid Not Found）—— Cookie 不完整，"
                "缺 UIFID。请在浏览器里打开一次 douyin.com 再整串复制。")
    return f"抖音校验失败：{text[:180]}"


class DouyinProvider(Provider):
    name = "douyin"
    label = "抖音"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._cookie_used: str = ""

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _cookie(self) -> str:
        """扫码登录保存的凭据优先，其次才是 .env 里的 DOUYIN_COOKIE。"""
        from ..services import credentials
        return credentials.get_cookie("douyin") or settings.douyin_cookie

    def available(self) -> tuple[bool, str]:
        if not settings.douyin_enabled:
            return False, "已通过 DOUYIN_ENABLED 关闭"
        if not self._cookie():
            return False, "未登录：可在「账号」页扫码登录，或配置 DOUYIN_COOKIE"
        return True, "ok"

    def _headers(self, cookie: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": DOUYIN_UA,
            "Referer": "https://www.douyin.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        cookie = self._cookie() if cookie is None else cookie
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _make_client(self, cookie: str) -> httpx.Client:
        """按指定 cookie 建一个 httpx 客户端（校验候选 cookie 时要用）。"""
        kwargs: dict[str, Any] = {
            "timeout": settings.search_timeout,
            "follow_redirects": True,
            "headers": self._headers(cookie),
        }
        if settings.proxy:
            kwargs["proxy"] = settings.proxy
        return httpx.Client(**kwargs)

    def _get_client(self) -> httpx.Client:
        cookie = self._cookie()
        if self._client is not None and self._cookie_used == cookie:
            return self._client
        with self._lock:
            if self._client is not None and self._cookie_used == cookie:
                return self._client
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._client = None
            self._client = self._make_client(cookie)
            self._cookie_used = cookie
        return self._client

    def _cookie_value(self, key: str, cookie: str | None = None) -> str:
        """从 cookie 串里抠出某个字段（msToken 要跟着一起签名）。"""
        cookie = self._cookie() if cookie is None else cookie
        for block in (cookie or "").split(";"):
            if "=" not in block:
                continue
            name, _, value = block.partition("=")
            if name.strip() == key:
                return value.strip()
        return ""

    def _params(self, extra: dict[str, Any], cookie: str | None = None) -> dict[str, Any]:
        params = dict(_BASE_PARAMS)
        ms_token = self._cookie_value("msToken", cookie)
        if ms_token:
            params["msToken"] = ms_token
        params.update(extra)
        return params

    @staticmethod
    def _sign(endpoint: str, params: dict[str, Any], request_type: str = "GET") -> str:
        """拼出带 a_bogus 的完整 URL。

        注意：签名是在**未编码**的参数串上计算的，这与浏览器端行为一致；
        真正的百分号编码交给 httpx 发送时处理。
        """
        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        fingerprint = BrowserFingerprintGenerator.generate_fingerprint("Edge")
        _, abogus, _ = ABogus(fp=fingerprint, user_agent=DOUYIN_UA).generate_abogus(
            param_str, request_type
        )
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{param_str}&a_bogus={abogus}"

    def _call(self, endpoint: str, params: dict[str, Any],
              client: httpx.Client | None = None) -> dict[str, Any]:
        url = self._sign(endpoint, params)
        try:
            resp = (client or self._get_client()).get(url)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"抖音请求失败：{exc}") from exc
        if resp.status_code == 403:
            raise ProviderError(
                "抖音拦截了请求（403 Uifid Not Found），请检查 DOUYIN_COOKIE 是否完整"
            )
        if resp.status_code != 200:
            raise ProviderError(f"抖音返回 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("抖音返回了非 JSON 内容，可能被风控") from exc

        code = data.get("status_code")
        if code == 2483:
            raise ProviderError("抖音要求登录：请配置有效的 DOUYIN_COOKIE 后重试")
        if code not in (0, None):
            raise ProviderError(f"抖音接口报错：status_code={code} {data.get('status_msg') or ''}")
        return data

    # ------------------------------------------------------------------
    # 校验候选 cookie
    # ------------------------------------------------------------------
    def check_cookie(self, cookie: str) -> tuple[bool, str]:
        """拿这段 cookie 真去搜一次，判断是不是登录态。

        抖音的判据很干脆：未登录时搜索接口直接回 status_code=2483
        「请先登录，再继续搜索吧」；cookie 缺 UIFID 时回 403。
        """
        if not cookie.strip():
            return False, "Cookie 是空的"

        params = self._params({
            "search_channel": "aweme_general",
            "keyword": "美食",
            "search_source": "normal_search",
            "query_correct_type": 1,
            "is_filter_search": 0,
            "from_group_id": "",
            "offset": 0,
            "count": 1,
            "need_filter_settings": 1,
            "filter_selected": "",
            "search_id": "",
        }, cookie)

        client = self._make_client(cookie)
        try:
            self._call(SEARCH_ENDPOINT, params, client=client)
        except ProviderError as exc:
            return False, _douyin_error_hint(str(exc))
        except Exception as exc:  # noqa: BLE001
            return False, f"抖音请求异常：{exc}"
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        return True, "ok"

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def search(self, keyword: str, limit: int = 20) -> list[MediaItem]:
        results: list[MediaItem] = []
        seen: set[str] = set()
        offset = 0
        count = 15

        while len(results) < limit and offset < limit * 3:
            params = self._params({
                "search_channel": "aweme_general",
                "keyword": keyword,
                "search_source": "normal_search",
                "query_correct_type": 1,
                "is_filter_search": 0,
                "from_group_id": "",
                "offset": offset,
                "count": count,
                "need_filter_settings": 1,
                "filter_selected": "",
                "search_id": "",
            })
            data = self._call(SEARCH_ENDPOINT, params)
            entries = data.get("data") or []
            if not entries:
                break

            for entry in entries:
                aweme = (entry or {}).get("aweme_info") or {}
                aweme_id = str(aweme.get("aweme_id") or "")
                if not aweme_id or aweme_id in seen:
                    continue
                # 图集没有音轨，直接跳过
                if aweme.get("images") and not aweme.get("video"):
                    continue
                video = aweme.get("video") or {}
                if not video:
                    continue

                seen.add(aweme_id)
                author = aweme.get("author") or {}
                results.append(
                    MediaItem(
                        platform=self.name,
                        source_id=aweme_id,
                        title=(aweme.get("desc") or "").strip(),
                        author=(author.get("nickname") or "").strip(),
                        cover=pick_first_url(video.get("cover") or video.get("origin_cover")),
                        duration=int((video.get("duration") or 0) // 1000),
                        source_url=f"https://www.douyin.com/video/{aweme_id}",
                        extra={
                            "digg_count": (aweme.get("statistics") or {}).get("digg_count") or 0,
                        },
                    )
                )
                if len(results) >= limit:
                    break

            if not data.get("has_more"):
                break
            offset += count

        return results

    # ------------------------------------------------------------------
    # 解析直链
    # ------------------------------------------------------------------
    def resolve(self, item: MediaItem) -> Resolved:
        params = self._params({"aweme_id": item.source_id})
        data = self._call(DETAIL_ENDPOINT, params)
        aweme = data.get("aweme_detail") or {}
        if not aweme:
            raise ProviderError("抖音未返回作品详情（可能已删除或需要登录）")

        video = aweme.get("video") or {}
        if not video:
            raise ProviderError("这是一条图集作品，没有音轨可以提取")

        url = self._pick_play_url(video)
        if not url:
            raise ProviderError("未能取到抖音视频播放地址")

        author = aweme.get("author") or {}
        return Resolved(
            mode="direct",
            url=url,
            headers={
                "User-Agent": DOUYIN_UA,
                "Referer": "https://www.douyin.com/",
                **({"Cookie": self._cookie()} if self._cookie() else {}),
            },
            title=(aweme.get("desc") or item.title or "").strip()[:120],
            author=(author.get("nickname") or item.author or "").strip(),
            cover=pick_first_url(video.get("cover") or video.get("origin_cover")) or item.cover,
            duration=int((video.get("duration") or 0) // 1000) or item.duration,
        )

    @staticmethod
    def _pick_play_url(video: dict) -> str:
        # 优先挑码率最高的那条
        bit_rates = video.get("bit_rate")
        if isinstance(bit_rates, list) and bit_rates:
            ordered = sorted(
                [b for b in bit_rates if isinstance(b, dict)],
                key=lambda b: (b.get("bit_rate") or 0),
                reverse=True,
            )
            for entry in ordered:
                url = pick_first_url(entry.get("play_addr"))
                if url:
                    return url
        return pick_first_url(video.get("play_addr"))
