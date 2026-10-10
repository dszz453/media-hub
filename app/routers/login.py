"""扫码登录接口。

主服务本身不碰浏览器，只做两件事：
  1. 把请求转发给 login-agent（独立容器，跑真实 Chromium）；
  2. 拿到 cookie 后落盘到 data/cookies.json，并让 Provider 自动换用新凭据。

Provider 那边是靠「cookie 变了就重建客户端」来生效的，
所以这里不需要显式通知它刷新。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException

from ..config import settings
from ..providers import get_provider
from ..services import credentials

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/login", tags=["login"])

SUPPORTED = ("xhs", "douyin")
LABELS = {"xhs": "小红书", "douyin": "抖音"}


def _agent_url(path: str) -> str:
    return f"{settings.login_agent_url}{path}"


def _require_enabled() -> None:
    if not settings.login_enabled:
        raise HTTPException(status_code=400, detail="扫码登录已通过 LOGIN_ENABLED 关闭")


def _detail(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return resp.text[:200] or f"HTTP {resp.status_code}"
    return str(payload.get("detail") or payload)


def _agent_ready() -> bool:
    try:
        return httpx.get(_agent_url("/health"), timeout=4).status_code == 200
    except Exception:  # noqa: BLE001
        return False


@router.get("/status")
def login_status() -> dict:
    saved = {item["platform"]: item for item in credentials.summary()}
    platforms = []
    for key in SUPPORTED:
        entry = saved.get(key) or {}
        platforms.append({
            "platform": key,
            "label": LABELS.get(key, key),
            "logged_in": bool(entry.get("has_cookie")),
            "nickname": entry.get("nickname") or "",
            "updated_at": entry.get("updated_at") or 0,
            "source": entry.get("source") or "",
        })
    return {
        "enabled": settings.login_enabled,
        "agent_ready": _agent_ready() if settings.login_enabled else False,
        "platforms": platforms,
    }


@router.post("/{platform}/start")
def start_login(platform: str) -> dict:
    _require_enabled()
    if platform not in SUPPORTED:
        raise HTTPException(status_code=404, detail=f"暂不支持扫码登录：{platform}")
    try:
        resp = httpx.post(_agent_url(f"/session/{platform}"), timeout=settings.login_timeout)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"登录服务不可用（{settings.login_agent_url}）：{exc}",
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=_detail(resp))
    return resp.json()


@router.get("/{platform}/poll/{session_id}")
def poll_login(platform: str, session_id: str) -> dict:
    _require_enabled()
    if platform not in SUPPORTED:
        raise HTTPException(status_code=404, detail=f"暂不支持扫码登录：{platform}")
    try:
        resp = httpx.get(_agent_url(f"/session/{platform}/{session_id}"), timeout=20)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"登录服务不可用：{exc}") from exc
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="扫码会话已失效，请重新获取二维码")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=_detail(resp))

    data = resp.json()
    if data.get("status") == "success" and data.get("cookie"):
        credentials.set_cookie(platform, data["cookie"], data.get("nickname") or "")
        log.info("已保存 %s 的扫码登录凭据（%d 字节）", platform, len(data["cookie"]))
    return data


@router.post("/{platform}/cancel/{session_id}")
def cancel_login(platform: str, session_id: str) -> dict:
    try:
        httpx.delete(_agent_url(f"/session/{platform}/{session_id}"), timeout=15)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


@router.post("/{platform}/cookie")
def import_cookie(platform: str, payload: dict) -> dict:
    """手动粘贴 Cookie 登录。

    关键点是**先校验再落盘**。小红书会给每个访客发一个匿名 web_session，
    只看「有没有 cookie」会把访客会话当成登录态存下来，表现为
    「账号页显示已登录，一搜索就报登录已过期」。所以这里真去搜一次，
    搜得动才算数。

    响应固定 200，用 ok 字段区分成败 —— 前端好把 message 直接显示出来。
    """
    _require_enabled()
    if platform not in SUPPORTED:
        return {"ok": False, "message": f"暂不支持该平台：{platform}"}

    raw = str((payload or {}).get("cookie") or "")
    cookie = credentials.normalize_cookie(raw)
    if not cookie:
        return {"ok": False, "message": "没读到有效的 Cookie，请把整串复制过来"}

    provider = get_provider(platform)
    if provider is None:
        return {"ok": False, "message": f"没有 {platform} 的 Provider 实现"}

    try:
        ok, reason = provider.check_cookie(cookie)
    except Exception as exc:  # noqa: BLE001
        log.exception("校验 %s cookie 时异常", platform)
        return {"ok": False, "message": f"校验过程出错：{exc}"}

    if not ok:
        log.info("%s 粘贴的 cookie 校验未通过：%s", platform, reason)
        return {"ok": False, "message": reason}

    entry = credentials.set_cookie(platform, cookie, nickname="", source="manual")
    log.info("已保存 %s 的手动粘贴凭据（%d 字节，%d 个字段）",
             platform, len(cookie), cookie.count(";") + 1)
    return {
        "ok": True,
        "message": "校验通过，登录凭据已保存",
        "platform": platform,
        "fields": cookie.count(";") + 1,
        "updated_at": entry.get("updated_at") or 0,
    }


@router.delete("/{platform}")
def logout(platform: str) -> dict:
    """退出登录：删掉扫码保存的凭据（.env 里的静态 cookie 不受影响）。"""
    removed = credentials.clear(platform)
    return {"ok": True, "removed": removed}

