"""登录凭据存储。

扫码登录拿到的 Cookie 落到 ``data/cookies.json``，和数据库、音频缓存放在
同一个卷里，所以重启容器不会丢。

取值优先级：
    运行时扫码得到的 cookie  >  ``.env`` 里的静态 cookie

这样「扫码一次，长期有效」；想回到手动配置，把 cookies.json 里的对应项删掉
（或在设置页点「退出登录」）即可。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _path() -> Path:
    return settings.data_dir / "cookies.json"


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        log.warning("cookies.json 解析失败，按空处理")
        return {}


def _save(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_cookie(platform: str) -> str:
    """返回扫码登录保存的 cookie，没有则空串。"""
    with _LOCK:
        entry = _load().get(platform) or {}
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("cookie") or "")


def get_entry(platform: str) -> dict:
    with _LOCK:
        entry = _load().get(platform) or {}
    return dict(entry) if isinstance(entry, dict) else {}


def set_cookie(platform: str, cookie: str, nickname: str = "",
               source: str = "qrcode") -> dict:
    with _LOCK:
        data = _load()
        entry = {
            "cookie": cookie,
            "nickname": nickname,
            "source": source,
            "updated_at": int(time.time()),
        }
        data[platform] = entry
        _save(data)
        return dict(entry)


def clear(platform: str) -> bool:
    with _LOCK:
        data = _load()
        existed = platform in data
        data.pop(platform, None)
        _save(data)
        return existed


def summary() -> list[dict]:
    """只返回元信息，不回传 cookie 明文。"""
    with _LOCK:
        data = _load()
    out: list[dict] = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        cookie = str(entry.get("cookie") or "")
        out.append({
            "platform": key,
            "nickname": entry.get("nickname") or "",
            "source": entry.get("source") or "",
            "updated_at": int(entry.get("updated_at") or 0),
            "has_cookie": bool(cookie),
        })
    return out


def normalize_cookie(raw: str) -> str:
    """把用户粘贴的内容整理成标准 cookie 串。

    用户复制 Cookie 的姿势五花八门，这里都兜住：
      * 纯 cookie 串：``a=1; b=2``
      * 从 DevTools 的 Application → Cookies 表格里多行复制
      * 从请求头里连名字一起复制：``Cookie: a=1; b=2``
      * 从 cURL 里复制：``-H 'cookie: a=1; b=2'``
      * 首尾带引号、带换行

    同名 cookie 保留**最后一个**（浏览器里后来覆盖的才作数）。
    """
    text = (raw or "").strip()
    if not text:
        return ""

    # 处理 `Cookie: ...` / cURL 的 `-H 'cookie: ...'`
    low = text.lower()
    idx = low.find("cookie:")
    if idx != -1:
        text = text[idx + len("cookie:"):]
        for stop in ("'", '"', "\n", "\r", "\\"):
            pos = text.find(stop)
            if pos != -1:
                text = text[:pos]

    pairs: dict[str, str] = {}
    for chunk in text.replace("\r", ";").replace("\n", ";").split(";"):
        chunk = chunk.strip().strip("'\"")
        if not chunk:
            continue
        if "=" in chunk:
            name, _, value = chunk.partition("=")
        elif "\t" in chunk:
            # DevTools 的 Cookies 表格整块复制出来是「名字<TAB>值」
            name, _, value = chunk.partition("\t")
        else:
            continue
        name, value = name.strip(), value.strip()
        if name:
            pairs[name] = value
    return "; ".join(f"{k}={v}" for k, v in pairs.items())

