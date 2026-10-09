"""全局配置：全部通过环境变量注入，方便 Docker 部署时覆盖。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "y"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


class Settings:
    """运行时配置。所有路径都落在 DATA_DIR 下，方便挂载一个卷搞定持久化。"""

    def __init__(self) -> None:
        # ---------- 存储 ----------
        self.data_dir = Path(_env("DATA_DIR", "./data")).resolve()
        self.cache_dir = Path(_env("CACHE_DIR", str(self.data_dir / "cache"))).resolve()
        self.tmp_dir = Path(_env("TMP_DIR", str(self.data_dir / "tmp"))).resolve()
        self.db_path = Path(_env("DB_PATH", str(self.data_dir / "media_hub.db"))).resolve()

        # ---------- 音频输出 ----------
        self.audio_format = _env("AUDIO_FORMAT", "mp3").lower()  # mp3 / m4a
        self.audio_bitrate = _env("AUDIO_BITRATE", "192k")
        self.embed_cover = _env_bool("EMBED_COVER", True)
        self.embed_metadata = _env_bool("EMBED_METADATA", True)
        self.keep_video = _env_bool("KEEP_VIDEO", False)  # 是否保留原始视频文件

        # ---------- 并发 ----------
        self.max_concurrent_jobs = _env_int("MAX_CONCURRENT_JOBS", 2)
        self.download_timeout = _env_int("DOWNLOAD_TIMEOUT", 600)
        self.search_timeout = _env_int("SEARCH_TIMEOUT", 20)
        self.search_cache_ttl = _env_int("SEARCH_CACHE_TTL", 600)  # 秒，0 表示不缓存

        # ---------- 网络 ----------
        # 形如 http://127.0.0.1:7890 ，YouTube 场景通常需要
        self.proxy = _env("PROXY", "")
        self.ytdlp_cookies_file = _env("YTDLP_COOKIES_FILE", "")  # netscape 格式 cookies.txt

        # ---------- 小红书 ----------
        # 必需字段：a1 / web_session / webId，直接从浏览器 Cookie 复制整串
        # 注意：扫码登录成功后，运行时凭据会覆盖这里的值（见 services/credentials.py）
        self.xhs_cookie = _env("XHS_COOKIE", "")
        # 签名服务地址。为空则尝试本地 playwright；都没有则小红书不可用
        self.xhs_sign_server = _env("XHS_SIGN_SERVER", "").rstrip("/")
        self.xhs_enabled = _env_bool("XHS_ENABLED", True)

        # ---------- 抖音 ----------
        self.douyin_cookie = _env("DOUYIN_COOKIE", "")
        self.douyin_enabled = _env_bool("DOUYIN_ENABLED", True)

        # ---------- 扫码登录代理 ----------
        # 独立容器，用真实 Chromium 完成扫码（两家平台的登录接口都只认浏览器）
        self.login_agent_url = _env("LOGIN_AGENT_URL", "http://login-agent:8100").rstrip("/")
        self.login_enabled = _env_bool("LOGIN_ENABLED", True)
        self.login_timeout = _env_int("LOGIN_TIMEOUT", 360)

        # ---------- YouTube ----------
        self.youtube_enabled = _env_bool("YOUTUBE_ENABLED", True)
        self.youtube_region = _env("YOUTUBE_REGION", "HK")

        # ---------- 其它 ----------
        self.request_ua = _env(
            "REQUEST_UA",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )

    # ------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.cache_dir, self.tmp_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def audio_suffix(self) -> str:
        return ".m4a" if self.audio_format == "m4a" else ".mp3"

    def ytdlp_proxy_args(self) -> list[str]:
        return ["--proxy", self.proxy] if self.proxy else []


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
