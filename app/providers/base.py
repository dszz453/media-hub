"""Provider 抽象层。

每个平台只需要回答两个问题：
  1. search(keyword) —— 关键词能搜出哪些内容？
  2. resolve(item)   —— 这条内容怎么变成音频？（直链自己下，还是交给 yt-dlp）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MediaItem:
    platform: str
    source_id: str
    title: str = ""
    author: str = ""
    cover: str = ""
    duration: int = 0
    source_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "source_id": self.source_id,
            "title": self.title,
            "author": self.author,
            "cover": self.cover,
            "duration": self.duration,
            "source_url": self.source_url,
            "extra": self.extra,
        }


@dataclass
class Resolved:
    """一条可下载的媒体。

    mode = "direct"：url 是 CDN 直链，先下到本地再交给 ffmpeg 抽音轨；
    mode = "ytdlp" ：url 是页面地址，整条交给 yt-dlp（YouTube 走这条路）。
    """

    mode: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    title: str = ""
    author: str = ""
    cover: str = ""
    duration: int = 0
    # 平台如果直接给了音频流地址（比如抖音的 music.play_url），可以省掉一次转码
    direct_audio_url: str = ""


class ProviderError(RuntimeError):
    """可展示给用户的平台错误。"""


class Provider:
    name: str = ""
    label: str = ""

    def available(self) -> tuple[bool, str]:
        """返回 (是否可用, 说明)。不可用时前端会给出提示而不是静默失败。"""
        return True, "ok"

    def check_cookie(self, cookie: str) -> tuple[bool, str]:
        """校验一段候选 Cookie 是不是**真的登录态**。

        用户在「账号」页粘贴 Cookie 时，先跑这个再落盘。不校验就直接存，
        会把访客会话当成登录凭据（小红书给每个访客都发 web_session），
        表现为「显示已登录，一搜就报登录已过期」。

        返回 (是否可用, 说明)。子类必须实现。
        """
        return False, "该平台暂不支持粘贴 Cookie"

    def search(self, keyword: str, limit: int = 20) -> list[MediaItem]:
        raise NotImplementedError

    def resolve(self, item: MediaItem) -> Resolved:
        raise NotImplementedError


def pick_first_url(node: Any) -> str:
    """从 {"url_list": [...], "url": "..."} 这类结构里取第一个可用 URL。"""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("master_url", "url", "play_url", "src", "url_default", "url_pre"):
            val = node.get(key)
            if isinstance(val, str) and val:
                return val
        for key in ("url_list", "backup_urls", "urls"):
            val = node.get(key)
            if isinstance(val, list):
                for entry in val:
                    got = pick_first_url(entry)
                    if got:
                        return got
    if isinstance(node, list):
        for entry in node:
            got = pick_first_url(entry)
            if got:
                return got
    return ""
