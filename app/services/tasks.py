"""后台任务队列：把"搜索结果"变成"可播放的音频文件"。

只有一个进程内的 asyncio 队列 + N 个 worker。每个 job 的重活（subprocess、
网络 IO）都丢到线程池，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import db
from ..config import settings
from ..providers import get_provider
from ..providers.base import MediaItem
from . import audio

log = logging.getLogger("mediahub.tasks")


class TaskManager:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._active: set[str] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._workers:
            return
        for idx in range(max(1, settings.max_concurrent_jobs)):
            self._workers.append(asyncio.create_task(self._worker(idx), name=f"job-{idx}"))
        log.info("任务队列已启动，worker 数：%s", len(self._workers))
        # 上次进程被强杀时可能留下 running/queued 的残留，重新入队
        for track in db.list_tracks(limit=500):
            if track["status"] in {"queued", "running"} and not track["audio_path"]:
                await self.submit(track["id"])

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    async def submit(self, track_id: str) -> bool:
        """入队。已在队列/执行中则返回 False（避免重复下载）。"""
        async with self._lock:
            if track_id in self._active:
                return False
            self._active.add(track_id)
        db.update_track(track_id, status="queued", progress=0, error="")
        await self.queue.put(track_id)
        return True

    def pending_count(self) -> int:
        return len(self._active)

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    async def _worker(self, idx: int) -> None:
        while True:
            track_id = await self.queue.get()
            try:
                await asyncio.to_thread(self._run_job, track_id)
            except Exception as exc:  # noqa: BLE001 - 任何失败都要落到 UI 上
                log.warning("任务失败 %s: %s", track_id, exc)
                db.update_track(track_id, status="failed", error=str(exc)[:500], progress=0)
            finally:
                async with self._lock:
                    self._active.discard(track_id)
                self.queue.task_done()

    # ------------------------------------------------------------------
    # 真正的干活逻辑（在线程里跑）
    # ------------------------------------------------------------------
    def _run_job(self, track_id: str) -> None:
        track = db.get_track(track_id)
        if not track:
            return

        # 已经转好且文件还在，直接跳过
        if track["status"] == "ready" and track["audio_path"]:
            existing = settings.cache_dir / track["audio_path"]
            if existing.exists() and existing.stat().st_size > 0:
                db.update_track(track_id, progress=100)
                return

        provider = get_provider(track["platform"])
        if provider is None:
            raise RuntimeError(f"未知平台：{track['platform']}")

        db.update_track(track_id, status="running", progress=1, error="")

        item = MediaItem(
            platform=track["platform"],
            source_id=track["source_id"],
            title=track["title"],
            author=track["author"],
            cover=track["cover"],
            duration=track["duration"],
            source_url=track["source_url"],
            extra=track.get("extra") or {},
        )

        resolved = provider.resolve(item)

        def on_progress(percent: float) -> None:
            db.update_track(track_id, progress=max(1.0, min(99.0, round(percent, 1))))

        title = (resolved.title or track["title"] or track_id).strip()
        author = (resolved.author or track["author"] or "").strip()
        stem = f"{audio.safe_filename(title)}-{track_id}"
        out_dir = settings.cache_dir / track["platform"]
        out_dir.mkdir(parents=True, exist_ok=True)

        if resolved.mode == "ytdlp":
            final_path = audio.ytdlp_extract_audio(
                resolved.url, out_dir, stem, progress_cb=on_progress
            )
        else:
            final_path = self._extract_from_direct(track_id, resolved, out_dir, stem, on_progress)

        # 封面单独缓存一份，供 /api/cover 使用（远程图有防盗链，不能直接给前端）
        cover_url = resolved.cover or track["cover"]
        self._cache_cover(track_id, cover_url)

        duration = audio.probe_duration(final_path) or resolved.duration or track["duration"]
        rel_path = final_path.relative_to(settings.cache_dir).as_posix()

        db.update_track(
            track_id,
            status="ready",
            progress=100,
            audio_path=rel_path,
            duration=duration,
            title=title,
            author=author,
            error="",
        )
        log.info("转音频完成：%s -> %s", title, rel_path)

    # ------------------------------------------------------------------
    def _extract_from_direct(self, track_id, resolved, out_dir, stem, on_progress) -> Path:
        """小红书 / 抖音：先下视频，再 ffmpeg 抽音轨。"""
        video_path = settings.tmp_dir / f"{track_id}.mp4"
        try:
            audio.download_direct(
                resolved.url,
                video_path,
                headers=resolved.headers,
                # 下载只占整体进度的前 80%，剩下 20% 留给 ffmpeg
                progress_cb=lambda p: on_progress(p * 0.8),
            )
            if not audio.probe_has_audio(video_path):
                raise RuntimeError("该视频不含音轨，无法提取音频")

            cover = None
            if resolved.cover or settings.embed_cover:
                cover = audio.download_cover(resolved.cover, settings.tmp_dir / track_id)

            target = out_dir / f"{stem}{settings.audio_suffix}"
            on_progress(85)
            audio.extract_audio(
                video_path,
                target,
                cover=cover,
                meta={
                    "title": resolved.title,
                    "artist": resolved.author,
                    "album": f"MediaHub · {resolved.mode}",
                    "comment": resolved.url,
                },
            )
            on_progress(99)
            return target
        finally:
            if not settings.keep_video:
                audio.cleanup([video_path])

    @staticmethod
    def _cache_cover(track_id: str, cover_url: str) -> None:
        if not cover_url:
            return
        target = settings.cache_dir / "covers" / f"{track_id}.jpg"
        if target.exists() and target.stat().st_size > 0:
            return
        audio.download_cover(cover_url, settings.cache_dir / "covers" / track_id)


# 全局单例
manager = TaskManager()
