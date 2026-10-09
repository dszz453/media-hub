"""音频处理内核：yt-dlp 调用、ffmpeg 转码、直链下载、封面/元数据写入、时长探测。

设计原则：
* 所有外部进程调用都走 subprocess，并把进度通过回调抛出来，前端才能看到百分比；
* ffmpeg / yt-dlp 全部用 list 形式传参，不做 shell 拼接，避免 URL 里的特殊字符出问题。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import httpx

from ..config import settings

ProgressCB = Callable[[float], None]

_PERCENT_RE = re.compile(r"\[download\]\s+([\d.]+)%")
_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE = shutil.which("ffprobe") or "ffprobe"


# ----------------------------------------------------------------------
# 环境探测
# ----------------------------------------------------------------------
def ffmpeg_available() -> bool:
    try:
        subprocess.run([_FFMPEG, "-version"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


# 工具版本在容器生命周期内不会变，但探测要 fork 两个子进程。
# 这台机器（J1900）上 ffmpeg/yt-dlp 的冷启动加起来要 4~5 秒，
# 而 /api/health 每次都调它 —— 正好卡在 Docker healthcheck 的 5 秒超时线上，
# 容器于是被判成 unhealthy（接口本身其实一直是 200）。
# 用 dict 做一次性缓存：第一次探测完就固定下来。
_TOOL_VERSIONS: dict[str, str] | None = None


def tool_versions() -> dict[str, str]:
    global _TOOL_VERSIONS
    if _TOOL_VERSIONS is None:
        out: dict[str, str] = {}
        for name, cmd in (("ffmpeg", [_FFMPEG, "-version"]), ("yt-dlp", ["yt-dlp", "--version"])):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=15, text=True)
                first = (res.stdout or res.stderr or "").splitlines()[0]
                out[name] = first.strip()
            except Exception as exc:  # pragma: no cover
                out[name] = f"unavailable: {exc}"
        _TOOL_VERSIONS = out
    return dict(_TOOL_VERSIONS)


def safe_filename(name: str, fallback: str = "audio") -> str:
    """把标题压成安全的文件名片段（保留中文）。"""
    name = unicodedata.normalize("NFKC", name or "")
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name).strip(" ._")
    name = re.sub(r"\s+", " ", name)
    return (name[:80] or fallback)


# ----------------------------------------------------------------------
# 直链下载（小红书 / 抖音走这条路）
# ----------------------------------------------------------------------
def download_direct(
    url: str,
    dst: Path,
    headers: dict[str, str] | None = None,
    progress_cb: ProgressCB | None = None,
) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    merged = {
        "User-Agent": settings.request_ua,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        merged.update({k: v for k, v in headers.items() if v})

    timeout = httpx.Timeout(settings.download_timeout, connect=20.0)
    with httpx.Client(timeout=timeout, follow_redirects=True, http2=False) as client:
        with client.stream("GET", url, headers=merged) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with dst.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(99.0, done * 100.0 / total))
    if dst.stat().st_size == 0:
        raise RuntimeError("下载到空文件，可能是直链已过期或需要 cookie")
    return dst


def download_cover(url: str, dst_dir: Path) -> Path | None:
    if not url:
        return None
    dst = dst_dir / "cover.jpg"
    try:
        download_direct(url, dst)
        return dst if dst.stat().st_size > 0 else None
    except Exception:
        return None


# ----------------------------------------------------------------------
# ffmpeg
# ----------------------------------------------------------------------
def probe_duration(path: Path) -> int:
    try:
        res = subprocess.run(
            [_FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(res.stdout or "{}")
        return int(float(data.get("format", {}).get("duration") or 0))
    except Exception:
        return 0


def extract_audio(
    src: Path,
    dst: Path,
    cover: Path | None = None,
    meta: dict[str, str] | None = None,
) -> Path:
    """从视频/音频文件里抽出音轨，顺带写封面和标签。"""
    meta = {k: v for k, v in (meta or {}).items() if v}
    dst.parent.mkdir(parents=True, exist_ok=True)
    fmt = settings.audio_format
    has_cover = bool(cover and cover.exists() and settings.embed_cover and fmt == "mp3")

    cmd: list[str] = [_FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if has_cover:
        cmd += ["-i", str(cover)]

    cmd += ["-map", "0:a:0"]
    if has_cover:
        cmd += ["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]

    if fmt == "m4a":
        cmd += ["-c:a", "aac", "-b:a", settings.audio_bitrate, "-movflags", "+faststart"]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", settings.audio_bitrate, "-id3v2_version", "3"]

    # 丢掉源视频里的杂乱标签，只写我们自己给的
    cmd += ["-map_metadata", "-1"]

    if settings.embed_metadata:
        if meta.get("title"):
            cmd += ["-metadata", f"title={meta['title']}"]
        if meta.get("artist"):
            cmd += ["-metadata", f"artist={meta['artist']}"]
        if meta.get("album"):
            cmd += ["-metadata", f"album={meta['album']}"]
        if meta.get("comment"):
            cmd += ["-metadata", f"comment={meta['comment']}"]
    if has_cover:
        cmd += ["-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)"]

    cmd.append(str(dst))
    res = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=settings.download_timeout)
    if res.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 转码失败: {(res.stderr or '').strip()[:400]}")
    return dst


def probe_has_audio(path: Path) -> bool:
    try:
        res = subprocess.run(
            [_FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return "audio" in (res.stdout or "")
    except Exception:
        return False


# ----------------------------------------------------------------------
# yt-dlp
# ----------------------------------------------------------------------
def _base_args() -> list[str]:
    # 这里刻意不放 --no-playlist：它只对"视频+播放列表"型 URL 生效，
    # 由各平台 extractor 自己判断，对 ytsearch 没有影响。
    # 只在真正需要"只要这一条"的调用处（dump / 下载）显式加上，意图更清楚。
    args = ["yt-dlp", "--no-warnings", "--ignore-config",
            "--no-check-certificates", "--socket-timeout", "20"]
    args += settings.ytdlp_proxy_args()
    if settings.ytdlp_cookies_file and Path(settings.ytdlp_cookies_file).exists():
        args += ["--cookies", settings.ytdlp_cookies_file]
    return args


def ytdlp_search(keyword: str, limit: int = 20, timeout: int | None = None) -> list[dict[str, Any]]:
    """用 ytsearchN: 做搜索，--flat-playlist 只取列表不解析详情，快很多。"""
    args = _base_args() + [
        f"ytsearch{limit}:{keyword}",
        "--flat-playlist",
        "--dump-json",
        "--skip-download",
    ]
    res = subprocess.run(args, capture_output=True, text=True,
                         timeout=timeout or settings.search_timeout * 3)
    items: list[dict[str, Any]] = []
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not items and res.returncode != 0:
        raise RuntimeError((res.stderr or "yt-dlp 搜索失败").strip()[:300])
    return items


def ytdlp_dump(url: str) -> dict[str, Any]:
    args = _base_args() + ["--no-playlist", "--dump-json", "--skip-download", url]
    res = subprocess.run(args, capture_output=True, text=True,
                         timeout=settings.download_timeout)
    for line in (res.stdout or "").splitlines():
        if line.strip().startswith("{"):
            return json.loads(line)
    raise RuntimeError((res.stderr or "yt-dlp 解析失败").strip()[:300])


def ytdlp_extract_audio(
    url: str,
    out_dir: Path,
    stem: str,
    progress_cb: ProgressCB | None = None,
    extra_args: Sequence[str] | None = None,
) -> Path:
    """下载并转音频。返回最终文件路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / f"{stem}.%(ext)s")
    args = _base_args() + [
        "--no-playlist",          # 只要这一个视频，不要整个列表
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", settings.audio_format,
        "--audio-quality", settings.audio_bitrate.replace("k", "K"),
        "--no-part",
        "--newline",
        "--progress",
        "-o", template,
    ]
    if settings.embed_metadata:
        args += ["--embed-metadata"]
    if settings.embed_cover:
        args += ["--embed-thumbnail"]
    if extra_args:
        args += list(extra_args)
    args.append(url)

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        tail.append(line.rstrip())
        if len(tail) > 40:
            tail.pop(0)
        if progress_cb:
            m = _PERCENT_RE.search(line)
            if m:
                try:
                    progress_cb(min(99.0, float(m.group(1))))
                except ValueError:
                    pass
    proc.wait(timeout=settings.download_timeout)
    if proc.returncode != 0:
        raise RuntimeError("yt-dlp 下载失败: " + " | ".join(tail[-6:])[:400])

    target = out_dir / f"{stem}{settings.audio_suffix}"
    if target.exists():
        return target
    # yt-dlp 可能生成了别的后缀，兜底找一下
    for cand in sorted(out_dir.glob(f"{stem}.*")):
        if cand.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm", ".aac", ".wav"}:
            return cand
    raise RuntimeError("yt-dlp 未产出音频文件")


def ytdlp_supports(url: str) -> bool:
    """粗判 yt-dlp 是否认这个链接。"""
    try:
        args = _base_args() + ["--simulate", "--dump-json", url]
        res = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return res.returncode == 0 and "{" in (res.stdout or "")
    except Exception:
        return False


def cleanup(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
