"""HTTP 接口层。"""

from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

from .. import db
from ..config import settings
from ..providers import all_providers, get_provider, provider_status
from ..providers.base import MediaItem, ProviderError
from ..schemas import (
    ExtractRequest,
    PlaylistCreate,
    SearchResponse,
)
from ..services import audio
from ..services.tasks import manager

router = APIRouter(prefix="/api")

CHUNK = 1 << 16


# ----------------------------------------------------------------------
# 基础
# ----------------------------------------------------------------------
@router.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "audio_format": settings.audio_format,
        "tools": audio.tool_versions(),
        "queue": manager.pending_count(),
    }


@router.get("/platforms")
def platforms() -> dict:
    return {"items": provider_status()}


@router.get("/stats")
def stats() -> dict:
    data = db.stats()
    data["queue"] = manager.pending_count()
    data["cache_bytes_human"] = _human(data.get("cache_bytes", 0))
    return data


@router.post("/cache/clear")
def clear_cache() -> dict:
    removed = db.cache_clear()
    return {"removed": removed}


# ----------------------------------------------------------------------
# 搜索
# ----------------------------------------------------------------------
@router.get("/search", response_model=SearchResponse)
async def search(
    keyword: str = Query(..., min_length=1, max_length=120),
    platform: str = Query("all"),
    limit: int = Query(20, ge=1, le=50),
) -> SearchResponse:
    keyword = keyword.strip()
    cache_key = f"search:{platform}:{limit}:{keyword}"
    cached = db.cache_get(cache_key)
    if cached:
        return SearchResponse(keyword=keyword, platform=platform, cached=True, **cached)

    targets = all_providers() if platform == "all" else [get_provider(platform)]
    targets = [p for p in targets if p is not None]
    if not targets:
        raise HTTPException(status_code=400, detail=f"未知平台：{platform}")

    errors: dict[str, str] = {}
    collected: list[dict] = []

    async def run(provider):
        ok, reason = await asyncio.to_thread(provider.available)
        if not ok:
            errors[provider.name] = reason
            return []
        try:
            items = await asyncio.to_thread(provider.search, keyword, limit)
        except ProviderError as exc:
            errors[provider.name] = str(exc)
            return []
        except Exception as exc:  # noqa: BLE001
            errors[provider.name] = f"{type(exc).__name__}: {exc}"[:300]
            return []
        return [i.to_dict() for i in items]

    batches = await asyncio.gather(*(run(p) for p in targets))

    # 多平台时按平台轮转交错，避免"全部"结果里前面全是同一个平台
    queues = [b for b in batches if b]
    idx = 0
    while queues:
        current = queues[idx % len(queues)]
        if current:
            collected.append(current.pop(0))
        queues = [q for q in queues if q]
        idx += 1
        if idx > 10000:
            break

    payload = {"items": collected, "errors": errors}
    if collected:
        db.cache_set(cache_key, payload)
    return SearchResponse(keyword=keyword, platform=platform, **payload)


# ----------------------------------------------------------------------
# 转音频
# ----------------------------------------------------------------------
@router.post("/extract")
async def extract(req: ExtractRequest) -> dict:
    provider = get_provider(req.platform)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"未知平台：{req.platform}")

    track_id = db.upsert_track(
        {
            "platform": req.platform,
            "source_id": req.source_id,
            "title": req.title,
            "author": req.author,
            "cover": req.cover,
            "duration": req.duration,
            "source_url": req.source_url,
            "extra": req.extra,
        }
    )
    track = db.get_track(track_id)
    if track and track["status"] == "ready" and track["audio_path"]:
        if (settings.cache_dir / track["audio_path"]).exists():
            return {"track_id": track_id, "status": "ready", "queued": False}

    queued = await manager.submit(track_id)
    return {"track_id": track_id, "status": "queued", "queued": queued}


@router.get("/track/{track_id}")
def get_track(track_id: str) -> dict:
    track = db.get_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="曲目不存在")
    return track


@router.get("/jobs")
def jobs() -> dict:
    items = [t for t in db.list_tracks(limit=200) if t["status"] in {"queued", "running"}]
    failed = [t for t in db.list_tracks(status="failed", limit=50)]
    return {"active": items, "failed": failed, "count": len(items)}


@router.get("/tracks")
def tracks(status: str | None = None, platform: str | None = None,
           limit: int = Query(200, ge=1, le=500), offset: int = 0) -> dict:
    return {"items": db.list_tracks(status=status, platform=platform,
                                    limit=limit, offset=offset)}


@router.delete("/track/{track_id}")
def delete_track(track_id: str, remove_file: bool = True) -> dict:
    track = db.get_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="曲目不存在")
    if remove_file and track["audio_path"]:
        audio.cleanup([settings.cache_dir / track["audio_path"]])
    with db.cursor() as cur:
        cur.execute("DELETE FROM tracks WHERE id=?", (track_id,))
    return {"deleted": track_id}


# ----------------------------------------------------------------------
# 音频流 / 下载 / 封面
# ----------------------------------------------------------------------
_MIME = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "opus": "audio/ogg", "wav": "audio/wav"}


def _resolve_audio(track_id: str) -> tuple[dict, Path]:
    track = db.get_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="曲目不存在")
    if track["status"] != "ready" or not track["audio_path"]:
        raise HTTPException(status_code=409, detail=f"音频尚未就绪（{track['status']}）")
    path = settings.cache_dir / track["audio_path"]
    if not path.exists():
        raise HTTPException(status_code=410, detail="音频文件已被清理，请重新转一次")
    return track, path


@router.get("/stream/{track_id}")
def stream(track_id: str, request: Request):
    """支持 HTTP Range，<audio> 才能拖动进度条。"""
    track, path = _resolve_audio(track_id)
    suffix = path.suffix.lstrip(".").lower()
    media_type = _MIME.get(suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400"},
        )

    match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        return FileResponse(path, media_type=media_type,
                            headers={"Accept-Ranges": "bytes"})
    raw_start, raw_end = match.group(1), match.group(2)
    start = int(raw_start) if raw_start else 0
    end = int(raw_end) if raw_end else size - 1
    if start >= size:
        return JSONResponse({"detail": "Range 超出文件大小"}, status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    length = end - start + 1

    def iterator():
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "public, max-age=86400",
    }
    return StreamingResponse(iterator(), status_code=206, headers=headers,
                             media_type=media_type)


@router.get("/download/{track_id}")
def download(track_id: str):
    track, path = _resolve_audio(track_id)
    name = f"{audio.safe_filename(track['title'])}{path.suffix}"
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@router.get("/cover/{track_id}")
def cover(track_id: str):
    track = db.get_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="曲目不存在")
    cached = settings.cache_dir / "covers" / f"{track_id}.jpg"
    if cached.exists() and cached.stat().st_size > 0:
        return FileResponse(cached, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800"})
    remote = track.get("cover") or ""
    if not remote:
        raise HTTPException(status_code=404, detail="无封面")
    # 首次访问时拉一次并落盘
    try:
        audio.download_cover(remote, settings.cache_dir / "covers" / track_id)
    except Exception:  # noqa: BLE001
        return RedirectResponse(remote, status_code=302)
    if cached.exists():
        return FileResponse(cached, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800"})
    return RedirectResponse(remote, status_code=302)


# ----------------------------------------------------------------------
# 图片代理
#   小红书的封面有防盗链，必须带 Referer 才能取到；搜索阶段曲目还没入库，
#   所以需要一个通用的图片代理给前端用。
# ----------------------------------------------------------------------
_REFERER_BY_HOST = {
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "xhscdn": "https://www.xiaohongshu.com/",
    "douyin": "https://www.douyin.com/",
    "douyinpic": "https://www.douyin.com/",
    "byteimg": "https://www.douyin.com/",
    "ytimg": "https://www.youtube.com/",
    "ggpht": "https://www.youtube.com/",
}


def _is_public_host(host: str) -> bool:
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


@router.get("/proxy")
def image_proxy(url: str = Query(..., min_length=8)):
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="仅支持 http/https 图片地址")
    if not _is_public_host(parsed.hostname):
        raise HTTPException(status_code=400, detail="不允许访问内网地址")

    referer = "https://www.xiaohongshu.com/"
    for key, value in _REFERER_BY_HOST.items():
        if key in parsed.hostname:
            referer = value
            break

    headers = {"User-Agent": settings.request_ua, "Referer": referer, "Accept": "image/*,*/*"}
    try:
        client = httpx.Client(timeout=20, follow_redirects=True)
        resp = client.send(client.build_request("GET", url, headers=headers), stream=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"图片拉取失败：{exc}") from exc

    media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]

    def iterator():
        try:
            for chunk in resp.iter_bytes(CHUNK):
                yield chunk
        finally:
            resp.close()
            client.close()

    return StreamingResponse(iterator(), media_type=media_type,
                             headers={"Cache-Control": "public, max-age=604800"})


# ----------------------------------------------------------------------
# 收藏
# ----------------------------------------------------------------------
@router.get("/favorites")
def favorites() -> dict:
    return {"items": db.list_favorites()}


@router.post("/favorites/{track_id}")
def add_favorite(track_id: str) -> dict:
    if not db.get_track(track_id):
        raise HTTPException(status_code=404, detail="曲目不存在")
    db.add_favorite(track_id)
    return {"track_id": track_id, "favorited": True}


@router.delete("/favorites/{track_id}")
def del_favorite(track_id: str) -> dict:
    db.remove_favorite(track_id)
    return {"track_id": track_id, "favorited": False}


# ----------------------------------------------------------------------
# 历史
# ----------------------------------------------------------------------
@router.get("/history")
def history(limit: int = Query(100, ge=1, le=300)) -> dict:
    return {"items": db.list_history(limit)}


@router.post("/history/{track_id}")
def add_history(track_id: str) -> dict:
    if not db.get_track(track_id):
        raise HTTPException(status_code=404, detail="曲目不存在")
    db.add_history(track_id)
    return {"ok": True}


# ----------------------------------------------------------------------
# 歌单
# ----------------------------------------------------------------------
@router.get("/playlists")
def playlists() -> dict:
    return {"items": db.list_playlists()}


@router.post("/playlists")
def create_playlist(body: PlaylistCreate) -> dict:
    pid = db.create_playlist(body.name.strip())
    return {"id": pid, "name": body.name.strip()}


@router.delete("/playlists/{playlist_id}")
def delete_playlist(playlist_id: int) -> dict:
    db.delete_playlist(playlist_id)
    return {"deleted": playlist_id}


@router.get("/playlists/{playlist_id}")
def playlist_detail(playlist_id: int) -> dict:
    return {"id": playlist_id, "items": db.playlist_tracks(playlist_id)}


@router.post("/playlists/{playlist_id}/items")
def playlist_add(playlist_id: int, track_id: str = Query(...)) -> dict:
    if not db.get_track(track_id):
        raise HTTPException(status_code=404, detail="曲目不存在")
    db.add_to_playlist(playlist_id, track_id)
    return {"ok": True}


@router.delete("/playlists/{playlist_id}/items/{track_id}")
def playlist_remove(playlist_id: int, track_id: str) -> dict:
    db.remove_from_playlist(playlist_id, track_id)
    return {"ok": True}


# ----------------------------------------------------------------------
# 粘贴链接直接解析
# ----------------------------------------------------------------------
_URL_RULES = [
    ("xhs", re.compile(r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-zA-Z]+)")),
    ("xhs", re.compile(r"xhslink\.com/([0-9a-zA-Z]+)")),
    ("douyin", re.compile(r"douyin\.com/video/(\d+)")),
    ("douyin", re.compile(r"v\.douyin\.com/([0-9a-zA-Z]+)")),
    ("youtube", re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([0-9a-zA-Z_-]{6,})")),
]


@router.post("/parse")
async def parse_url(payload: dict) -> dict:
    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="缺少 url")

    for platform, pattern in _URL_RULES:
        match = pattern.search(url)
        if not match:
            continue
        provider = get_provider(platform)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"不支持的平台：{platform}")
        ok, reason = await asyncio.to_thread(provider.available)
        if not ok:
            raise HTTPException(status_code=409, detail=f"{provider.label} 不可用：{reason}")

        if platform == "youtube":
            try:
                info = await asyncio.to_thread(audio.ytdlp_dump, url)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"解析失败：{exc}") from exc
            item = MediaItem(
                platform="youtube",
                source_id=info.get("id") or match.group(1),
                title=info.get("title") or "",
                author=info.get("uploader") or "",
                cover=info.get("thumbnail") or "",
                duration=int(info.get("duration") or 0),
                source_url=info.get("webpage_url") or url,
            )
        else:
            item = MediaItem(platform=platform, source_id=match.group(1), source_url=url)
            try:
                resolved = await asyncio.to_thread(provider.resolve, item)
            except ProviderError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            item.title = resolved.title
            item.author = resolved.author
            item.cover = resolved.cover
            item.duration = resolved.duration

        track_id = db.upsert_track(item.to_dict())
        return {"track_id": track_id, "item": db.get_track(track_id)}

    raise HTTPException(status_code=400, detail="无法识别的链接，请粘贴小红书/抖音/YouTube 的分享链接")


# ----------------------------------------------------------------------
def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
