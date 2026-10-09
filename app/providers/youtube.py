"""YouTube：完全依赖 yt-dlp 的 ytsearch，不需要任何 cookie 就能搜。"""

from __future__ import annotations

from ..config import settings
from ..services import audio
from .base import MediaItem, Provider, ProviderError, Resolved


class YouTubeProvider(Provider):
    name = "youtube"
    label = "YouTube"

    def available(self) -> tuple[bool, str]:
        if not settings.youtube_enabled:
            return False, "已通过 YOUTUBE_ENABLED 关闭"
        return True, "ok"

    def search(self, keyword: str, limit: int = 20) -> list[MediaItem]:
        try:
            raw_items = audio.ytdlp_search(keyword, limit=limit)
        except Exception as exc:
            raise ProviderError(f"YouTube 搜索失败：{exc}") from exc

        results: list[MediaItem] = []
        for raw in raw_items:
            video_id = raw.get("id") or ""
            if not video_id:
                continue
            cover = self._cover(raw, video_id)
            results.append(
                MediaItem(
                    platform=self.name,
                    source_id=video_id,
                    title=raw.get("title") or "",
                    author=raw.get("uploader") or raw.get("channel") or "",
                    cover=cover,
                    duration=int(raw.get("duration") or 0),
                    source_url=raw.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                    extra={"view_count": raw.get("view_count") or 0},
                )
            )
        return results

    @staticmethod
    def _cover(raw: dict, video_id: str) -> str:
        thumbs = raw.get("thumbnails") or []
        if isinstance(thumbs, list) and thumbs:
            # flat-playlist 模式下缩略图可能只有一个默认项
            best = thumbs[-1]
            if isinstance(best, dict) and best.get("url"):
                return best["url"]
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"

    def resolve(self, item: MediaItem) -> Resolved:
        url = item.source_url or f"https://www.youtube.com/watch?v={item.source_id}"
        return Resolved(
            mode="ytdlp",
            url=url,
            title=item.title,
            author=item.author,
            cover=item.cover,
            duration=item.duration,
        )
